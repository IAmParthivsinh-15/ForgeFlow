"""Task Graph Engine primitives (spec sections 16, 19)."""

from __future__ import annotations

from collections.abc import Iterable

from forgeflow.core.errors import InvalidTaskGraph
from forgeflow.schemas.task import SubtaskSpec, Task, TaskStatus


def validate_plan(subtasks: list[SubtaskSpec], max_subtasks: int) -> list[SubtaskSpec]:
    """Reject malformed decompositions; return subtasks in a valid topological order."""
    if len(subtasks) > max_subtasks:
        raise InvalidTaskGraph(f"plan has {len(subtasks)} subtasks; the limit is {max_subtasks}")
    keys = [s.key for s in subtasks]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    if duplicates:
        raise InvalidTaskGraph(f"duplicate subtask keys: {', '.join(duplicates)}")
    known = set(keys)
    for s in subtasks:
        unknown = [d for d in s.depends_on if d not in known]
        if unknown:
            raise InvalidTaskGraph(f"subtask '{s.key}' depends on unknown key(s): {unknown}")
        if s.key in s.depends_on:
            raise InvalidTaskGraph(f"subtask '{s.key}' depends on itself")
    order = topological_order({s.key: list(s.depends_on) for s in subtasks})
    by_key = {s.key: s for s in subtasks}
    return [by_key[k] for k in order]


def topological_order(dependencies: dict[str, list[str]]) -> list[str]:
    """Kahn's algorithm; stable with respect to input order. Raises on cycles."""
    remaining = {node: set(deps) for node, deps in dependencies.items()}
    order: list[str] = []
    while remaining:
        ready = [n for n, deps in remaining.items() if not deps]
        if not ready:
            raise InvalidTaskGraph(f"dependency cycle among: {', '.join(sorted(remaining))}")
        for node in ready:
            order.append(node)
            del remaining[node]
        for deps in remaining.values():
            deps.difference_update(ready)
    return order


def ancestors(task: Task, by_id: dict[str, Task]) -> set[str]:
    seen: set[str] = set()
    stack = list(task.dependencies)
    while stack:
        current = stack.pop()
        if current in seen or current not in by_id:
            continue
        seen.add(current)
        stack.extend(by_id[current].dependencies)
    return seen


def dependency_state(task: Task, by_id: dict[str, Task]) -> str:
    """'satisfied', 'waiting', or 'broken' (a dependency failed terminally or was cancelled)."""
    deps = [by_id[d] for d in task.dependencies if d in by_id]
    if any(d.status == TaskStatus.CANCELLED or d.terminal_failure for d in deps):
        return "broken"
    if all(d.status == TaskStatus.COMPLETED for d in deps):
        return "satisfied"
    return "waiting"


def completed_in_order(tasks: Iterable[Task]) -> list[Task]:
    done = [t for t in tasks if t.status == TaskStatus.COMPLETED and t.completed_at is not None]
    return sorted(done, key=lambda t: t.completed_at)  # type: ignore[arg-type,return-value]
