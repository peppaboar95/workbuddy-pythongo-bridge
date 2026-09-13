import json
import os
import tempfile
import unittest

from workbuddy_pythongo.bootstrap import initialize
from workbuddy_pythongo.assets.pythongo_embedded_adapter import WorkBuddyPythonGOAdapter
from workbuddy_pythongo.close_split import split_order
from workbuddy_pythongo.config import load_config
from workbuddy_pythongo.errors import BridgeError
from workbuddy_pythongo.futures import build_preview, validate_trade_request
from workbuddy_pythongo.margin_reference import validate_margin_csv
from workbuddy_pythongo.modes import normalize_mode
from workbuddy_pythongo.util import iso_now


def _trade_request(**overrides):
    value = {
        "account_alias": "main_futures",
        "instrument": {"exchange": "shfe", "instrument_id": "au2610"},
        "action": "OPEN_LONG",
        "sizing": {"type": "FIXED_VOLUME", "value": 1},
        "price_policy": {"type": "FIXED_LIMIT", "limit_price": 600.0},
        "hedge_flag": "SPECULATION",
        "execution_mode": "OBSERVE_ONLY",
        "source": {"type": "HUMAN", "signal_id": "example"},
    }
    value.update(overrides)
    return value


class CloseSplitTests(unittest.TestCase):
    def test_open_order_is_not_split(self):
        self.assertEqual(
            split_order("OPEN_LONG", 2, None, "TODAY_FIRST"),
            [{"action": "OPEN_LONG", "direction": "BUY", "offset": "OPEN", "volume": 2}],
        )

    def test_today_first_close_is_split_conservatively(self):
        position = {"long": {"td_close_available": 1, "yd_close_available": 3}}
        self.assertEqual(
            split_order("CLOSE_LONG", 3, position, "TODAY_FIRST"),
            [
                {"action": "CLOSE_TODAY_LONG", "direction": "SELL", "offset": "CLOSE_TODAY", "volume": 1},
                {"action": "CLOSE_YESTERDAY_LONG", "direction": "SELL", "offset": "CLOSE_YESTERDAY", "volume": 2},
            ],
        )

    def test_explicit_only_rejects_generic_close(self):
        position = {"short": {"td_close_available": 1, "yd_close_available": 1}}
        with self.assertRaises(BridgeError) as raised:
            split_order("CLOSE_SHORT", 1, position, "EXPLICIT_ONLY")
        self.assertEqual(raised.exception.code, "EXPLICIT_OFFSET_REQUIRED")

    def test_insufficient_position_fails_closed(self):
        position = {"long": {"td_close_available": 0, "yd_close_available": 1}}
        with self.assertRaises(BridgeError) as raised:
            split_order("CLOSE_LONG", 2, position, "TODAY_FIRST")
        self.assertEqual(raised.exception.code, "POSITION_INSUFFICIENT")


class ValidationTests(unittest.TestCase):
    def test_trade_request_normalizes_exchange(self):
        normalized = validate_trade_request(_trade_request())
        self.assertEqual(normalized["instrument"], {"exchange": "SHFE", "instrument_id": "au2610"})
        self.assertEqual(normalized["price_policy"]["max_deviation_pct"], 0.02)

    def test_boolean_volume_is_rejected(self):
        request = _trade_request(sizing={"type": "FIXED_VOLUME", "value": True})
        with self.assertRaises(BridgeError) as raised:
            validate_trade_request(request)
        self.assertEqual(raised.exception.code, "INVALID_REQUEST")

    def test_unknown_request_field_is_rejected(self):
        request = _trade_request(unexpected="value")
        with self.assertRaises(BridgeError):
            validate_trade_request(request)

    def test_price_policy_accepts_a_stricter_tick_deviation(self):
        request = _trade_request(price_policy={
            "type": "FIXED_LIMIT", "limit_price": 600.0,
            "max_deviation_pct": 0.01, "max_deviation_ticks": 3,
        })
        normalized = validate_trade_request(request)
        self.assertEqual(normalized["price_policy"]["max_deviation_ticks"], 3)

    def test_legacy_mode_only_migrates_when_explicitly_allowed(self):
        with self.assertRaises(ValueError):
            normalize_mode("READ_ONLY")
        self.assertEqual(normalize_mode("READ_ONLY", allow_legacy=True), "OBSERVE_ONLY")


