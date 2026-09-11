import contextlib
import io
import json
import os
import tempfile
import unittest

from workbuddy_pythongo.bootstrap import initialize
from workbuddy_pythongo.errors import BridgeError
from workbuddy_pythongo.manager import main as manager_main
from workbuddy_pythongo.margin_policy import margin_policy_hash
from workbuddy_pythongo.margin_reference import (
    migrate_margin_policy,
    migrate_margin_policy_if_safe,
    refresh_margin_reference,
)
from workbuddy_pythongo.worker import build_runtime


def _csv_bytes():
    headers = [
        "交易所名称", "合约代码", "保证金-买", "保证金-卖", "保证金-每手",
        "手续费标准-开仓-万分之", "手续费标准-开仓-元",
        "手续费标准-平昨-万分之", "手续费标准-平昨-元",
        "手续费标准-平今-万分之", "手续费标准-平今-元", "手续费更新时间",
    ]
    row = [
        "上海期货交易所", "au2610", "12", "13", "90000",
        "0", "10", "0", "10", "0", "20", "2026-09-07 09:00:00",
    ]
    return (",".join(headers) + "\n" + ",".join(row) + "\n").encode("utf-8")


def _paths(result):
    config_path = result["config"]
    adapter_path = os.path.join(result["ready_dir"], "pythongo_adapter.json")
    profile_path = os.path.join(result["ready_dir"], "pythongo_profile.json")
    return config_path, adapter_path, profile_path


