# Copyright (c) 2025 ByteDance Ltd. and/or its affiliates
# SPDX-License-Identifier: MIT

"""Tools module for Trae Agent."""

from trae_agent.tools.base import Tool, ToolCall, ToolExecutor, ToolResult
from trae_agent.tools.bash_tool import BashTool
from trae_agent.tools.ckg_tool import CKGTool
from trae_agent.tools.edit_tool import TextEditorTool
from trae_agent.tools.json_edit_tool import JSONEditTool
from trae_agent.tools.sequential_thinking_tool import SequentialThinkingTool
from trae_agent.tools.task_done_tool import TaskDoneTool

__all__ = [
    "Tool",
    "ToolResult",
    "ToolCall",
    "ToolExecutor",
    "BashTool",
    "TextEditorTool",
    "JSONEditTool",
    "SequentialThinkingTool",
    "TaskDoneTool",
    "CKGTool",
    "iter_enabled_builtin_tool_names",
]

tools_registry: dict[str, type[Tool]] = {
    "bash": BashTool,
    "str_replace_based_edit_tool": TextEditorTool,
    "json_edit_tool": JSONEditTool,
    "sequentialthinking": SequentialThinkingTool,
    "task_done": TaskDoneTool,
    "ckg": CKGTool,
}


def iter_enabled_builtin_tool_names(tools_blacklist: list[str]) -> list[str]:
    """Return built-in tool names to load: all registry keys minus ``tools_blacklist`` (order preserved)."""
    blacklist = set(tools_blacklist)
    unknown = blacklist - set(tools_registry.keys())
    if unknown:
        raise ValueError(
            f"Unknown tool name(s) in tools_blacklist: {sorted(unknown)}. "
            f"Valid names: {sorted(tools_registry.keys())}"
        )
    return [name for name in tools_registry if name not in blacklist]