class BootstrapTests(unittest.TestCase):
    def test_initialize_is_safe_and_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            first = initialize(root)
            token_path = os.path.join(root, "data", "secrets", "worker.token")
            with open(token_path, "rb") as stream:
                token_before = stream.read()

            second = initialize(root)
            with open(token_path, "rb") as stream:
                token_after = stream.read()

            with open(first["config"], "r", encoding="utf-8") as stream:
                config = json.load(stream)
            with open(os.path.join(first["ready_dir"], "pythongo_profile.json"), "r", encoding="utf-8") as stream:
                profile = json.load(stream)

            self.assertEqual(config["default_mode"], "OBSERVE_ONLY")
            self.assertEqual(profile["verified"], False)
            self.assertEqual(token_before, token_after)
            self.assertNotIn(token_path, second["created"])

    def test_small_conservative_preset_is_available_for_new_runtime(self):
        with tempfile.TemporaryDirectory() as root:
            initialized = initialize(root, risk_preset="SMALL_CONSERVATIVE")
            config = load_config(initialized["config"])
            limits = config.account("main_futures").risk_limits
            self.assertEqual(limits.max_order_volume, 1)
            self.assertEqual(limits.max_price_deviation_ticks, 5)
            self.assertEqual(limits.trade_max_quote_age_seconds, 3)


class RiskBehaviorTests(unittest.TestCase):
    def test_financial_exposure_limits_block_open_but_not_strict_close(self):
        with tempfile.TemporaryDirectory() as root:
            initialized = initialize(root)
            account = load_config(initialized["config"]).account("main_futures")
            captured = iso_now()
            account_snapshot = {
                "captured_at": captured,
                "payload": {
                    "dynamic_rights": 100000.0, "available": 10000.0,
                    "margin": 90000.0, "risk": 0.95,
                    "close_profit": -20000.0, "position_profit": 0.0,
                },
            }
            position = {
                "captured_at": captured,
                "payload": {
                    "position": 1,
                    "long": {"position": 1, "td_close_available": 1, "yd_close_available": 0},
                    "short": {"position": 0, "td_close_available": 0, "yd_close_available": 0},
                },
            }
            quote = {
                "captured_at": captured,
                "payload": {
                    "last_price": 600.0, "price_tick": 1.0, "volume_multiple": 10,
                    "lower_limit_price": 500.0, "upper_limit_price": 700.0,
                    "margin_per_lot": 1000.0, "margin_ratio": 0.1,
                },
            }
            counts = {"orders": 100, "cancels": 100, "total_position_volume": 1}

            opened = build_preview(
                _trade_request(action="OPEN_LONG"), account,
                account_snapshot, position, quote, [], counts,
            )
            closed = build_preview(
                _trade_request(action="CLOSE_LONG"), account,
                account_snapshot, position, quote, [], counts,
            )

            self.assertIn("MAX_RISK_RATIO", opened["risk"]["reasons"])
            self.assertIn("MAX_TOTAL_MARGIN", opened["risk"]["reasons"])
            self.assertIn("MAX_DAILY_LOSS", opened["risk"]["reasons"])
            self.assertTrue(closed["risk"]["allowed"])
            self.assertFalse(closed["decision_material"]["risk_increasing"])

    def test_adapter_halt_override_requires_an_exact_available_close(self):
        with tempfile.TemporaryDirectory() as root:
            initialized = initialize(root)
            adapter_path = os.path.join(initialized["ready_dir"], "pythongo_adapter.json")
            with open(adapter_path, "r", encoding="utf-8") as stream:
                adapter_config = json.load(stream)
            adapter_config["pythongo_mode"] = "MANUAL_LIVE"
            adapter = WorkBuddyPythonGOAdapter()
            adapter._config = adapter_config
            adapter._load_profile = lambda: ("VALID", {"mappings": {}})
            adapter._local_halted = lambda: True
            adapter._local_authorized = lambda *_args: None
            adapter._quote = lambda *_args: {
                "price_tick": 1.0, "volume_multiple": 10,
                "upper_limit_price": 700.0, "lower_limit_price": 500.0,
                "last_price": 600.0,
            }
            adapter._account = lambda: {
                "dynamic_rights": 100000.0, "available": 1000.0,
                "margin": 90000.0, "risk": 0.95,
            }
            adapter._mapping = lambda _command: {"hedgeflag": "1"}
            adapter._target_position = lambda *_args: {
                "long": {"td_close_available": 1, "yd_close_available": 0},
                "short": {"td_close_available": 0, "yd_close_available": 0},
            }
            command = {
                "action": "CLOSE_TODAY_LONG", "direction": "SELL", "offset": "CLOSE_TODAY",
                "volume": 1, "type": "EXECUTE_ORDER", "intent_id": "intent_test",
                "child_order_id": "child_test", "child_no": 1, "client_order_key": "client_test",
                "memo_token": "WBTEST", "account_alias": "main_futures", "account_type": "FUTURES",
                "adapter_instance": "pythongo_futures_01", "exchange": "SHFE",
                "instrument_id": "au2610", "limit_price": 600.0, "hedge_flag": "SPECULATION",
                "execution_mode": "MANUAL_LIVE", "risk_decision_id": "risk_test",
                "source_signal_id": "signal_test", "risk_limits": {},
                "semantic_action": "CLOSE_LONG", "sizing_type": "FIXED_VOLUME",
                "order_notional": 6000.0, "source": {}, "auto_permit": None,
            }

            quote, _ = adapter._final_risk(command)
            self.assertEqual(quote["last_price"], 600.0)
            command["direction"] = "BUY"
            with self.assertRaises(RuntimeError):
                adapter._final_risk(command)

    def test_adapter_keeps_exact_cancel_available_while_halted(self):
        with tempfile.TemporaryDirectory() as root:
            initialized = initialize(root)
            adapter_path = os.path.join(initialized["ready_dir"], "pythongo_adapter.json")
            with open(adapter_path, "r", encoding="utf-8") as stream:
                adapter_config = json.load(stream)
            acknowledgements = []
            adapter = WorkBuddyPythonGOAdapter()
            adapter._config = adapter_config
            adapter._local_halted = lambda: True
            adapter.cancel_order = lambda order_id: 0 if order_id == 42 else -1
            adapter._ack = lambda command, status, message_id, details, folder: acknowledgements.append(
                (status, message_id, details, folder)
            )

            adapter._execute_cancel(
                {"account_alias": "main_futures", "pythongo_order_id": 42},
                "message_cancel",
            )

            self.assertEqual(acknowledgements[0][0], "CANCEL_REQUEST_SENT")
            self.assertEqual(acknowledgements[0][3], "control_acks")


