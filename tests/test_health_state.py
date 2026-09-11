import json
import os
import tempfile
import unittest

from workbuddy_pythongo.bootstrap import initialize
from workbuddy_pythongo.util import iso_now, json_text
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
            self.assertEqual(
                health["trade_protection"],
                {
                    "active": True,
                    "kind": "INCIDENT_HALT",
                    "reason": "account binding changed",
                    "queries_available": True,
                    "blocked_operation": "NEW_TRADES",
                },
            )


if __name__ == "__main__":
    unittest.main()
