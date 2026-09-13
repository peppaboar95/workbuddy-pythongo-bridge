import datetime as dt
import json
import os
import tempfile
import unittest

from workbuddy_pythongo.bootstrap import initialize
from workbuddy_pythongo.util import iso_now, json_text, utc_now
from workbuddy_pythongo.worker import build_runtime


class HealthStateTests(unittest.TestCase):
    def test_observation_readiness_is_separate_from_trade_protection(self):
        with tempfile.TemporaryDirectory() as root:
            initialized = initialize(root)
            config, database, core = build_runtime(initialized["config"])
            adapter_path = os.path.join(initialized["ready_dir"], "pythongo_adapter.json")
            with open(adapter_path, "r", encoding="utf-8") as stream:
                adapter = json.load(stream)
            now = iso_now()
            payload = {
                "adapter_instance": "pythongo_futures_01",
                "account_alias": "main_futures",
                "status": "READY",
                "mode": "OBSERVE_ONLY",
                "profile_status": "UNVERIFIED",
                "occurred_at": now,
                "local_halt": True,
                "margin_policy_generation": adapter["margin_reference_policy_generation"],
                "margin_policy_hash": adapter["margin_reference_policy_hash"],
            }
            with database.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE system_state SET value='true',updated_at=? WHERE key='halted'",
                    (now,),
                )
                connection.execute(
                    "UPDATE system_state SET value='account binding changed',updated_at=? "
                    "WHERE key='halt_reason'",
                    (now,),
                )
                connection.execute(
                    "INSERT INTO heartbeats(adapter_instance,account_alias,status,mode,profile_status,"
                    "occurred_at,received_at,payload_json) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        "pythongo_futures_01",
                        "main_futures",
                        "READY",
                        "OBSERVE_ONLY",
                        "UNVERIFIED",
                        now,
                        now,
                        json_text(payload),
                    ),
                )

            health = core.pythongo_health()
            account = health["accounts"][0]
            self.assertTrue(account["connected"])
            self.assertTrue(account["observation_ready"])
            self.assertFalse(account["trade_ready"])
            self.assertTrue(health["observation_ready"])
            self.assertFalse(health["trade_ready"])
            self.assertEqual(health["protection_level"], "RISK_REDUCING_ONLY")
            self.assertEqual(
                health["trade_protection"],
                {
                    "active": True,
                    "kind": "INCIDENT_HALT",
                    "reason": "account binding changed",
                    "queries_available": True,
                    "blocked_operation": "RISK_INCREASING_TRADES",
                    "risk_reducing_allowed": False,
                },
            )

    def test_halt_blocks_open_but_preserves_strict_close_authorization(self):
        with tempfile.TemporaryDirectory() as root:
            initialized = initialize(root)
            config, database, core = build_runtime(initialized["config"])
            account = config.account("main_futures")
            adapter_path = os.path.join(initialized["ready_dir"], "pythongo_adapter.json")
            with open(adapter_path, "r", encoding="utf-8") as stream:
                adapter = json.load(stream)
            now = iso_now()
            heartbeat_payload = {
                "local_halt": True,
                "limited_auto_protocol": 1,
                "margin_policy_generation": adapter["margin_reference_policy_generation"],
                "margin_policy_hash": adapter["margin_reference_policy_hash"],
            }
            with database.transaction(immediate=True) as connection:
                core._set_state(connection, "mode", "MANUAL_LIVE", now)
                core._set_state(connection, "halted", "true", now)
                connection.execute(
                    "INSERT INTO heartbeats(adapter_instance,account_alias,status,mode,profile_status,"
                    "occurred_at,received_at,payload_json) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        account.adapter_instance, account.alias, "READY", "MANUAL_LIVE", "VALID",
                        now, now, json_text(heartbeat_payload),
                    ),
                )

                def gated(action):
                    return core._apply_system_gates(
                        connection,
                        {"action": action, "execution_mode": "MANUAL_LIVE"},
                        {"risk": {"reasons": [], "allowed": True}, "decision_material": {}},
                        account,
                    )

                opened = gated("OPEN_LONG")
                closed = gated("CLOSE_LONG")
                authorization = core._check_submit_authorization(
                    connection, {}, {"action": "CLOSE_LONG", "execution_mode": "MANUAL_LIVE"},
                    None, closed,
                )

            self.assertIn("TRADING_HALTED", opened["risk"]["reasons"])
            self.assertNotIn("TRADING_HALTED", closed["risk"]["reasons"])
            self.assertEqual(closed["protection_level"], "RISK_REDUCING_ONLY")
            self.assertEqual(authorization["type"], "RISK_REDUCING_ONLY")

    def test_limited_auto_pause_writes_pause_new_open_authorization(self):
        with tempfile.TemporaryDirectory() as root:
            initialized = initialize(root)
            config, database, core = build_runtime(initialized["config"])
            account = config.account("main_futures")
            now = utc_now()
            policy = {
                "permit_id": "permit_test", "generation": 0,
                "starts_at": (now - dt.timedelta(minutes=1)).isoformat(timespec="milliseconds"),
                "expires_at": (now + dt.timedelta(minutes=10)).isoformat(timespec="milliseconds"),
            }
            with database.transaction(immediate=True) as connection:
                connection.execute(
                    "INSERT INTO auto_permits(permit_id,account_alias,status,generation,policy_hash,"
                    "policy_json,actor,reason,created_at,starts_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "permit_test", account.alias, "ACTIVE", 0, "policy_hash", json_text(policy),
                        "test", "test", iso_now(), policy["starts_at"], policy["expires_at"],
                    ),
                )

            self.assertTrue(core._pause_active_auto_permit(
                account.alias, ["AUTO_ACCOUNT_SNAPSHOT_STALE"], actor="test",
            ))
            auth_path = os.path.join(
                config.data_dir, "pythongo_runtime", account.adapter_instance,
                "local_authorization.json",
            )
            with open(auth_path, "r", encoding="utf-8") as stream:
                authorization = json.load(stream)["payload"]
            self.assertEqual(authorization["authorization_type"], "PAUSE_NEW_OPEN")
            self.assertEqual(authorization["allowed_modes"], ["LIMITED_AUTO"])
            adapter_path = os.path.join(initialized["ready_dir"], "pythongo_adapter.json")
            with open(adapter_path, "r", encoding="utf-8") as stream:
                adapter = json.load(stream)
            heartbeat_at = iso_now()
            heartbeat_payload = {
                "local_halt": False,
                "limited_auto_local_pause": False,
                "margin_policy_generation": adapter["margin_reference_policy_generation"],
                "margin_policy_hash": adapter["margin_reference_policy_hash"],
            }
            with database.transaction(immediate=True) as connection:
                core._set_state(connection, "mode", "LIMITED_AUTO", heartbeat_at)
                connection.execute(
                    "INSERT INTO heartbeats(adapter_instance,account_alias,status,mode,profile_status,"
                    "occurred_at,received_at,payload_json) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        account.adapter_instance, account.alias, "READY", "LIMITED_AUTO", "VALID",
                        heartbeat_at, heartbeat_at, json_text(heartbeat_payload),
                    ),
                )
                self.assertIsNone(core._active_auto_permit_row(connection, account.alias))
                paused = core._active_auto_permit_row(connection, account.alias, allow_paused=True)
            self.assertEqual(paused["status"], "PAUSED")
            health = core.pythongo_health()
            self.assertEqual(health["protection_level"], "PAUSE_NEW_OPEN")
            self.assertFalse(health["trade_ready"])
            self.assertTrue(health["trade_protection"]["risk_reducing_allowed"])


if __name__ == "__main__":
    unittest.main()
