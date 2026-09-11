import contextlib
import io
import json
import os
import pathlib
import tempfile
import unittest

from workbuddy_pythongo.desktop import (
    _status_issues,
    create_shortcuts,
    print_status_human,
    run_setup,
)


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class DesktopSetupTests(unittest.TestCase):
    def test_setup_accepts_plaintext_account_input(self):
        with tempfile.TemporaryDirectory() as root:
            answers = iter(["", "n", "", "TEST-ACCOUNT-001", "n"])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = run_setup(root, input_func=lambda _prompt: next(answers))

            text = output.getvalue()
            self.assertIn("不是密码", text)
            self.assertIn("明文显示", text)
            adapter_path = os.path.join(result["ready_dir"], "pythongo_adapter.json")
            with open(adapter_path, "r", encoding="utf-8") as stream:
                adapter = json.load(stream)
            self.assertEqual(adapter["investor_id"], "TEST-ACCOUNT-001")

    def test_shortcuts_pin_the_selected_python_executable(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as shortcuts:
            answers = iter(["", "n", "n", "n"])
            with contextlib.redirect_stdout(io.StringIO()):
                result = run_setup(root, input_func=lambda _prompt: next(answers))
            python_executable = os.path.abspath(os.path.join(root, "Python Path", "python.exe"))

            create_shortcuts(result["config"], shortcuts, python_executable=python_executable)

            launcher = pathlib.Path(shortcuts, "启动PythonGO桥接.cmd").read_text(
                encoding="mbcs" if os.name == "nt" else None,
            )
            self.assertIn('"%s" -m workbuddy_pythongo.desktop' % python_executable, launcher)
            self.assertIn('"%s" -m workbuddy_pythongo.manager' % python_executable, launcher)

    def test_installer_keeps_the_original_cmd_wheel_flow(self):
        installer = (REPO_ROOT / "首次安装与配置.cmd").read_text(encoding="utf-8")
        launcher_probe = installer.index('py -3 -c "import sys;')
        python_probe = installer.index('python -c "import sys;')
        self.assertLess(launcher_probe, python_probe)
        self.assertIn("-m pip install --user --upgrade --force-reinstall --no-deps", installer)
        self.assertIn("-m workbuddy_pythongo.desktop setup", installer)
        self.assertNotIn("--repair", installer)

    def test_observe_mode_reports_query_ready_while_trade_protection_is_active(self):
        health = {
            "worker": "READY",
            "mode": "OBSERVE_ONLY",
            "halted": True,
            "halt_reason": "account binding changed",
            "observation_ready": True,
            "trade_ready": False,
            "trade_protection": {
                "active": True,
                "reason": "account binding changed",
                "queries_available": True,
            },
            "accounts": [{
                "account_alias": "main_futures",
                "adapter_status": "READY",
                "heartbeat_age_seconds": 1,
                "adapter_mode": "OBSERVE_ONLY",
                "profile_status": "UNVERIFIED",
                "local_halt": True,
                "ready": False,
                "observation_ready": True,
                "trade_ready": False,
                "queue_depths": {},
            }],
            "unresolved_submit_unknown": 0,
        }
        probe = {"state": "RUNNING", "message": "", "response": {"ok": True, "data": health}}

        issues = _status_issues(probe)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            print_status_human("bridge.json", probe, issues)

        self.assertEqual(issues, [])
        self.assertIn("查询状态：可用", output.getvalue())
        self.assertIn("新的交易提交受保护", output.getvalue())

    def test_setup_auto_migrates_tracking_fields_without_changing_profile(self):
        with tempfile.TemporaryDirectory() as root:
            answers = iter(["", "n", "n", "n"])
            with contextlib.redirect_stdout(io.StringIO()):
                initial = run_setup(root, input_func=lambda _prompt: next(answers))
            adapter_path = os.path.join(initial["ready_dir"], "pythongo_adapter.json")
            profile_path = os.path.join(initial["ready_dir"], "pythongo_profile.json")
            with open(adapter_path, "r", encoding="utf-8") as stream:
                adapter = json.load(stream)
            adapter.pop("margin_reference_policy_generation")
            adapter.pop("margin_reference_policy_hash")
            with open(adapter_path, "w", encoding="utf-8") as stream:
                json.dump(adapter, stream, ensure_ascii=False)
            with open(profile_path, "rb") as stream:
                profile_before = stream.read()

            answers = iter(["", "n", "n", "n"])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                upgraded = run_setup(root, input_func=lambda _prompt: next(answers))

            migration = upgraded["margin_policy_migration"]
            self.assertTrue(migration["automatic"])
            self.assertTrue(migration["migrated"])
            self.assertFalse(migration["material_change"])
            self.assertFalse(migration["halted_for_policy_change"])
            self.assertIn("已自动补齐保证金策略跟踪字段", output.getvalue())
            with open(profile_path, "rb") as stream:
                self.assertEqual(stream.read(), profile_before)


if __name__ == "__main__":
    unittest.main()
