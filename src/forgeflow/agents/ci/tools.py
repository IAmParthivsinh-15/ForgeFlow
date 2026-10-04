"""CI agent tools: read the repository to relate a failed build log to the code.

Pipeline operations (render, validate, create/update job, trigger, wait, fetch
logs) are typed, deterministic ForgeFlow operations; the agent only diagnoses.
"""

from __future__ import annotations

from agents import FunctionTool

from forgeflow.tools.filesystem.agent_tools import READ_TOOLS


def get_tools() -> list[FunctionTool]:
    return list(READ_TOOLS)
