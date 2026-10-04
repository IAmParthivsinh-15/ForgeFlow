from datetime import timedelta

import pytest

from forgeflow.core.errors import AllProvidersFailed, ValidationFailed
from forgeflow.core.ids import utcnow
from forgeflow.platform.orchestration.fake_gateway import FakeAgentGateway
from forgeflow.platform.orchestration.gateway import AgentOutcome
from forgeflow.platform.state.store import Commit, TaskWrite
from forgeflow.schemas.events import EventType
from forgeflow.schemas.task import DevelopmentPlan, SubtaskSpec, Task, TaskResult, TaskStatus
from forgeflow.schemas.workflow import WorkflowStatus
from tests.conftest import drive, git


async def planned_workflow(container, request="Add a status page"):
    """Create a workflow on repos/app and take it through analysis to PLANNED."""
    svc = container.service
    wf = await svc.create_workflow(request, "app")
    wf = await svc.process_analysis(wf.workflow_id, 1)
    if wf.status == WorkflowStatus.AWAITING_CLARIFICATION:
        [q] = await container.store.list_questions(wf.workflow_id)
        await svc.answer_question(q.question_id, "A", None)
        wf = await svc.process_analysis(wf.workflow_id, 2)
    assert wf.status == WorkflowStatus.PLANNED
    return wf


def tasks_by_key(tasks):
    return {t.key: t for t in tasks}


class PlanGateway(FakeAgentGateway):
    def __init__(self, subtasks, fail_times=None):
        self.subtasks = subtasks
        self.fail_times = dict(fail_times or {})

    async def plan_development(self, ctx, request):
        plan = DevelopmentPlan(summary="custom plan", subtasks=self.subtasks)
        return AgentOutcome(plan, "test", [])

    async def implement_subtask(self, ctx, request):
        key = request.task.key.split("-", 1)[1]
        if self.fail_times.get(key, 0) > 0:
            self.fail_times[key] -= 1
            raise AllProvidersFailed("provider down")
        return await super().implement_subtask(ctx, request)


def sub(key, scope, deps=()):
    return SubtaskSpec(
        key=key, title=key, instructions=key, file_scope=scope, depends_on=list(deps)
    )


# ----------------------------------------------------------------- happy path


async def test_full_development_flow(container, git_repo):
    wf = await planned_workflow(container, "Add a forgot-password flow")
    base = git(git_repo, "rev-parse", "HEAD")

    wf = await container.execution.start_execution(wf.workflow_id)
    assert wf.status == WorkflowStatus.EXECUTING
    assert wf.execution.base_commit == base and wf.execution.base_ref == "main"
    [t1] = await container.store.list_tasks(wf.workflow_id)
    assert t1.key == "T1" and t1.kind == "decompose" and t1.status == TaskStatus.DISPATCHED

    await drive(container, wf.workflow_id)

    wf = await container.store.get_workflow(wf.workflow_id)
    tasks = tasks_by_key(await container.store.list_tasks(wf.workflow_id))
    assert set(tasks) == {"T1", "T2-backend", "T3-frontend", "T4-docs", "T5-integration"}
    assert all(t.status == TaskStatus.COMPLETED for t in tasks.values())

    # Development finished; review/security/qa/ci are planned but not implemented yet.
    assert wf.status == WorkflowStatus.PAUSED
    assert "not implemented yet" in wf.execution.note
    dev = next(s for s in wf.route_plan.stages if s.capability == "development")
    assert dev.status == "completed"

    # The docs task depends on backend + frontend and was built on top of their code.
    assert sorted(tasks["T4-docs"].result.merged_tasks) == ["T2-backend", "T3-frontend"]

    # Integration branch holds every subtask's change; the user's branch is untouched.
    integration = tasks["T5-integration"].result
    assert wf.execution.integration_branch == "forgeflow/" + wf.workflow_id + "/integration"
    assert wf.execution.integration_commit == integration.commit
    assert sorted(integration.files_changed) == [
        "forgeflow-demo/CHANGES.md",
        "forgeflow-demo/backend/T2-backend.md",
        "forgeflow-demo/frontend/T3-frontend.md",
    ]
    assert git(git_repo, "rev-parse", "main") == base
    assert not (git_repo / "forgeflow-demo").exists()
    branches = git(git_repo, "branch", "--list", f"forgeflow/{wf.workflow_id}/*")
    assert "integration" in branches and "T2-backend" in branches

    # Every implement task has its own workspace record.
    workspaces = await container.store.list_workspaces(wf.workflow_id)
    assert {w.task_id.split(".")[-1] for w in workspaces} == {
        "T2-backend",
        "T3-frontend",
        "T4-docs",
        "T5-integration",
    }


