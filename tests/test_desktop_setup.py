import contextlib
import io
import json
import os
import pathlib
import tempfile
import unittest

from workbuddy_pythongo.bootstrap import initialize
from workbuddy_pythongo.console import get_non_observe_mode_blockers
from workbuddy_pythongo.desktop import (
    _status_issues,
    create_shortcuts,
    deploy_adapter_files,
    discover_mcp_config_paths,
    discover_runtime_root,
    print_doctor_human,
    print_status_human,
    run_setup,
)


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class DesktopSetupTests(unittest.TestCase):
    def test_setup_accepts_plaintext_account_input(self):
        with tempfile.TemporaryDirectory() as root:
            answers = iter(["", "n", "", "TEST-ACCOUNT-001", "", "n"])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = run_setup(
                    root,
                    input_func=lambda _prompt: next(answers),
                    pointer_path=os.path.join(root, "runtime.path"),
                    strategy_candidates=[],
                )

            text = output.getvalue()
            self.assertIn("不是密码", text)
            self.assertIn("明文显示", text)
            self.assertIn("都不是查询前置步骤", text)
            adapter_path = os.path.join(result["ready_dir"], "pythongo_adapter.json")
            with open(adapter_path, "r", encoding="utf-8") as stream:
                adapter = json.load(stream)
            self.assertEqual(adapter["investor_id"], "TEST-ACCOUNT-001")
            self.assertFalse(result["binding"]["halted"])
            self.assertEqual(result["binding"]["trade_protection_kind"], "SETUP_LOCK")
            self.assertIn(
                "TRADE_PROTECTION_ACTIVE",
                {item["code"] for item in get_non_observe_mode_blockers(result["config"])},
            )
            local_halt = os.path.join(
                root, "data", "pythongo_runtime", "pythongo_futures_01", "local_halt.json"
            )
            with open(local_halt, "r", encoding="utf-8") as stream:
                self.assertTrue(json.load(stream)["payload"]["halted"])

    def test_shortcuts_pin_the_selected_python_executable(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as shortcuts:
            answers = iter(["", "n", "n", "", "n"])
            with contextlib.redirect_stdout(io.StringIO()):
                result = run_setup(
                    root,
                    input_func=lambda _prompt: next(answers),
                    pointer_path=os.path.join(root, "runtime.path"),
                    strategy_candidates=[],
                )
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
        self.assertIn("%LOCALAPPDATA%\\WorkBuddyPythonGO\\runtime", installer)
        self.assertIn("--discover-existing --legacy-root", installer)
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
                "kind": "INCIDENT_HALT",
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

    def test_status_and_doctor_print_actionable_chinese_next_steps(self):
        stopped = {"state": "STOPPED", "message": "Worker未启动", "response": None}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            print_status_human("bridge.json", stopped, _status_issues(stopped))
            print_doctor_human({
                "errors": 0,
                "warnings": 1,
                "checks": [{
                    "name": "main_futures:heartbeat",
                    "ok": False,
                    "severity": "warning",
                    "detail": "adapter=OFFLINE",
                }],
            })
        text = output.getvalue()
        self.assertIn("双击“启动PythonGO桥接.cmd”", text)
        self.assertIn("无限易Adapter连接", text)
        self.assertIn("建议操作", text)
        self.assertIn("启动WorkBuddyPythonGO策略", text)

    def test_setup_auto_migrates_tracking_fields_without_changing_profile(self):
        with tempfile.TemporaryDirectory() as root:
            pointer_path = os.path.join(root, "runtime.path")
            answers = iter(["", "n", "n", "", "n"])
            with contextlib.redirect_stdout(io.StringIO()):
                initial = run_setup(
                    root, input_func=lambda _prompt: next(answers),
                    pointer_path=pointer_path, strategy_candidates=[],
                )
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

            answers = iter(["", "n", "n", "", "n"])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                upgraded = run_setup(
                    root, input_func=lambda _prompt: next(answers),
                    pointer_path=pointer_path, strategy_candidates=[],
                )

            migration = upgraded["margin_policy_migration"]
            self.assertTrue(migration["automatic"])
            self.assertTrue(migration["migrated"])
            self.assertFalse(migration["material_change"])
            self.assertFalse(migration["halted_for_policy_change"])
            self.assertIn("已自动补齐保证金策略跟踪字段", output.getvalue())
            with open(profile_path, "rb") as stream:
                self.assertEqual(stream.read(), profile_before)

    def test_runtime_discovery_prefers_saved_then_legacy_runtime(self):
        with tempfile.TemporaryDirectory() as root:
            saved = os.path.join(root, "保存 目录")
            legacy = os.path.join(root, "旧安装包", "runtime")
            default = os.path.join(root, "default")
            os.makedirs(os.path.dirname(legacy), exist_ok=True)
            initialize(saved)
            initialize(legacy)
            pointer = os.path.join(root, "runtime.path")
            pathlib.Path(pointer).write_text(saved + "\n", encoding="utf-8")

            result = discover_runtime_root(default, legacy, pointer)
            self.assertEqual(result["source"], "saved")
            self.assertEqual(result["path"], os.path.abspath(saved))

            pathlib.Path(pointer).write_text(os.path.join(root, "不存在") + "\n", encoding="utf-8")
            result = discover_runtime_root(default, legacy, pointer)
            self.assertEqual(result["source"], "legacy")
            self.assertEqual(result["path"], os.path.abspath(legacy))

    def test_adapter_deployment_backs_up_exact_files_and_retires_legacy_json(self):
        with tempfile.TemporaryDirectory(prefix="安装 包 ") as root:
            initialized = initialize(os.path.join(root, "稳定 runtime"))
            strategy = os.path.join(root, "无限易 中文", "pyStrategy", "self_strategy")
            os.makedirs(strategy)
            current = pathlib.Path(strategy, "WorkBuddyPythonGOAdapter.py")
            legacy = pathlib.Path(strategy, "pythongo_adapter.json")
            current.write_text("old adapter", encoding="utf-8")
            legacy.write_text("old json", encoding="utf-8")

            result = deploy_adapter_files(initialized["ready_dir"], strategy)

            self.assertEqual(result["directory"], os.path.abspath(strategy))
            self.assertEqual(len(result["files"]), 2)
            self.assertTrue(result["files"][0]["backup"])
            self.assertFalse(legacy.exists())
            self.assertTrue(result["retired"][0]["backup"].startswith(str(legacy) + ".bak."))

    def test_mcp_discovery_honors_override_and_common_locations(self):
        with tempfile.TemporaryDirectory() as root:
            override = os.path.join(root, "自定义", "mcp.json")
            common = os.path.join(root, ".workbuddy", "mcp.json")
            os.makedirs(os.path.dirname(override))
            os.makedirs(os.path.dirname(common))
            pathlib.Path(override).write_text("{}", encoding="utf-8")
            pathlib.Path(common).write_text("{}", encoding="utf-8")

            result = discover_mcp_config_paths(
                {"WORKBUDDY_MCP_CONFIG": override}, home=root,
            )

            self.assertEqual(result, [os.path.abspath(override), os.path.abspath(common)])


if __name__ == "__main__":
    unittest.main()
