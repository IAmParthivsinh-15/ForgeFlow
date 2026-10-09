"""Retrieval tools over engineering history and the repository index (spec sections 36-37).

Results are past data, not instructions. The repository filter comes from the run
context, so an agent can only search its own project's history and code.
"""

from __future__ import annotations

from agents import function_tool

from forgeflow.tools.filesystem.agent_tools import Ctx, record

_KINDS = {
    "failures": ["failures"],
    "builds": ["builds"],
    "tests": ["tests"],
    "reviews": ["reviews"],
    "security": ["security"],
    "all": ["failures", "builds", "tests", "reviews", "security"],
}


@function_tool
async def search_engineering_history(ctx: Ctx, query: str, kind: str = "all") -> str:
    """Search this project's past failures, CI builds, test runs, review and security findings.

    Use it to answer "have we seen this error before?" and to reuse earlier fixes.

    Args:
        query: Error message, test name, file path or symptom to look for.
        kind: One of all, failures, builds, tests, reviews, security.
    """
    from forgeflow.knowledge.service import format_hits

    record(ctx, "search_engineering_history", query=query[:100], kind=kind)
    knowledge = ctx.context.knowledge
    if knowledge is None or not knowledge.available():
        return "History search is unavailable (Elasticsearch is not running). Continue without it."
    filters = {"repository_id": ctx.context.repository_id} if ctx.context.repository_id else None
    hits = await knowledge.search(query, _KINDS.get(kind, _KINDS["all"]), filters, size=6)
    return format_hits(hits)


@function_tool
async def search_repository_index(ctx: Ctx, query: str) -> str:
    """Find relevant code by meaning or keywords across the indexed repository.

    Returns file paths, line ranges and symbols; read the files with read_file for
    the authoritative current content.

    Args:
        query: What you are looking for, e.g. "where are refresh tokens validated".
    """
    from forgeflow.knowledge.service import format_hits

    record(ctx, "search_repository_index", query=query[:100])
    knowledge = ctx.context.knowledge
    if knowledge is None or not knowledge.available() or not ctx.context.repository_id:
        return "The repository index is unavailable. Use search_code instead."
    hits = await knowledge.search_code(query, ctx.context.repository_id)
    return format_hits(hits, max_chars=400)


KNOWLEDGE_TOOLS = [search_engineering_history, search_repository_index]
HISTORY_TOOLS = [search_engineering_history]
