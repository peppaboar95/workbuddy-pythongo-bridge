import json
import os
import tempfile
import unittest

from workbuddy_pythongo.bootstrap import initialize
from workbuddy_pythongo.close_split import split_order
from workbuddy_pythongo.errors import BridgeError
from workbuddy_pythongo.futures import validate_trade_request
from workbuddy_pythongo.margin_reference import validate_margin_csv
from workbuddy_pythongo.modes import normalize_mode


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

