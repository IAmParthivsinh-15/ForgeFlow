"""Agent tool for running allowlisted repository checks."""

from __future__ import annotations

from agents import function_tool

from forgeflow.core.errors import PolicyViolation
from forgeflow.tools.filesystem.agent_tools import Ctx, record


@function_tool
async def run_check(ctx: Ctx, kind: str) -> str:
    """Run the repository's own command for a check kind in your workspace.

    Results are recorded by ForgeFlow; never claim a check passed without running it.

    Args:
        kind: One of "test", "lint", "typecheck", "build".
    """
    record(ctx, "run_check", kind=kind)
    runner = ctx.context.checks
    if runner is None:
        return "ERROR: checks are not available for this task"
    try:
        runs = await runner.run(kind)
    except PolicyViolation as exc:
        return f"ERROR: {exc}"
    if not runs:
        available = ", ".join(k for k in runner.available() if k != "setup") or "none"
        return f"No '{kind}' command is configured for this repository (available: {available})."
    lines = []
    for r in runs:
        status = "TIMED OUT" if r.timed_out else ("PASSED" if r.passed else "FAILED")
        lines.append(
            f"$ {r.command}\n{status} (exit {r.exit_code}, {r.duration_ms} ms)\n{r.output[-4000:]}"
        )
    return "\n\n".join(lines)
