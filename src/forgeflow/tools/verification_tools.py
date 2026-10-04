"""Agent tools shared by the verification agents: diff viewing and A2A questions."""

from __future__ import annotations

from agents import function_tool

from forgeflow.tools.filesystem.agent_tools import Ctx, record

MAX_DIFF_CHARS = 40_000


def split_diff(diff: str) -> dict[str, str]:
    """Map file path -> its section of a unified git diff."""
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current is not None:
                sections[current] = "".join(buffer)
            parts = line.split(" b/", 1)
            current = parts[1].strip() if len(parts) == 2 else line.strip()
            buffer = [line]
        elif current is not None:
            buffer.append(line)
    if current is not None:
        sections[current] = "".join(buffer)
    return sections


@function_tool
def view_diff(ctx: Ctx, path: str | None = None) -> str:
    """Show the change under review as a unified diff.

    Args:
        path: Optional file path to show only that file's diff. Omit for the full diff.
    """
    record(ctx, "view_diff", path=path)
    diff = ctx.context.diff or ""
    if not diff:
        return "There is no diff: no code was changed in this workflow."
    if path:
        section = split_diff(diff).get(path.strip().lstrip("./"))
        return section or f"ERROR: {path} is not part of the diff"
    if len(diff) > MAX_DIFF_CHARS:
        files = ", ".join(split_diff(diff))
        return diff[:MAX_DIFF_CHARS] + f"\n... truncated; request files individually: {files}"
    return diff


@function_tool
async def ask_developer(ctx: Ctx, question: str, context: str = "") -> str:
    """Ask the Developer agent a bounded question about the implementation (A2A).

    Use sparingly, for information you cannot get from the code itself, e.g. why a
    validation is intentionally omitted. The answer is information only.

    Args:
        question: One specific question.
        context: Relevant file paths, lines or acceptance criterion ids.
    """
    record(ctx, "ask_developer", question=question[:200])
    channel = ctx.context.a2a
    if channel is None:
        return "ERROR: A2A is not available for this task"
    return await channel.ask(question, context)