async def test_independent_tasks_are_dispatched_in_parallel(container, git_repo):
    wf = await planned_workflow(container)
    await container.execution.start_execution(wf.workflow_id)
    executor = container.executor()
    [t1] = await container.store.list_tasks(wf.workflow_id)
    await executor.execute(t1.task_id, t1.attempt)

    dispatched = await container.execution.tick(wf.workflow_id)
    keys = sorted(d.split(".")[-1] for d in dispatched)
    assert keys == ["T2-backend", "T3-frontend"]  # docs waits for both
    docs = tasks_by_key(await container.store.list_tasks(wf.workflow_id))["T4-docs"]
    assert docs.status == TaskStatus.PENDING


async def test_overlapping_scopes_are_serialized(container, git_repo):
    container.gateway = PlanGateway([sub("a", ["src/**"]), sub("b", ["src/app.py"])])
    wf = await planned_workflow(container)
    await container.execution.start_execution(wf.workflow_id)
    executor = container.executor()
    [t1] = await container.store.list_tasks(wf.workflow_id)
    await executor.execute(t1.task_id, t1.attempt)

    dispatched = await container.execution.tick(wf.workflow_id)
    assert [d.split(".")[-1] for d in dispatched] == ["T2-a"]
    waiting = tasks_by_key(await container.store.list_tasks(wf.workflow_id))["T3-b"]
    assert waiting.status == TaskStatus.READY
    assert waiting.wait_reason.startswith("serialized: file scope overlaps")


async def test_parallelism_limit(container, git_repo, settings):
    settings.max_parallel_tasks = 1
    container.gateway = PlanGateway([sub("a", ["a/**"]), sub("b", ["b/**"])])
    wf = await planned_workflow(container)
    await container.execution.start_execution(wf.workflow_id)
    executor = container.executor()
    [t1] = await container.store.list_tasks(wf.workflow_id)
    await executor.execute(t1.task_id, t1.attempt)
    assert len(await container.execution.tick(wf.workflow_id)) == 1


# -------------------------------------------------------------- failure paths


async def run_with_gateway(container, gw):
    wf = await planned_workflow(container)
    await container.execution.start_execution(wf.workflow_id)
    container.gateway = gw  # used by executors created from here on
    await drive(container, wf.workflow_id)
    return await container.store.get_workflow(wf.workflow_id)


async def test_retryable_failure_is_retried_automatically(container, git_repo):
    gw = PlanGateway([sub("a", ["a/**"])], fail_times={"a": 1})
    wf = await run_with_gateway(container, gw)
    tasks = tasks_by_key(await container.store.list_tasks(wf.workflow_id))
    assert tasks["T2-a"].status == TaskStatus.COMPLETED
    assert tasks["T2-a"].attempt == 2
    events = [se.event.event_type for se in await container.store.list_events(wf.workflow_id)]
    assert EventType.TASK_RETRYING in events


async def test_exhausted_retries_pause_and_manual_retry_resumes(container, git_repo):
    gw = PlanGateway([sub("a", ["a/**"]), sub("b", ["b/**"], deps=["a"])], fail_times={"a": 2})
    wf = await run_with_gateway(container, gw)
    tasks = tasks_by_key(await container.store.list_tasks(wf.workflow_id))
    assert wf.status == WorkflowStatus.PAUSED
    assert "T2-a failed after 2 attempt(s)" in wf.error
    assert tasks["T2-a"].terminal_failure
    assert tasks["T3-b"].status == TaskStatus.BLOCKED

    await container.execution.retry_task(tasks["T2-a"].task_id)
    assert (await container.store.get_workflow(wf.workflow_id)).status == WorkflowStatus.EXECUTING
    await drive(container, wf.workflow_id)
    wf = await container.store.get_workflow(wf.workflow_id)
    tasks = tasks_by_key(await container.store.list_tasks(wf.workflow_id))
    assert all(t.status == TaskStatus.COMPLETED for t in tasks.values())
    assert wf.error is None and wf.execution.integration_commit


async def test_retry_rejects_non_failed_tasks(container, git_repo):
    wf = await planned_workflow(container)
    await container.execution.start_execution(wf.workflow_id)
    [t1] = await container.store.list_tasks(wf.workflow_id)
    with pytest.raises(ValidationFailed):
        await container.execution.retry_task(t1.task_id)


async def test_cancel_workflow_cancels_tasks_and_running_work_is_discarded(container, git_repo):
    wf = await planned_workflow(container)
    await container.execution.start_execution(wf.workflow_id)
    [t1] = await container.store.list_tasks(wf.workflow_id)
    await container.service.cancel_workflow(wf.workflow_id)
    [t1] = await container.store.list_tasks(wf.workflow_id)
    assert t1.status == TaskStatus.CANCELLED
    assert await container.executor().execute(t1.task_id, t1.attempt) is None


