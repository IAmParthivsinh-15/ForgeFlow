"""Integrator tools: read the integration worktree; write only the conflicted files.

ForgeFlow scopes the writable paths to exactly the files git reported as
conflicted, verifies no conflict markers remain, and concludes the merge itself.
"""

from __future__ import annotations

from agents import FunctionTool

from forgeflow.tools.filesystem.agent_tools import READ_TOOLS, write_file


def get_tools() -> list[FunctionTool]:
    return [*READ_TOOLS, write_file]
