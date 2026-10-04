"""Code Review tools: read the code, view the diff, ask the Developer (A2A). No writes."""

from __future__ import annotations

from agents import FunctionTool

from forgeflow.tools.filesystem.agent_tools import READ_TOOLS
from forgeflow.tools.verification_tools import ask_developer, view_diff


def get_tools() -> list[FunctionTool]:
    return [*READ_TOOLS, view_diff, ask_developer]
