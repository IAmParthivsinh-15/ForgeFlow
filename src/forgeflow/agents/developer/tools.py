"""Developer agent tools: read-only inspection for planning.

The Developer never edits files. It requests subtasks; ForgeFlow creates them in
the Task Graph Engine and runs them in isolated worktrees (spec section 182).
"""

from __future__ import annotations

from agents import FunctionTool

from forgeflow.tools.filesystem.agent_tools import READ_TOOLS


def get_tools() -> list[FunctionTool]:
    return list(READ_TOOLS)