class MarginReferenceTests(unittest.TestCase):
    def test_valid_csv_summary_does_not_expose_row_data(self):
        headers = [
            "交易所名称", "合约代码", "保证金-买", "保证金-卖", "保证金-每手",
            "手续费标准-开仓-万分之", "手续费标准-开仓-元",
            "手续费标准-平昨-万分之", "手续费标准-平昨-元",
            "手续费标准-平今-万分之", "手续费标准-平今-元", "手续费更新时间",
        ]
        row = ["上海期货交易所", "au2610", "12", "13", "90000", "0", "10", "0", "10", "0", "20", "2026-09-07 09:00:00"]
        encoded = (",".join(headers) + "\n" + ",".join(row) + "\n").encode("utf-8")
        summary = validate_margin_csv(encoded)
        self.assertEqual(summary["valid_records"], 1)
        self.assertEqual(summary["rejected_records"], 0)
        self.assertNotIn("records", summary)

    def test_duplicate_contract_is_removed(self):
        headers = [
            "交易所名称", "合约代码", "保证金-买", "保证金-卖", "保证金-每手",
            "手续费标准-开仓-万分之", "手续费标准-开仓-元",
            "手续费标准-平昨-万分之", "手续费标准-平昨-元",
            "手续费标准-平今-万分之", "手续费标准-平今-元", "手续费更新时间",
        ]
        row = ["上海期货交易所", "au2610", "12", "13", "90000", "0", "10", "0", "10", "0", "20", "2026-09-07 09:00:00"]
        encoded = (",".join(headers) + "\n" + ",".join(row) + "\n" + ",".join(row) + "\n").encode("utf-8")
        with self.assertRaises(BridgeError) as raised:
            validate_margin_csv(encoded)
        self.assertEqual(raised.exception.code, "MARGIN_REFERENCE_INVALID")


if __name__ == "__main__":
    unittest.main()
