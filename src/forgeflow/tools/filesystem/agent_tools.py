"""Agents SDK function-tool wrappers over the deterministic filesystem tools.

Paths and roots come from the run context, never from the model. Policy errors
are returned to the model as "ERROR: ..." so it can adjust.
"""

from __future__ import annotations

from typing import Any

from agents import RunContextWrapper, function_tool

from forgeflow.agents.context import AgentRuntimeContext
from forgeflow.core.errors import PolicyViolation

Ctx = RunContextWrapper[AgentRuntimeContext]


def record(ctx: Ctx, tool: str, **args: Any) -> None:
    ctx.context.tool_calls.append({"tool": tool, "args": args})


# --------------------------------------------------------------------- read


@function_tool
def list_files(ctx: Ctx, path: str = ".", max_depth: int = 3) -> str:
    """List repository files under a directory.

    Args:
        path: Directory relative to the repository root. Use "." for the root.
        max_depth: How many directory levels to descend (1-8).
    """
    record(ctx, "list_files", path=path, max_depth=max_depth)
    try:
        return "\n".join(ctx.context.sandbox().list_files(path, max_depth=max_depth)) or "(empty)"
    except (PolicyViolation, LookupError) as exc:
        return f"ERROR: {exc}"


@function_tool
def read_file(ctx: Ctx, path: str, start_line: int = 1, max_lines: int = 400) -> str:
    """Read a text file from the repository with line numbers.

    Args:
        path: File path relative to the repository root.
        start_line: First line to return (1-based).
        max_lines: Maximum number of lines to return.
    """
    record(ctx, "read_file", path=path, start_line=start_line)
    try:
        return ctx.context.sandbox().read_file(path, start_line=start_line, max_lines=max_lines)
    except (PolicyViolation, LookupError) as exc:
        return f"ERROR: {exc}"


@function_tool
def search_code(ctx: Ctx, query: str, glob: str | None = None, is_regex: bool = False) -> str:
    """Search repository files for text (case-insensitive).

    Args:
        query: Text or regular expression to find.
        glob: Optional filename/path filter such as "*.py" or "backend/**".
        is_regex: Treat the query as a regular expression.
    """
    record(ctx, "search_code", query=query, glob=glob)
    try:
        matches = ctx.context.sandbox().search_code(query, glob=glob, is_regex=is_regex)
    except (PolicyViolation, LookupError) as exc:
        return f"ERROR: {exc}"
    if not matches:
        return "No matches."
    return "\n".join(f"{m.path}:{m.line}: {m.text}" for m in matches)


READ_TOOLS = [list_files, read_file, search_code]


# -------------------------------------------------------------------- write


@function_tool
def write_file(ctx: Ctx, path: str, content: str) -> str:
    """Create or overwrite a file with the full content. Only paths inside your file scope.

    Args:
        path: File path relative to the repository root.
        content: Complete new file content.
    """
    record(ctx, "write_file", path=path, bytes=len(content))
    try:
        return ctx.context.workspace().write_file(path, content)
    except (PolicyViolation, LookupError) as exc:
        return f"ERROR: {exc}"


@function_tool
def replace_in_file(ctx: Ctx, path: str, old: str, new: str, count: int = 1) -> str:
    """Replace exact text in a file. `old` must match the file exactly, including whitespace.

    Args:
        path: File path relative to the repository root.
        old: Exact existing text to replace. Include enough context to be unique.
        new: Replacement text.
        count: Expected number of occurrences (0 = replace all).
    """
    record(ctx, "replace_in_file", path=path)
    try:
        return ctx.context.workspace().replace_in_file(path, old, new, count)
    except (PolicyViolation, LookupError) as exc:
        return f"ERROR: {exc}"


@function_tool
def delete_file(ctx: Ctx, path: str) -> str:
    """Delete a file inside your file scope.

    Args:
        path: File path relative to the repository root.
    """
    record(ctx, "delete_file", path=path)
    try:
        return ctx.context.workspace().delete_file(path)
    except (PolicyViolation, LookupError) as exc:
        return f"ERROR: {exc}"


WRITE_TOOLS = [write_file, replace_in_file, delete_file]
