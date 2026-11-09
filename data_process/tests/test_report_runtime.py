"""Report-tool compatibility tests with a stubbed base class, no models/APIs."""
import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parents[1]


class ReportRuntimeTests(unittest.TestCase):
    def setUp(self):
        repo = Path(os.environ.get("MEDENV_TEST_REPO_ROOT", SCRIPT_DIR.parent))
        target = repo / "rllm/tools/med_tool/cxr_tool.py"
        if not target.is_file():
            target = SCRIPT_DIR.parent / "runtime_reference/cxr_tool.py"
        if not target.is_file():
            self.skipTest("Surrounding MedEnv report tool not present")
        base = types.ModuleType("rllm.tools.tool_base")
        class Tool:
            def __init__(self, name, description):
                self.name, self.description = name, description
        class ToolOutput:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)
        base.Tool, base.ToolOutput = Tool, ToolOutput
        spec = importlib.util.spec_from_file_location("report_tool_fixture", target)
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"rllm.tools.tool_base": base}):
            spec.loader.exec_module(self.module)

    def test_reports_need_no_dicoms_or_image_files(self):
        with patch.dict(os.environ, {"MEDENV_CXR_MODE": "reports"}):
            tool = self.module.CXRTool()
            with patch.object(self.module, "_read_file_b64", side_effect=AssertionError("image access forbidden")):
                result = tool.forward(context={"ehr": {"CXR": [{"report_text": "Invented report."}]}})
            self.assertEqual(result.output["report_text"], "Invented report.")
            self.assertNotIn("images", result.output)
            self.assertNotIn("return_base64", tool.json["function"]["parameters"]["properties"])

    def test_legacy_image_paths_ignored_in_report_mode(self):
        with patch.dict(os.environ, {"MEDENV_CXR_MODE": "reports"}):
            tool = self.module.CXRTool()
            study = {"report_text": ["Invented", "report"], "dicoms": [{"jpg_path": "missing.jpg"}]}
            with patch.object(self.module, "_read_file_b64", side_effect=AssertionError("image access forbidden")):
                result = tool.forward(context={"ehr": {"CXR": [study]}}, return_base64=True)
            self.assertEqual(result.output["report_text"], "Invented\nreport")
            self.assertNotIn("missing.jpg", str(result.output))

    def test_missing_report_is_explicit(self):
        with patch.dict(os.environ, {"MEDENV_CXR_MODE": "reports"}):
            result = self.module.CXRTool().forward(context={"ehr": {"CXR": [{"report_text": ""}]}})
            self.assertEqual(result.error, "missing_cxr_report")

    def test_vision_branch_still_requires_dicoms(self):
        with patch.dict(os.environ, {"MEDENV_CXR_MODE": "vision"}):
            tool = self.module.CXRTool()
            result = tool.forward(context={"ehr": {"CXR": [{"report_text": "Invented report."}]}})
            self.assertEqual(result.error, "missing_dicoms")
            self.assertIn("return_base64", tool.json["function"]["parameters"]["properties"])


if __name__ == "__main__":
    unittest.main()
