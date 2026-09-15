import datetime as dt
import json
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from workbuddy_pythongo.assets.pythongo_embedded_adapter import WorkBuddyPythonGOAdapter
from workbuddy_pythongo.bootstrap import initialize
from workbuddy_pythongo.connection_sync import AdapterConnectionSync
from workbuddy_pythongo.console import bind_investor, set_mode
from workbuddy_pythongo.errors import BridgeError
from workbuddy_pythongo.security import make_envelope, validate_envelope
from workbuddy_pythongo.util import iso_now, utc_now
from workbuddy_pythongo.worker import RuntimeLoop, build_runtime, revoke_startup_authorizations


class StartupOrderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        initialized = initialize(temporary.name)
        self.config_path = initialized["config"]
        self.adapter_path = str(pathlib.Path(initialized["ready_dir"], "pythongo_adapter.json"))
        bind_investor(self.config_path, "main_futures", "TEST-STARTUP-ORDER", "BIND-ACCOUNT")
        self.config, self.database, self.core = build_runtime(self.config_path)
        self.account = self.config.account("main_futures")
        self.halt_path = pathlib.Path(self.config.data_dir, "pythongo_runtime", self.account.adapter_instance, "local_halt.json")
        self.halt_before = self.halt_path.read_bytes()
        config_patch = mock.patch.object(WorkBuddyPythonGOAdapter, "_config_path", new_callable=mock.PropertyMock, return_value=self.adapter_path)
        config_patch.start()
        self.addCleanup(config_patch.stop)
        self.sync = AdapterConnectionSync(self.core)

    def start_adapter(self):
        adapter = WorkBuddyPythonGOAdapter()
        adapter._require_native_base = mock.Mock()
        adapter.output = mock.Mock()
        adapter.sub_market_data = mock.Mock()
        adapter._account = mock.Mock(return_value={"available": 100000.0, "balance": 100000.0})
        adapter._positions = mock.Mock(return_value=[])
        adapter.send_order = mock.Mock(side_effect=AssertionError("Startup must never submit orders"))
        adapter.on_init()
        adapter.on_start()
        self.addCleanup(adapter.on_stop)
        self.assertEqual(adapter._status, "READY")
        return adapter

    def heartbeat(self, occurred=None, status="READY", session="session-1", **updates):
        raw = json.loads(pathlib.Path(self.adapter_path).read_text(encoding="utf-8"))
        payload = {
            "account_alias": self.account.alias, "adapter_instance": self.account.adapter_instance,
            "adapter_session_id": session, "status": status, "mode": "OBSERVE_ONLY",
            "profile_status": "UNVERIFIED", "local_halt": True,
            "margin_policy_generation": raw["margin_reference_policy_generation"],
            "margin_policy_hash": raw["margin_reference_policy_hash"], "occurred_at": occurred or iso_now(),
        }
        payload.update(updates)
        envelope = make_envelope(self.core.keyring, "HEARTBEAT", payload, 86400, "pythongo-embedded-adapter")
        self.core.file_queue.write(self.account.adapter_instance, "events", envelope)
        self.core.ingester.scan_once()
        return envelope

    def commands(self):
        with self.database.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM adapter_commands ORDER BY created_at")]

    def assert_protected(self):
        health = self.core.pythongo_health()
        self.assertEqual(health["mode"], "OBSERVE_ONLY")
        self.assertFalse(health["halted"])
        self.assertEqual(health["trade_protection"]["kind"], "SETUP_LOCK")
        self.assertFalse(health["trade_ready"])
        self.assertEqual(self.halt_path.read_bytes(), self.halt_before)
        self.assertTrue(all(row["command_type"] == "REQUEST_SYNC" for row in self.commands()))

    def wait_for_sync(self, loop):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if loop.connection_sync.states.get(self.account.alias, {}).get("phase") == "READY":
                break
            time.sleep(0.02)
        else:
            self.fail("Worker did not automatically synchronize with Adapter")
        self.assertFalse(loop.failed_closed)
        self.assertTrue(self.core.pythongo_health()["observation_ready"])
        self.assertEqual(len(self.commands()), 1)
        self.assertEqual(self.commands()[0]["status"], "SYNC_COMPLETED")
        self.assertEqual(self.core.file_queue.depths(self.account.adapter_instance)["dead_letter"], 0)
        self.assert_protected()

    def start_loop(self):
        self.core.reconciler.require_on_startup()
        revoke_startup_authorizations(self.config, self.database, self.core)
        loop = RuntimeLoop(self.core, maintenance_interval=0.05)

        def stop():
            loop.stop_event.set()
            loop.join(timeout=2)
        self.addCleanup(stop)
        loop.start()
        return loop

    def test_worker_first_waits_without_expiring_startup_commands(self):
        for now in (0, 61, 86401):
            self.assertEqual(self.sync.scan_once(now), 0)
        self.assertEqual(self.commands(), [])
        loop = self.start_loop()
        time.sleep(0.25)
        self.assertEqual(self.commands(), [])
        adapter = self.start_adapter()
        self.wait_for_sync(loop)
        adapter.send_order.assert_not_called()

    def test_adapter_first_connects_after_worker_starts(self):
        adapter = self.start_adapter()
        self.assertEqual(self.commands(), [])
        # Exercise the real desktop mode selection path with an already loaded Adapter.
        set_mode(self.config_path, "OBSERVE_ONLY")
        loop = self.start_loop()
        self.wait_for_sync(loop)
        adapter.send_order.assert_not_called()

    def test_old_ready_heartbeat_is_stale_and_cannot_overwrite_new_stop(self):
        old = (utc_now() - dt.timedelta(seconds=120)).isoformat()
        self.heartbeat(occurred=old)
        self.assertGreater(self.core.pythongo_health()["accounts"][0]["heartbeat_age_seconds"], 100)
        self.assertEqual(self.sync.scan_once(0), 0)
        self.heartbeat(status="STOPPED")
        self.heartbeat(occurred=old, status="READY")
        self.assertEqual(self.core.pythongo_health()["accounts"][0]["adapter_status"], "STOPPED")
        self.assertEqual(self.sync.scan_once(61), 0)
        self.heartbeat()
        self.assertEqual(self.sync.scan_once(62), 1)
        self.assert_protected()

    def test_expired_signed_heartbeat_is_audited_without_becoming_fresh(self):
        old = utc_now() - dt.timedelta(days=2)
        envelope = self.heartbeat(occurred=old.isoformat())
        envelope["message_id"] += "_expired"
        envelope["issued_at"] = old.isoformat()
        envelope["expires_at"] = (old + dt.timedelta(days=1)).isoformat()
        envelope["signature"] = self.core.keyring.sign(envelope)
        self.core.file_queue.write(self.account.adapter_instance, "events", envelope)
        result = self.core.ingester.scan_once()[self.account.alias]["events"]
        self.assertEqual(result, {"processed": 1, "dead_lettered": 0})
        self.assertEqual(self.sync.scan_once(0), 0)
        with self.database.connect() as connection:
            self.assertIsNotNone(connection.execute("SELECT 1 FROM inbound_events WHERE message_id=?", (envelope["message_id"],)).fetchone())
        for message_type in ("REQUEST_SYNC", "TRADE_INTENT", "ACCOUNT_SNAPSHOT", "COMMAND_ACK"):
            envelope["message_type"] = message_type
            envelope["signature"] = self.core.keyring.sign(envelope)
            with self.assertRaises(BridgeError) as error:
                validate_envelope(envelope, self.core.keyring, allow_expired_heartbeats=True)
            self.assertEqual(error.exception.code, "MESSAGE_EXPIRED")
        envelope["message_type"] = "HEARTBEAT"
        envelope["signature"] = "invalid"
        with self.assertRaises(BridgeError):
            validate_envelope(envelope, self.core.keyring, allow_expired_heartbeats=True)

    def test_configuration_mismatch_waits_for_reload(self):
        self.heartbeat(mode="LIMITED_AUTO")
        self.assertEqual(self.sync.scan_once(0), 0)
        self.heartbeat(margin_policy_hash="old-policy")
        self.assertEqual(self.sync.scan_once(1), 0)
        self.heartbeat()
        self.assertEqual(self.sync.scan_once(2), 1)
        self.assert_protected()

    def test_sync_is_throttled_and_retries_rejection_or_timeout(self):
        self.heartbeat()
        self.assertEqual(self.sync.scan_once(0), 1)
        for now in (1, 5, 30, 60):
            self.assertEqual(self.sync.scan_once(now), 0)
        self.assertEqual(self.sync.scan_once(61), 1)
        self.assertEqual(len(self.commands()), 2)
        with self.database.transaction() as connection:
            connection.execute("UPDATE adapter_commands SET status='ADAPTER_REJECTED' WHERE message_id=?", (self.sync.states[self.account.alias]["message_id"],))
        self.assertEqual(self.sync.scan_once(65), 0)
        self.assertEqual(self.sync.scan_once(66), 1)
        self.assert_protected()

    def test_reconnect_and_fast_adapter_restart_resynchronize(self):
        self.heartbeat(session="one")
        self.assertEqual(self.sync.scan_once(0), 1)
        self.heartbeat(session="one")
        self.assertEqual(self.sync.scan_once(1), 0)
        self.heartbeat(session="two")
        self.assertEqual(self.sync.scan_once(2), 1)
        self.heartbeat(status="STOPPED", session="two")
        self.assertEqual(self.sync.scan_once(3), 0)
        self.heartbeat(session="two")
        self.assertEqual(self.sync.scan_once(4), 1)
        self.assertEqual(len(self.commands()), 3)
        self.assert_protected()

    def test_worker_restart_resynchronizes_running_adapter(self):
        adapter = self.start_adapter()
        self.core.ingester.scan_once()
        self.assertEqual(self.sync.scan_once(0), 1)
        adapter.poll_commands()
        self.core.ingester.scan_once()
        self.assertEqual(self.sync.scan_once(1), 0)
        self.assertEqual(self.sync.states[self.account.alias]["phase"], "READY")
        self.assertEqual(self.sync.scan_once(100), 0)
        _, _, restarted = build_runtime(self.config_path)
        restarted.reconciler.require_on_startup()
        revoke_startup_authorizations(self.config, self.database, restarted)
        new_sync = AdapterConnectionSync(restarted)
        self.assertEqual(new_sync.scan_once(0), 1)
        adapter.poll_commands()
        restarted.ingester.scan_once()
        self.assertEqual(new_sync.scan_once(1), 0)
        self.assertEqual(new_sync.states[self.account.alias]["phase"], "READY")
        self.assertEqual(len(self.commands()), 2)
        adapter.send_order.assert_not_called()
        self.assert_protected()

    def test_startup_reconciliation_waits_for_adapter_and_preserves_unknown(self):
        now = iso_now()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO trade_intents(intent_id,preview_id,account_alias,status,action,exchange,instrument_id,requested_volume,execution_mode,created_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                ("intent_boot", "preview_boot", self.account.alias, "SUBMIT_UNKNOWN", "OPEN_LONG", "SHFE", "au2610", 1, "MANUAL_LIVE", now, now, "{}"),
            )
            connection.execute(
                "INSERT INTO child_orders(child_order_id,intent_id,child_no,client_order_key,memo_token,action,offset,volume,limit_price,status,created_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("child_boot", "intent_boot", 1, "client_boot", "WBBOOT01", "OPEN_LONG", "OPEN", 1, 700.0, "SUBMIT_UNKNOWN", now, now, "{}"),
            )
        self.assertEqual(self.core.reconciler.require_on_startup(), [self.account.alias])
        self.assertEqual(self.sync.scan_once(1000), 0)
        self.assertEqual(self.commands(), [])
        adapter = self.start_adapter()
        self.core.ingester.scan_once()
        self.assertEqual(self.sync.scan_once(1001), 1)
        adapter.poll_commands()
        self.core.ingester.scan_once()
        self.core.reconciler.scan_once()
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT status FROM reconciliation_runs LIMIT 1").fetchone()[0], "MANUAL_REVIEW")
            self.assertEqual(connection.execute("SELECT status FROM child_orders WHERE child_order_id='child_boot'").fetchone()[0], "SUBMIT_UNKNOWN")
            self.assertEqual(connection.execute("SELECT value FROM system_state WHERE key='reconciliation_required:main_futures'").fetchone()[0], "true")
        adapter.send_order.assert_not_called()
        self.assert_protected()

    def test_incident_halt_remains_active_after_connection_sync(self):
        self.core.halt_trading("test incident before reconnect")
        before = self.halt_path.read_bytes()
        adapter = self.start_adapter()
        self.core.ingester.scan_once()
        self.assertEqual(self.sync.scan_once(0), 1)
        adapter.poll_commands()
        self.core.ingester.scan_once()
        self.assertEqual(self.sync.scan_once(1), 0)
        self.assertEqual(self.sync.states[self.account.alias]["phase"], "READY")
        health = self.core.pythongo_health()
        self.assertTrue(health["observation_ready"])
        self.assertTrue(health["halted"])
        self.assertEqual(health["trade_protection"]["kind"], "INCIDENT_HALT")
        self.assertFalse(health["trade_ready"])
        self.assertEqual(self.halt_path.read_bytes(), before)
        adapter.send_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
