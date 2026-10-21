# Adapted for MedEnv (2026): portable benchmark-only packaging; see NOTICE and docs/CHANGES.md.
from .registry import ToolRegistry
from .med_tool import DialogTool, DiagnosisTool, RetrieveTool, CXRTool, ExamResultTool, CXRGroundingTool, FinishTool
DEFAULT_TOOLS = {
    "ask_patient": DialogTool, "diagnosis": DiagnosisTool, "retrieve": RetrieveTool,
    "cxr": CXRTool, "request_exam": ExamResultTool, "cxr_grounding": CXRGroundingTool,
    "finish": FinishTool,
}
tool_registry = ToolRegistry()
tool_registry.register_all(DEFAULT_TOOLS)
