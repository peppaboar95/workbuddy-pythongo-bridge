import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

from workbuddy_pythongo.bootstrap import initialize
from workbuddy_pythongo.desktop import create_shortcuts


PROBE_MODULE = '''import json
import os
import sys

with open(os.environ["LAUNCH_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps({"module": __name__, "args": sys.argv[1:]}) + "\\n")
if "manager" in __file__:
    print("REFRESH_OUTPUT_MUST_BE_HIDDEN")
    print("刷新探针错误输出", file=sys.stderr)
    sys.exit(int(os.environ["REFRESH_EXIT"]))
print("启动探针中文：" + sys.argv[sys.argv.index("--config") + 1])
print("启动探针错误输出", file=sys.stderr)
sys.exit(7)
'''


@unittest.skipUnless(os.name == "nt", "Requires Windows CMD and PowerShell")
class DesktopLauncherTests(unittest.TestCase):
    def test_start_launcher_handles_lf_chinese_paths_and_refresh_exit_codes(self):
        with tempfile.TemporaryDirectory(prefix="中文 & ! % ' 桥接 ") as root:
            root = pathlib.Path(root)
            initialized = initialize(str(root / "运行 目录"))
            shortcuts = root / "桌面 入口"
            create_shortcuts(initialized["config"], str(shortcuts), python_executable=sys.executable)
            launcher = shortcuts / "启动PythonGO桥接.cmd"
            original = launcher.read_bytes()
            self.assertEqual({path.name for path in shortcuts.iterdir()}, {
                "启动PythonGO桥接.cmd", "查看PythonGO桥接状态.cmd",
            })
            probe_package = root / "probe" / "workbuddy_pythongo"
            probe_package.mkdir(parents=True)
            (probe_package / "__init__.py").write_text("", encoding="utf-8")
            for module in ("manager", "desktop"):
                (probe_package / (module + ".py")).write_text(PROBE_MODULE, encoding="utf-8")
            log = root / "calls.jsonl"
            environment = dict(os.environ, PYTHONUTF8="0", PYTHONIOENCODING="gbk")
            environment["PYTHONPATH"] = str(probe_package.parent)
            environment["LAUNCH_LOG"] = str(log)

            for newline in ("CRLF", "LF"):
                launcher.write_bytes(original if newline == "CRLF" else original.replace(b"\r\n", b"\n"))
                for codepage in (936, 437):
                    for refresh_exit in (0, 2, 3):
                        with self.subTest(newline=newline, codepage=codepage, refresh_exit=refresh_exit):
                            log.write_text("", encoding="utf-8")
                            environment["REFRESH_EXIT"] = str(refresh_exit)
                            result = subprocess.run(
                                'cmd.exe /d /v:off /s /c "chcp %d >nul & "%s""' % (codepage, launcher),
                                input=b"\r\n",
                                capture_output=True,
                                env=environment,
                                timeout=20,
                            )

                            output = result.stdout.decode("utf-8")
                            self.assertIn("Worker运行期间请保持此窗口打开", output)
                            self.assertNotIn("REFRESH_OUTPUT_MUST_BE_HIDDEN", output)
                            calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
                            self.assertEqual(calls[0]["args"], [
                                "--config", initialized["config"], "refresh-margin-reference", "--if-due",
                            ])
                            if refresh_exit == 3:
                                self.assertEqual(result.stderr, b"")
                                self.assertEqual(result.returncode, 3)
                                self.assertEqual(len(calls), 1)
                                self.assertIn("需要复核", output)
                                self.assertIn("MIGRATE-MARGIN-POLICY", output)
                            else:
                                self.assertEqual(result.stderr.decode("utf-8").strip(), "启动探针错误输出")
                                self.assertEqual(result.returncode, 7)
                                self.assertEqual(len(calls), 2)
                                self.assertEqual(calls[1]["args"], ["--config", initialized["config"], "start"])
                                self.assertIn("启动探针中文：" + initialized["config"], output)
                                self.assertIn("桥接服务未正常退出", output)
                                self.assertIn("“查看PythonGO桥接状态.cmd”", output)
                                if refresh_exit == 2:
                                    self.assertIn("保证金参考刷新失败", output)


if __name__ == "__main__":
    unittest.main()
