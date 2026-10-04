"""Security tools: read the code and diff, ask the Developer (A2A).

Deterministic scanners (Gitleaks, Bandit, Semgrep, dependency audits) are run by
ForgeFlow before the agent starts; their findings are part of the agent's input.
"""

from __future__ import annotations

from agents import FunctionTool

from forgeflow.tools.filesystem.agent_tools import READ_TOOLS
from forgeflow.tools.verification_tools import ask_developer, view_diff


def get_tools() -> list[FunctionTool]:
    return [*READ_TOOLS, view_diff, ask_developer]
