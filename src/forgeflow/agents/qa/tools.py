"""QA tools: read the code, run the repository's allowlisted checks, ask the Developer (A2A).

Browser tools are not listed here: Playwright MCP tools reach the QA agent through
the Extensibility Gateway when a project enables them (spec sections 28, 266A).
"""

from __future__ import annotations

from agents import FunctionTool

from forgeflow.tools.filesystem.agent_tools import READ_TOOLS
from forgeflow.tools.testing.agent_tools import run_check
from forgeflow.tools.verification_tools import ask_developer, view_diff


def get_tools() -> list[FunctionTool]:
    return [*READ_TOOLS, view_diff, run_check, ask_developer]