class MarginPolicyTests(unittest.TestCase):
    def test_bootstrap_binds_policy_generation_and_hash(self):
        with tempfile.TemporaryDirectory() as root:
            _, adapter_path, _ = _paths(initialize(root))
            with open(adapter_path, "r", encoding="utf-8") as stream:
                adapter = json.load(stream)
            self.assertEqual(adapter["margin_reference_policy_generation"], 1)
            self.assertEqual(adapter["margin_reference_policy_hash"], margin_policy_hash(adapter))

    def test_daily_refresh_preserves_profile_and_adapter_config(self):
        with tempfile.TemporaryDirectory() as root:
            config_path, adapter_path, profile_path = _paths(initialize(root))
            source_path = os.path.join(root, "source.csv")
            with open(source_path, "wb") as stream:
                stream.write(_csv_bytes())
            with open(adapter_path, "rb") as stream:
                adapter_before = stream.read()
            with open(profile_path, "rb") as stream:
                profile_before = stream.read()

            result = refresh_margin_reference(config_path, source_path)

            with open(adapter_path, "rb") as stream:
                self.assertEqual(stream.read(), adapter_before)
            with open(profile_path, "rb") as stream:
                self.assertEqual(stream.read(), profile_before)
            self.assertTrue(result["profiles_preserved"])
            self.assertEqual(result["policies"][0]["policy_generation"], 1)

    def test_refresh_refuses_to_migrate_old_config(self):
        with tempfile.TemporaryDirectory() as root:
            config_path, adapter_path, profile_path = _paths(initialize(root))
            with open(adapter_path, "r", encoding="utf-8") as stream:
                adapter = json.load(stream)
            adapter.pop("margin_reference_policy_generation")
            adapter.pop("margin_reference_policy_hash")
            with open(adapter_path, "w", encoding="utf-8") as stream:
                json.dump(adapter, stream, ensure_ascii=False)
            with open(adapter_path, "rb") as stream:
                adapter_before = stream.read()
            with open(profile_path, "rb") as stream:
                profile_before = stream.read()

            with self.assertRaises(BridgeError) as raised:
                refresh_margin_reference(config_path, os.path.join(root, "not-read.csv"))

            self.assertEqual(raised.exception.code, "MARGIN_POLICY_MIGRATION_REQUIRED")
            with open(adapter_path, "rb") as stream:
                self.assertEqual(stream.read(), adapter_before)
            with open(profile_path, "rb") as stream:
                self.assertEqual(stream.read(), profile_before)

    def test_tracking_only_migration_preserves_profile_without_halt(self):
        with tempfile.TemporaryDirectory() as root:
            config_path, adapter_path, profile_path = _paths(initialize(root))
            with open(adapter_path, "r", encoding="utf-8") as stream:
                adapter = json.load(stream)
            adapter.pop("margin_reference_policy_generation")
            adapter.pop("margin_reference_policy_hash")
            with open(adapter_path, "w", encoding="utf-8") as stream:
                json.dump(adapter, stream, ensure_ascii=False)
            with open(profile_path, "rb") as stream:
                profile_before = stream.read()

            result = migrate_margin_policy(config_path, "MIGRATE-MARGIN-POLICY")

            self.assertFalse(result["material_change"])
            self.assertFalse(result["halted_for_policy_change"])
            self.assertTrue(result["profiles_preserved"])
            with open(profile_path, "rb") as stream:
                self.assertEqual(stream.read(), profile_before)
            with open(adapter_path, "r", encoding="utf-8") as stream:
                migrated = json.load(stream)
            self.assertEqual(migrated["margin_reference_policy_generation"], 1)
            self.assertEqual(migrated["margin_reference_policy_hash"], margin_policy_hash(migrated))

    def test_material_migration_halts_but_preserves_profile(self):
        with tempfile.TemporaryDirectory() as root:
            config_path, adapter_path, profile_path = _paths(initialize(root))
            with open(adapter_path, "r", encoding="utf-8") as stream:
                adapter = json.load(stream)
            adapter["margin_reference_max_age_hours"] = adapter.pop(
                "margin_reference_refresh_max_age_hours"
            )
            adapter.pop("margin_reference_source_warn_age_hours")
            adapter.pop("margin_reference_policy_generation")
            adapter.pop("margin_reference_policy_hash")
            with open(adapter_path, "w", encoding="utf-8") as stream:
                json.dump(adapter, stream, ensure_ascii=False)
            with open(profile_path, "rb") as stream:
                profile_before = stream.read()

            result = migrate_margin_policy(config_path, "MIGRATE-MARGIN-POLICY")

            self.assertTrue(result["material_change"])
            self.assertTrue(result["halted_for_policy_change"])
            self.assertTrue(result["profiles_preserved"])
            _, database, _ = build_runtime(config_path)
            with database.connect() as connection:
                kind = connection.execute(
                    "SELECT value FROM system_state WHERE key='trade_protection_kind'"
                ).fetchone()["value"]
            self.assertEqual(kind, "POLICY_REVIEW")
            with open(profile_path, "rb") as stream:
                self.assertEqual(stream.read(), profile_before)

    def test_safe_installer_migration_declines_material_change_without_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            config_path, adapter_path, profile_path = _paths(initialize(root))
            with open(adapter_path, "r", encoding="utf-8") as stream:
                adapter = json.load(stream)
            adapter["margin_reference_max_age_hours"] = adapter.pop(
                "margin_reference_refresh_max_age_hours"
            )
            adapter.pop("margin_reference_source_warn_age_hours")
            adapter.pop("margin_reference_policy_generation")
            adapter.pop("margin_reference_policy_hash")
            with open(adapter_path, "w", encoding="utf-8") as stream:
                json.dump(adapter, stream, ensure_ascii=False)
            with open(adapter_path, "rb") as stream:
                adapter_before = stream.read()
            with open(profile_path, "rb") as stream:
                profile_before = stream.read()

            result = migrate_margin_policy_if_safe(config_path)

            self.assertTrue(result["automatic"])
            self.assertTrue(result["review_required"])
            self.assertTrue(result["material_change"])
            self.assertFalse(result["migrated"])
            self.assertFalse(result["halted_for_policy_change"])
            with open(adapter_path, "rb") as stream:
                self.assertEqual(stream.read(), adapter_before)
            with open(profile_path, "rb") as stream:
                self.assertEqual(stream.read(), profile_before)
            _, database, _ = build_runtime(config_path, require_margin_policy=False)
            with database.connect() as connection:
                halted = connection.execute(
                    "SELECT value FROM system_state WHERE key='halted'"
                ).fetchone()["value"]
            self.assertEqual(halted, "false")

    def test_tracking_generation_never_moves_backwards(self):
        with tempfile.TemporaryDirectory() as root:
            config_path, adapter_path, _ = _paths(initialize(root))
            _, database, _ = build_runtime(config_path)
            with database.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE system_state SET value='5' WHERE key='margin_policy_generation:main_futures'"
                )

            result = migrate_margin_policy(config_path, "MIGRATE-MARGIN-POLICY")

            self.assertFalse(result["material_change"])
            self.assertEqual(result["accounts"][0]["policy_generation"], 5)
            with open(adapter_path, "r", encoding="utf-8") as stream:
                adapter = json.load(stream)
            self.assertEqual(adapter["margin_reference_policy_generation"], 5)

    def test_manager_uses_distinct_exit_code_for_required_migration(self):
        with tempfile.TemporaryDirectory() as root:
            config_path, adapter_path, _ = _paths(initialize(root))
            with open(adapter_path, "r", encoding="utf-8") as stream:
                adapter = json.load(stream)
            adapter.pop("margin_reference_policy_generation")
            adapter.pop("margin_reference_policy_hash")
            with open(adapter_path, "w", encoding="utf-8") as stream:
                json.dump(adapter, stream, ensure_ascii=False)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = manager_main([
                    "--config", config_path, "refresh-margin-reference", "--if-due",
                ])
            self.assertEqual(code, 3)
            self.assertIn("MARGIN_POLICY_MIGRATION_REQUIRED", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