async def test_execution_requires_a_git_repository(container):
    svc = container.service
    wf = await svc.create_workflow("Add a status page", "demo-app")  # not a git repo
    wf = await svc.process_analysis(wf.workflow_id, 1)
    [q] = await container.store.list_questions(wf.workflow_id)
    await svc.answer_question(q.question_id, "A", None)
    wf = await svc.process_analysis(wf.workflow_id, 2)
    wf = await container.execution.start_execution(wf.workflow_id)
    assert wf.status == WorkflowStatus.PLANNED
    dev = next(s for s in wf.route_plan.stages if s.capability == "development")
    assert dev.status == "failed" and "not a git repository" in dev.reason


async def test_start_execution_is_idempotent(container, git_repo):
    wf = await planned_workflow(container)
    await container.execution.start_execution(wf.workflow_id)
    assert await container.execution.start_execution(wf.workflow_id) is None
    assert len(await container.store.list_tasks(wf.workflow_id)) == 1


# ------------------------------------------------------------ integration


async def test_integration_resolves_merge_conflicts(container, git_repo):
    """Two completed branches edit the same line; the integrator reconciles them."""
    wf = await planned_workflow(container)
    await container.execution.start_execution(wf.workflow_id)
    wf = await container.store.get_workflow(wf.workflow_id)
    base = wf.execution.base_commit
    commits = {}
    for key, text in (("T2-x", "one"), ("T3-y", "two")):
        prepared = await container.worktrees.create(
            workflow_id=wf.workflow_id,
            task_id=f"{wf.workflow_id}.{key}",
            key=key,
            repository_path="app",
            base_commit=base,
        )
        (prepared.path / "src" / "app.py").write_text(f"def handler():\n    return '{text}'\n")
        commits[key] = await container.git.commit_all(prepared.path, key)

    now = utcnow()
    [t1] = await container.store.list_tasks(wf.workflow_id)
    done = []
    for i, key in enumerate(commits):
        done.append(
            Task(
                task_id=f"{wf.workflow_id}.{key}",
                workflow_id=wf.workflow_id,
                key=key,
                title=key,
                kind="implement",
                agent_type="developer_subagent",
                status=TaskStatus.COMPLETED,
                file_scope=["src/**"],
                result=TaskResult(summary=key, commit=commits[key]),
                created_at=now,
                updated_at=now,
                completed_at=now + timedelta(seconds=i),
            )
        )
    integrate = Task(
        task_id=f"{wf.workflow_id}.T4-integration",
        workflow_id=wf.workflow_id,
        key="T4-integration",
        title="Integrate",
        kind="integrate",
        agent_type="integrator",
        status=TaskStatus.DISPATCHED,
        attempt=1,
        dependencies=[t.task_id for t in done],
        created_at=now,
        updated_at=now,
    )
    t1.status = TaskStatus.COMPLETED
    await container.store.commit(
        Commit(
            tasks=[TaskWrite(t1, t1.revision)] + [TaskWrite(t, None) for t in [*done, integrate]]
        )
    )

    status = await container.executor().execute(integrate.task_id, 1)
    assert status == TaskStatus.COMPLETED
    result = (await container.store.get_task(integrate.task_id)).result
    assert result.merged_tasks == ["T2-x", "T3-y"]
    assert result.conflicts_resolved == ["src/app.py"]
    merged = git(git_repo, "show", f"{result.commit}:src/app.py")
    assert "'one'" in merged and "'two'" in merged and "<<<<<<<" not in merged


# ------------------------------------------------------------------ reaper


async def test_reaper_fails_tasks_with_stale_heartbeats(container, git_repo, settings):
    wf = await planned_workflow(container)
    await container.execution.start_execution(wf.workflow_id)
    [t1] = await container.store.list_tasks(wf.workflow_id)
    t1.status = TaskStatus.RUNNING
    t1.worker_id = "dead-worker"
    t1.heartbeat_at = utcnow() - timedelta(seconds=settings.heartbeat_stale_seconds + 5)
    await container.store.commit(Commit(tasks=[TaskWrite(t1, t1.revision)]))

    to_tick = await container.execution.reap()
    assert to_tick == {wf.workflow_id}
    [t1] = await container.store.list_tasks(wf.workflow_id)
    assert t1.status == TaskStatus.FAILED and t1.error == "worker heartbeat lost"
    await container.execution.tick(wf.workflow_id)
    [t1] = await container.store.list_tasks(wf.workflow_id)
    assert t1.status == TaskStatus.DISPATCHED and t1.attempt == 2


async def test_reaper_redispatches_unclaimed_tasks(container, git_repo, settings):
    wf = await planned_workflow(container)
    await container.execution.start_execution(wf.workflow_id)
    [t1] = await container.store.list_tasks(wf.workflow_id)
    t1.updated_at = utcnow() - timedelta(seconds=settings.dispatch_timeout_seconds + 5)
    container.store.tasks[t1.task_id].updated_at = t1.updated_at
    await container.execution.reap()
    events = [se.event for se in await container.store.list_events(wf.workflow_id)]
    redispatches = [e for e in events if e.payload.get("redispatch")]
    assert len(redispatches) == 1 and redispatches[0].payload["attempt"] == 1
