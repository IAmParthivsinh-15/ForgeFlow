"""CI agent tools: read the repository to relate a failed build log to the code.

Pipeline operations (render, validate, create/update job, trigger, wait, fetch
logs) are typed, deterministic ForgeFlow operations; the agent only diagnoses.
"""

from __future__ import annotations

from agents import FunctionTool

from forgeflow.tools.filesystem.agent_tools import READ_TOOLS
from forgeflow.tools.knowledge_tools import HISTORY_TOOLS


def get_tools() -> list[FunctionTool]:
    return [*READ_TOOLS, *HISTORY_TOOLS]
