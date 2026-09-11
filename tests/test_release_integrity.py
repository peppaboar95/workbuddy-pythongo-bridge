import ast
import pathlib
import re
import tempfile
import unittest

import workbuddy_pythongo
from workbuddy_pythongo.bootstrap import initialize
from workbuddy_pythongo.errors import BridgeError
from workbuddy_pythongo.mcp_server import TOOLS
from workbuddy_pythongo.worker import build_runtime, make_handler


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class ReleaseIntegrityTests(unittest.TestCase):
    def test_project_and_runtime_versions_match(self):
        project = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version\s*=\s*"([^"]+)"\s*$', project, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), workbuddy_pythongo.__version__)

    def test_mcp_tool_names_are_unique(self):
        names = [item["name"] for item in TOOLS]
        self.assertEqual(len(names), 31)
        self.assertEqual(len(names), len(set(names)))

    def test_worker_server_header_uses_package_version(self):
        handler = make_handler(None, None, "x" * 32, 65536)
        self.assertEqual(handler.server_version, "WorkBuddyPythonGO/" + workbuddy_pythongo.__version__)

    def test_embedded_adapter_parses_as_python_3_6_compatible_syntax(self):
        adapter = REPO_ROOT / "src" / "workbuddy_pythongo" / "assets" / "pythongo_embedded_adapter.py"
        ast.parse(adapter.read_text(encoding="utf-8"), filename=str(adapter), feature_version=(3, 6))

    def test_p0_runtime_is_not_hardcoded_to_one_contract(self):
        core = (REPO_ROOT / "src" / "workbuddy_pythongo" / "core.py").read_text(encoding="utf-8")
        adapter = (
            REPO_ROOT / "src" / "workbuddy_pythongo" / "assets" / "pythongo_embedded_adapter.py"
        ).read_text(encoding="utf-8")
        for source in (core, adapter):
            self.assertNotIn("P0_ISOLATED_NOTIONAL_CAPS", source)
            self.assertNotIn('(command["exchange"], command["instrument_id"]) != ("SHFE", "au2610")', source)

        with tempfile.TemporaryDirectory() as root:
            initialized = initialize(root)
            _, _, bridge = build_runtime(initialized["config"])
            with self.assertRaises(BridgeError) as probe:
                bridge.queue_p0_test_order("main_futures", "DCE", "m2701", 500000)
            self.assertEqual(probe.exception.code, "ADAPTER_NOT_READY")
            with self.assertRaises(BridgeError) as leg:
                bridge.queue_p0_validation_leg("main_futures", "DCE", "m2701", "OPEN_LONG")
            self.assertEqual(leg.exception.code, "ADAPTER_NOT_READY")
            with self.assertRaises(BridgeError) as cap:
                bridge.queue_p0_test_order("main_futures", "DCE", "m2701", 1000000.01)
            self.assertEqual(cap.exception.code, "P0_ISOLATED_CAP_REJECTED")

    def test_public_docs_do_not_contain_build_machine_path(self):
        candidates = [REPO_ROOT / "README.md", *sorted((REPO_ROOT / "docs").glob("*.md"))]
        for path in candidates:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("F:\\workbuddy-pythongo-bridge", text, path.name)
            self.assertNotRegex(text, r"C:\\Users\\[0-9]+\\", path.name)


if __name__ == "__main__":
    unittest.main()
