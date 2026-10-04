from datetime import UTC, datetime

import pytest

from forgeflow.core.errors import InvalidStateTransition, InvalidTaskGraph
from forgeflow.platform.task_graph.conflicts import conflict_reason, scopes_conflict
from forgeflow.platform.task_graph.graph import (
    dependency_state,
    topological_order,
    validate_plan,
)
from forgeflow.platform.task_graph.state_machine import can_transition, ensure_transition
from forgeflow.schemas.task import SubtaskSpec, Task, TaskStatus
from forgeflow.tools.filesystem.globs import matches, patterns_may_overlap

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def task(key, status=TaskStatus.PENDING, deps=(), scope=(), kind="implement", **kw):
    return Task(
        task_id=f"wf.{key}",
        workflow_id="wf",
        key=key,
        title=key,
        kind=kind,
        agent_type="developer_subagent",
        status=status,
        dependencies=[f"wf.{d}" for d in deps],
        file_scope=list(scope),
        created_at=NOW,
        updated_at=NOW,
        **kw,
    )


# ------------------------------------------------------------------- globs


@pytest.mark.parametrize(
    "path,pattern,expected",
    [
        ("backend/auth/service.py", "backend/**", True),
        ("backend/auth/service.py", "backend/*.py", False),
        ("backend/app.py", "backend/*.py", True),
        ("README.md", "README.md", True),
        ("docs/a/b.md", "docs/", True),
        ("src/x.ts", "**/*.ts", True),
        ("x.ts", "**/*.ts", True),
        (".github/workflows/ci.yml", ".github/**", True),
        ("frontend/a.ts", "backend/**", False),
    ],
)
def test_glob_matching(path, pattern, expected):
    assert matches(path, pattern) is expected


# --------------------------------------------------------- conflict rules


def test_spec_section_95_examples():
    # Task A -> auth.py, Task B -> auth.py  =>  serialized
    assert scopes_conflict(["backend/auth.py"], ["backend/auth.py"])
    # Task A -> backend/**, Task B -> frontend/**  =>  parallel
    assert not scopes_conflict(["backend/**"], ["frontend/**"])


def test_spec_section_185_examples():
    assert not scopes_conflict(["backend/auth/**"], ["frontend/login/**"])
    assert not scopes_conflict(["backend/auth/**"], ["docs/auth.md"])
    assert scopes_conflict(["backend/auth/service.py"], ["backend/auth/**"])


@pytest.mark.parametrize(
    "a,b,overlap",
    [
        ("src/auth/**", "src/authz/**", False),
        ("src/auth*", "src/authz/x.py", True),
        ("*.md", "backend/**", True),  # uncertain -> conservative
        ("backend", "backend/x.py", True),
        ("docs/a.md", "docs/b.md", False),
    ],
)
def test_overlap_is_conservative(a, b, overlap):
    assert patterns_may_overlap(a, b) is overlap


def test_empty_scope_is_treated_as_conflicting():
    assert scopes_conflict([], ["x/**"])


def test_conflict_reason_only_between_implement_tasks():
    a = task("a", scope=["src/**"])
    b = task("b", scope=["src/x.py"])
    assert "overlaps" in conflict_reason(a, b)
    assert conflict_reason(a, task("i", kind="integrate")) is None
    shared = task("c", scope=["docs/**"], resource_scope=["database:schema"])
    other = task("d", scope=["api/**"], resource_scope=["database:schema"])
    assert "shared resource" in conflict_reason(shared, other)


# ------------------------------------------------------------ graph checks


def sub(key, deps=(), scope=("x/**",)):
    return SubtaskSpec(
        key=key, title=key, instructions="do", file_scope=list(scope), depends_on=list(deps)
    )


def test_validate_plan_orders_topologically():
    ordered = validate_plan([sub("docs", ["api", "ui"]), sub("ui", ["api"]), sub("api")], 6)
    assert [s.key for s in ordered] == ["api", "ui", "docs"]


@pytest.mark.parametrize(
    "plan,message",
    [
        ([sub("a"), sub("a")], "duplicate"),
        ([sub("a", ["missing"])], "unknown"),
        ([sub("a", ["a"])], "itself"),
        ([sub("a", ["b"]), sub("b", ["a"])], "cycle"),
        ([sub(str(i)) for i in range(3)], "limit"),
    ],
)
def test_validate_plan_rejects_malformed_graphs(plan, message):
    with pytest.raises(InvalidTaskGraph, match=message):
        validate_plan(plan, max_subtasks=2)


def test_topological_order_detects_cycles():
    with pytest.raises(InvalidTaskGraph):
        topological_order({"a": ["c"], "b": ["a"], "c": ["b"]})


def test_subtask_scope_is_normalised_and_rejects_parent_dirs():
    assert sub("a", scope=["./src/**", "/.github/**"]).file_scope == ["src/**", ".github/**"]
    with pytest.raises(ValueError):
        sub("a", scope=["../outside/**"])


def test_dependency_resolution_matches_spec_section_19():
    t1 = task("T1", TaskStatus.COMPLETED)
    t2 = task("T2", TaskStatus.COMPLETED)
    t3 = task("T3", TaskStatus.FAILED, attempt=2, max_attempts=2, retryable=True)
    t4 = task("T4", deps=["T1", "T2", "T3"])
    by_id = {t.task_id: t for t in (t1, t2, t3, t4)}
    assert dependency_state(t4, by_id) == "broken"  # T3 failed for good -> T4 blocked
    t3.status = TaskStatus.COMPLETED
    assert dependency_state(t4, by_id) == "satisfied"  # T3 retried and succeeded -> ready
    t3.status = TaskStatus.RUNNING
    assert dependency_state(t4, by_id) == "waiting"


def test_retryable_failure_is_not_terminal():
    assert not task(
        "x", TaskStatus.FAILED, attempt=1, max_attempts=2, retryable=True
    ).terminal_failure
    assert task("x", TaskStatus.FAILED, attempt=1, max_attempts=2).terminal_failure


# ------------------------------------------------------- task state machine


def test_task_transitions_follow_spec_section_17():
    path = [
        TaskStatus.PENDING,
        TaskStatus.READY,
        TaskStatus.DISPATCHED,
        TaskStatus.RUNNING,
        TaskStatus.FAILED,
        TaskStatus.RETRYING,
        TaskStatus.READY,
    ]
    for a, b in zip(path, path[1:], strict=False):
        assert can_transition(a, b)
    assert not can_transition(TaskStatus.PENDING, TaskStatus.RUNNING)
    with pytest.raises(InvalidStateTransition):
        ensure_transition(TaskStatus.COMPLETED, TaskStatus.READY)
