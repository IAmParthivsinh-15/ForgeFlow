"""Requirement Analyzer tools: read-only repository inspection."""

from __future__ import annotations

from agents import FunctionTool

from forgeflow.tools.filesystem.agent_tools import READ_TOOLS


def get_tools() -> list[FunctionTool]:
    return list(READ_TOOLS)
