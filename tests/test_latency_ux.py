import json
import os
import tempfile
import unittest

from workbuddy_pythongo.assets.pythongo_embedded_adapter import WorkBuddyPythonGOAdapter
from workbuddy_pythongo.bootstrap import initialize
from workbuddy_pythongo.config import load_config
from workbuddy_pythongo.console import _sync_adapter_mode
from workbuddy_pythongo.doctor import run_doctor
from workbuddy_pythongo.errors import BridgeError
from workbuddy_pythongo.security import make_envelope
from workbuddy_pythongo.util import iso_now, json_text
from workbuddy_pythongo.worker import RuntimeLoop, build_runtime


class LatencyUxTests(unittest.TestCase):
    def test_submit_queue_returns_explicit_async_status(self):
        with tempfile.TemporaryDirectory() as root:
            initialized = initialize(root)
            _, database, core = build_runtime(initialized["config"])
            now = iso_now()
            intent_id = "intent_latency_test"
            child_id = "child_latency_test"
            payload = {
                "type": "EXECUTE_ORDER",
                "intent_id": intent_id,
                "child_order_id": child_id,
            }
            envelope = make_envelope(
                core.keyring, "TRADE_INTENT", payload, 60,
                "pythongo-bridge-worker", intent_id,
            )
            with database.transaction(immediate=True) as connection:
                connection.execute(
                    "INSERT INTO trade_intents(intent_id,preview_id,account_alias,status,action,exchange,"
                    "instrument_id,requested_volume,execution_mode,source_signal_id,created_at,updated_at,payload_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        intent_id, "preview_latency_test", "main_futures", "PERSISTED",
                        "OPEN_LONG", "SHFE", "au2610", 1, "OBSERVE_ONLY",
                        "signal_latency_test", now, now, json_text(payload),
                    ),
                )
                connection.execute(
                    "INSERT INTO child_orders(child_order_id,intent_id,child_no,client_order_key,memo_token,"
                    "action,offset,volume,limit_price,status,created_at,updated_at,payload_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        child_id, intent_id, 1, "client_latency_test", "WBLATENCY01",
                        "OPEN_LONG", "OPEN", 1, 700.0, "PENDING_DELIVERY",
                        now, now, json_text(payload),
                    ),
                )
                connection.execute(
                    "INSERT INTO adapter_commands(message_id,intent_id,child_order_id,account_alias,command_type,"
                    "folder,status,created_at,payload_json) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        envelope["message_id"], intent_id, child_id, "main_futures",
                        "TRADE_INTENT", "commands", "PENDING_DELIVERY", now, json_text(envelope),
                    ),
                )

            core.dispatch_pending_commands("main_futures")
            detail = core.get_trade_intent(intent_id)

            self.assertEqual(detail["intent"]["status"], "QUEUED")
            self.assertEqual(detail["children"][0]["status"], "QUEUED")
            self.assertEqual(detail["async_status"]["state"], "QUEUED")
            self.assertTrue(detail["async_status"]["accepted"])
            self.assertTrue(detail["async_status"]["queue_delivered"])
            self.assertFalse(detail["async_status"]["broker_acknowledged"])
            self.assertFalse(detail["async_status"]["terminal"])
            self.assertEqual(detail["async_status"]["poll_after_ms"], 150)

            with database.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE child_orders SET status='CANCELLED',updated_at=? WHERE child_order_id=?",
                    (iso_now(), child_id),
                )
                connection.execute(
                    "INSERT INTO adapter_command_acks(event_id,message_id,intent_id,child_order_id,account_alias,"
                    "status,occurred_at,payload_json) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        "event_latency_test", envelope["message_id"], intent_id, child_id,
                        "main_futures", "SEND_RETURNED", iso_now(), "{}",
                    ),
                )
                connection.execute(
                    "INSERT INTO orders(account_alias,trading_day,pythongo_order_id,intent_id,child_order_id,"
                    "exchange,instrument_id,status,updated_at,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        "main_futures", "20260912", 1, intent_id, child_id,
                        "SHFE", "au2610", "CANCELLED", iso_now(), "{}",
                    ),
                )
                core.ingester._aggregate_intent(connection, child_id)
            cancelled = core.get_trade_intent(intent_id)
            self.assertEqual(cancelled["async_status"]["state"], "CANCELLED")
            self.assertTrue(cancelled["async_status"]["native_send_returned"])
            self.assertTrue(cancelled["async_status"]["broker_acknowledged"])
            self.assertTrue(cancelled["async_status"]["terminal"])
            self.assertIsNone(cancelled["async_status"]["poll_after_ms"])

    def test_trade_sync_rejects_kline_from_hot_path(self):
        with tempfile.TemporaryDirectory() as root:
            initialized = initialize(root)
            _, _, core = build_runtime(initialized["config"])
            with self.assertRaises(BridgeError) as raised:
                core.request_sync(
                    "main_futures", ["QUOTE", "KLINE"],
                    [{"exchange": "SHFE", "instrument_id": "au2610"}],
                    {"interval": "M1", "count": 20}, purpose="TRADE",
                )
            self.assertEqual(raised.exception.code, "INVALID_REQUEST")
            self.assertIn("trade hot path", raised.exception.message)

    def test_adapter_pre_subscribes_bridge_allowlist(self):
        with tempfile.TemporaryDirectory() as root:
            initialized = initialize(root)
            with open(initialized["config"], "r", encoding="utf-8") as stream:
                bridge = json.load(stream)
            bridge["accounts"][0]["instrument_allowlist"] = [
                {"exchange": "gfex", "instrument_id": "si2610"},
                {"exchange": "SHFE", "instrument_id": "au2610"},
            ]
            with open(initialized["config"], "w", encoding="utf-8") as stream:
                json.dump(bridge, stream, ensure_ascii=False)
            config = load_config(initialized["config"])
            _sync_adapter_mode(config, "OBSERVE_ONLY")
            adapter_path = os.path.join(initialized["ready_dir"], "pythongo_adapter.json")
            with open(adapter_path, "r", encoding="utf-8") as stream:
                adapter_config = json.load(stream)

            calls = []
            adapter = WorkBuddyPythonGOAdapter()
            adapter._config = adapter_config
            adapter.sub_market_data = lambda **kwargs: calls.append(kwargs)
            subscribed, warnings = adapter._pre_subscribe_quotes()

            self.assertEqual(subscribed, 2)
            self.assertEqual(warnings, [])
            self.assertEqual(
                calls,
                [
                    {"exchange": "GFEX", "instrument_id": "si2610"},
                    {"exchange": "SHFE", "instrument_id": "au2610"},
                ],
            )
            self.assertEqual(adapter_config["command_scan_active_ms"], 100)
            self.assertEqual(adapter_config["command_scan_idle_ms"], 200)

    def test_existing_adapter_config_can_upgrade_without_performance_migration(self):
        with tempfile.TemporaryDirectory() as root:
            initialized = initialize(root)
            adapter_path = os.path.join(initialized["ready_dir"], "pythongo_adapter.json")
            with open(adapter_path, "r", encoding="utf-8") as stream:
                adapter_config = json.load(stream)
            for name in (
                "command_scan_active_ms", "command_scan_idle_ms", "pre_subscribe_instruments",
            ):
                adapter_config.pop(name)
            with open(adapter_path, "w", encoding="utf-8") as stream:
                json.dump(adapter_config, stream, ensure_ascii=False)

            report = run_doctor(initialized["config"])
            schema = next(
                item for item in report["checks"]
                if item["name"] == "main_futures:adapter_config_schema"
            )
            self.assertTrue(schema["ok"])

    def test_worker_uses_100_to_200_ms_adaptive_scan(self):
        loop = RuntimeLoop(object())
        self.assertEqual(loop.active_interval, 0.1)
        self.assertEqual(loop.idle_interval, 0.2)
        self.assertTrue(RuntimeLoop._ingest_activity({
            "main_futures": {"events": {"processed": 1, "dead_lettered": 0}},
        }))
        self.assertFalse(RuntimeLoop._ingest_activity({
            "main_futures": {"events": {"processed": 0, "dead_lettered": 0}},
        }))


if __name__ == "__main__":
    unittest.main()
