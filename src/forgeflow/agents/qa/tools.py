"""QA tools: read the code, run the repository's allowlisted checks, ask the Developer (A2A).

Browser verification through Playwright MCP is a later phase (spec section 28);
until then browser-only criteria are reported as UNCERTAIN.
"""

from __future__ import annotations

from agents import FunctionTool

from forgeflow.tools.filesystem.agent_tools import READ_TOOLS
from forgeflow.tools.testing.agent_tools import run_check
from forgeflow.tools.verification_tools import ask_developer, view_diff


def get_tools() -> list[FunctionTool]:
    return [*READ_TOOLS, view_diff, run_check, ask_developer]
