"""Skill reference tools - a skill-specific retrieval layer (spec section 231)."""

from __future__ import annotations

from agents import function_tool

from forgeflow.tools.filesystem.agent_tools import Ctx, record


@function_tool
def search_skill_reference(ctx: Ctx, query: str) -> str:
    """Search the reference files of the skills enabled for this task.

    Args:
        query: Words to look for (case-insensitive).
    """
    record(ctx, "search_skill_reference", query=query[:100])
    files = ctx.context.skill_files
    if not files:
        return "No skill references are available."
    terms = [t for t in query.lower().split() if t]
    hits = []
    for path, text in sorted(files.items()):
        for n, line in enumerate(text.splitlines(), 1):
            if all(t in line.lower() for t in terms):
                hits.append(f"{path}:{n}: {line.strip()[:200]}")
                if len(hits) >= 30:
                    break
    return "\n".join(hits) or "No matches."


@function_tool
def read_skill_reference(ctx: Ctx, path: str) -> str:
    """Read one skill reference file, e.g. 'react-standards/references/testing.md'.

    Args:
        path: '<skill-slug>/<file path>' as listed in the skill section of your instructions.
    """
    record(ctx, "read_skill_reference", path=path)
    text = ctx.context.skill_files.get(path.strip())
    if text is None:
        available = ", ".join(sorted(ctx.context.skill_files))
        return f"ERROR: unknown reference {path}; available: {available}"
    return text[:40_000]


SKILL_TOOLS = [search_skill_reference, read_skill_reference]
