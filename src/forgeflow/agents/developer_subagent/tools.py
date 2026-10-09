"""Developer subagent tools: read + scoped write in its own worktree, allowlisted checks.

Committing is done by ForgeFlow after the run, not by the agent.
"""

from __future__ import annotations

from agents import FunctionTool

from forgeflow.tools.filesystem.agent_tools import READ_TOOLS, WRITE_TOOLS
from forgeflow.tools.knowledge_tools import KNOWLEDGE_TOOLS
from forgeflow.tools.testing.agent_tools import run_check


def get_tools(with_checks: bool = True) -> list[FunctionTool]:
    return [
        *READ_TOOLS,
        *KNOWLEDGE_TOOLS,
        *WRITE_TOOLS,
        *([run_check] if with_checks else []),
    ]
