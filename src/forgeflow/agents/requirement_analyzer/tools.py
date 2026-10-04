"""Requirement Analyzer tools: read-only repository inspection.

Thin wrappers over `forgeflow.tools.filesystem.repository`. The repository root
comes from the run context, never from the model.
"""

from __future__ import annotations

from typing import Any

from agents import FunctionTool, RunContextWrapper, function_tool

from forgeflow.agents.context import AgentRuntimeContext
from forgeflow.core.errors import PolicyViolation


def _record(ctx: RunContextWrapper[AgentRuntimeContext], tool: str, **args: Any) -> None:
    ctx.context.tool_calls.append({"tool": tool, "args": args})


@function_tool
def list_files(
    ctx: RunContextWrapper[AgentRuntimeContext], path: str = ".", max_depth: int = 3
) -> str:
    """List repository files under a directory.

    Args:
        path: Directory relative to the repository root. Use "." for the root.
        max_depth: How many directory levels to descend (1-8).
    """
    _record(ctx, "list_files", path=path, max_depth=max_depth)
    try:
        return "\n".join(ctx.context.sandbox().list_files(path, max_depth=max_depth)) or "(empty)"
    except (PolicyViolation, LookupError) as exc:
        return f"ERROR: {exc}"


@function_tool
def read_file(
    ctx: RunContextWrapper[AgentRuntimeContext],
    path: str,
    start_line: int = 1,
    max_lines: int = 400,
) -> str:
    """Read a text file from the repository with line numbers.

    Args:
        path: File path relative to the repository root.
        start_line: First line to return (1-based).
        max_lines: Maximum number of lines to return.
    """
    _record(ctx, "read_file", path=path, start_line=start_line)
    try:
        return ctx.context.sandbox().read_file(path, start_line=start_line, max_lines=max_lines)
    except (PolicyViolation, LookupError) as exc:
        return f"ERROR: {exc}"


@function_tool
def search_code(
    ctx: RunContextWrapper[AgentRuntimeContext],
    query: str,
    glob: str | None = None,
    is_regex: bool = False,
) -> str:
    """Search repository files for text (case-insensitive).

    Args:
        query: Text or regular expression to find.
        glob: Optional filename/path filter such as "*.py" or "backend/**".
        is_regex: Treat the query as a regular expression.
    """
    _record(ctx, "search_code", query=query, glob=glob)
    try:
        matches = ctx.context.sandbox().search_code(query, glob=glob, is_regex=is_regex)
    except (PolicyViolation, LookupError) as exc:
        return f"ERROR: {exc}"
    if not matches:
        return "No matches."
    return "\n".join(f"{m.path}:{m.line}: {m.text}" for m in matches)


def get_tools() -> list[FunctionTool]:
    return [list_files, read_file, search_code]
