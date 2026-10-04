"""Conflict analysis between tasks (spec sections 20, 185).

Two code-writing tasks conflict when their file scopes may overlap or they share a
resource. When in doubt the answer is "conflict": the scheduler serializes rather
than parallelizes.
"""

from __future__ import annotations

from forgeflow.schemas.task import Task
from forgeflow.tools.filesystem.globs import patterns_may_overlap


def scopes_conflict(a: list[str], b: list[str]) -> bool:
    if not a or not b:
        return True  # unknown scope: be conservative
    return any(patterns_may_overlap(x, y) for x in a for y in b)


def conflict_reason(a: Task, b: Task) -> str | None:
    """Why `a` and `b` must not run concurrently, or None if they may."""
    # Decompose and integrate tasks are ordered by dependencies, not by scope.
    if not (a.kind == b.kind == "implement"):
        return None
    shared = set(a.resource_scope) & set(b.resource_scope)
    if shared:
        return f"shared resource: {', '.join(sorted(shared))}"
    if scopes_conflict(a.file_scope, b.file_scope):
        return f"file scope overlaps with {b.key}"
    return None
