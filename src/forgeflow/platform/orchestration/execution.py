"""Execution orchestration: the Task Graph Engine's scheduler (spec sections 16-20, 46, 48-49).

The orchestrator never runs code itself. It creates logical tasks, decides which
are ready, serializes conflicting ones, and dispatches the rest by emitting
`task.dispatched` events. Agent workers execute them (platform/execution).

    workflow.routed (development required)
        -> PLANNED -> EXECUTING, decompose task T1
    tick():  FAILED(retryable) -> RETRYING -> READY (after backoff)
             PENDING -> READY when dependencies COMPLETED; -> BLOCKED if one is broken
             READY -> DISPATCHED (respecting conflicts and the parallelism limit)
             integrate task active -> INTEGRATING
             all tasks COMPLETED -> execution finished
             nothing can progress -> PAUSED (retry or cancel)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from forgeflow.core.config import Settings
from forgeflow.core.errors import ConcurrencyConflict, GitError, ValidationFailed
from forgeflow.core.ids import utcnow
from forgeflow.core.logging import bind_context, log_event
from forgeflow.platform.orchestration.state_machine import ensure_transition as ensure_wf
from forgeflow.platform.state.store import Commit, TaskWrite, WorkflowStore
from forgeflow.platform.task_graph.conflicts import conflict_reason
from forgeflow.platform.task_graph.graph import dependency_state
from forgeflow.platform.task_graph.state_machine import ACTIVE, ensure_transition
from forgeflow.platform.worktrees.manager import WorktreeManager
from forgeflow.schemas.events import Event, EventType, Topics
from forgeflow.schemas.task import Task, TaskStatus
from forgeflow.schemas.workflow import ExecutionInfo, Workflow, WorkflowStatus
from forgeflow.tools.git.client import GitClient

logger = logging.getLogger(__name__)

EXECUTING_STATES = (WorkflowStatus.EXECUTING, WorkflowStatus.INTEGRATING)
TICK_RETRIES = 5


def task_event(task: Task, event_type: str, **payload: Any) -> Event:
    return Event(
        event_type=event_type,
        topic=Topics.TASK,
        workflow_id=task.workflow_id,
        task_id=task.task_id,
        payload={"key": task.key, "status": str(task.status), **payload},
    )


def workflow_event(wf: Workflow, event_type: str, **payload: Any) -> Event:
    return Event(event_type=event_type, workflow_id=wf.workflow_id, payload=payload)


class TaskChanges:
    """Collects task mutations within one unit of work, remembering loaded revisions."""

    def __init__(self) -> None:
        self.writes: dict[str, TaskWrite] = {}
        self.events: list[Event] = []

    def touch(self, task: Task, now: datetime) -> None:
        if task.task_id not in self.writes:
            self.writes[task.task_id] = TaskWrite(task, task.revision)
        task.updated_at = now

    def insert(self, task: Task) -> None:
        self.writes[task.task_id] = TaskWrite(task, None)

    def move(
        self, task: Task, target: TaskStatus, event_type: str, now: datetime, **payload
    ) -> None:
        ensure_transition(task.status, target)
        self.touch(task, now)
        task.status = target
        self.events.append(task_event(task, event_type, **payload))

    def task_writes(self) -> list[TaskWrite]:
        return list(self.writes.values())


class ExecutionService:
    def __init__(
        self,
        store: WorkflowStore,
        git: GitClient,
        worktrees: WorktreeManager,
        settings: Settings,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.store = store
        self.git = git
        self.worktrees = worktrees
        self.settings = settings
        self.clock = clock

    # ------------------------------------------------------------------ start

    async def start_execution(self, workflow_id: str) -> Workflow | None:
        """Begin development for a PLANNED workflow whose route includes it. Idempotent."""
        bind_context(workflow_id=workflow_id)
        wf = await self.store.get_workflow(workflow_id)
        if wf.status != WorkflowStatus.PLANNED or wf.execution is not None or wf.route_plan is None:
            return None
        stage = next((s for s in wf.route_plan.stages if s.capability == "development"), None)
        if stage is None:
            return None
        revision = wf.revision
        now = self.clock()

        blocker = await self._repository_blocker(wf)
        if blocker is not None:
            stage.status = "failed"
            stage.reason = blocker
            event = workflow_event(
                wf, EventType.WORKFLOW_EXECUTION_FINISHED, outcome="not_started", reason=blocker
            )
            wf.updated_at = now
            result = await self.store.commit(
                Commit(workflow=wf, expected_revision=revision, events=[event])
            )
            return result.workflow

        sha, ref = await self.git.head(self.worktrees.repository(wf.repository_path or ""))
        wf.execution = ExecutionInfo(base_commit=sha, base_ref=ref, started_at=now)
        stage.status = "running"
        events = [
            workflow_event(wf, EventType.WORKFLOW_EXECUTION_STARTED, base_commit=sha, base_ref=ref)
        ]
        self._move_workflow(wf, WorkflowStatus.EXECUTING, events, now)

        changes = TaskChanges()
        decompose = Task(
            task_id=f"{wf.workflow_id}.T1",
            workflow_id=wf.workflow_id,
            key="T1",
            title="Plan implementation subtasks",
            kind="decompose",
            agent_type="developer",
            instructions="Decompose the approved requirement specification into subtasks.",
            status=TaskStatus.PENDING,
            max_attempts=self.settings.task_max_attempts,
            priority=100,
            created_at=now,
            updated_at=now,
        )
        changes.insert(decompose)
        changes.events.append(task_event(decompose, EventType.TASK_CREATED, kind="decompose"))
        await self.store.commit(
            Commit(
                workflow=wf,
                expected_revision=revision,
                tasks=changes.task_writes(),
                events=events + changes.events,
            )
        )
        log_event(logger, "execution started", base_commit=sha)
        await self.tick(workflow_id)
        return await self.store.get_workflow(workflow_id)

    async def _repository_blocker(self, wf: Workflow) -> str | None:
        if not wf.repository_path:
            return "Development requires a repository; none is attached to this workflow."
        try:
            repo = self.worktrees.repository(wf.repository_path)
        except ValidationFailed as exc:
            return str(exc)
        if not await self.git.is_repository(repo):
            return f"'{wf.repository_path}' is not a git repository."
        try:
            await self.git.head(repo)
        except GitError:
            return f"'{wf.repository_path}' has no commits yet."
        return None

    # ------------------------------------------------------------------- tick

    async def tick(self, workflow_id: str) -> list[str]:
        """Advance the task graph. Returns the ids of tasks dispatched by this tick."""
        for _ in range(TICK_RETRIES):
            try:
                return await self._tick_once(workflow_id)
            except ConcurrencyConflict:
                continue  # a worker updated a task meanwhile; reload and re-evaluate
        log_event(
            logger,
            "tick gave up after repeated conflicts",
            logging.WARNING,
            workflow_id=workflow_id,
        )
        return []

    async def _tick_once(self, workflow_id: str) -> list[str]:
        wf = await self.store.get_workflow(workflow_id)
        if wf.status not in EXECUTING_STATES:
            return []
        tasks = await self.store.list_tasks(workflow_id)
        by_id = {t.task_id: t for t in tasks}
        now = self.clock()
        changes = TaskChanges()
        wf_events: list[Event] = []
        wf_revision = wf.revision
        wf_changed = False

        # 1. Retry policy (spec section 49).
        for t in tasks:
            if t.status == TaskStatus.FAILED and not t.terminal_failure:
                backoff = timedelta(seconds=self.settings.task_retry_backoff_seconds * t.attempt)
                t.not_before = now + backoff
                changes.move(
                    t,
                    TaskStatus.RETRYING,
                    EventType.TASK_RETRYING,
                    now,
                    attempt=t.attempt,
                    not_before=t.not_before.isoformat(),
                )
        for t in tasks:
            if t.status == TaskStatus.RETRYING and (t.not_before is None or t.not_before <= now):
                t.not_before = None
                changes.move(t, TaskStatus.READY, EventType.TASK_READY, now, retry=True)

        # 2. Dependency resolution (spec section 19).
        for t in tasks:
            state = dependency_state(t, by_id)
            if t.status in (TaskStatus.PENDING, TaskStatus.READY) and state == "broken":
                changes.move(
                    t,
                    TaskStatus.BLOCKED,
                    EventType.TASK_BLOCKED,
                    now,
                    reason="a dependency failed or was cancelled",
                )
            elif t.status == TaskStatus.BLOCKED and state != "broken":
                changes.move(t, TaskStatus.PENDING, EventType.TASK_UNBLOCKED, now)
            if t.status == TaskStatus.PENDING and dependency_state(t, by_id) == "satisfied":
                changes.move(t, TaskStatus.READY, EventType.TASK_READY, now)

        # 3. Conflict-aware dispatch (spec sections 20, 113, 185).
        active = [t for t in tasks if t.status in ACTIVE]
        dispatched: list[str] = []
        ready = sorted(
            (t for t in tasks if t.status == TaskStatus.READY),
            key=lambda t: (-t.priority, t.created_at),
        )
        for t in ready:
            if len(active) >= self.settings.max_parallel_tasks:
                self._set_wait(t, "waiting for a free execution slot", changes, now)
                continue
            reason = next((r for a in active if (r := conflict_reason(t, a))), None)
            if reason is not None:
                self._set_wait(t, f"serialized: {reason}", changes, now)
                continue
            t.attempt += 1
            t.wait_reason = None
            t.dispatched_at = now
            t.worker_id = None
            changes.move(
                t,
                TaskStatus.DISPATCHED,
                EventType.TASK_DISPATCHED,
                now,
                attempt=t.attempt,
                idempotency_key=f"{workflow_id}:{t.task_id}:dispatch:{t.attempt}",
            )
            active.append(t)
            dispatched.append(t.task_id)

        # 4. Workflow progression.
        integrate = next((t for t in tasks if t.kind == "integrate"), None)
        if (
            wf.status == WorkflowStatus.EXECUTING
            and integrate is not None
            and integrate.status in ACTIVE
        ):
            self._move_workflow(wf, WorkflowStatus.INTEGRATING, wf_events, now)
            wf_changed = True

        progressing = {
            TaskStatus.READY,
            TaskStatus.DISPATCHED,
            TaskStatus.RUNNING,
            TaskStatus.RETRYING,
        }
        if tasks and all(t.status == TaskStatus.COMPLETED for t in tasks):
            self._finish(wf, integrate, wf_events, now)
            wf_changed = True
        elif tasks and not any(t.status in progressing for t in tasks):
            self._pause_stuck(wf, tasks, wf_events, now)
            wf_changed = True

        if changes.writes or wf_changed:
            await self.store.commit(
                Commit(
                    workflow=wf if wf_changed else None,
                    expected_revision=wf_revision,
                    tasks=changes.task_writes(),
                    events=changes.events + wf_events,
                )
            )
        return dispatched

    @staticmethod
    def _set_wait(task: Task, reason: str, changes: TaskChanges, now: datetime) -> None:
        if task.wait_reason != reason:
            changes.touch(task, now)
            task.wait_reason = reason

    def _move_workflow(
        self, wf: Workflow, target: WorkflowStatus, events: list[Event], now: datetime
    ) -> None:
        ensure_wf(wf.status, target)
        previous = wf.status
        wf.status = target
        wf.updated_at = now
        events.append(
            workflow_event(
                wf, EventType.WORKFLOW_STATUS_CHANGED, previous=str(previous), current=str(target)
            )
        )

    def _finish(self, wf: Workflow, integrate: Task | None, events: list[Event], now: datetime):
        assert wf.execution is not None and wf.route_plan is not None
        if wf.status == WorkflowStatus.EXECUTING:
            self._move_workflow(wf, WorkflowStatus.INTEGRATING, events, now)
        result = integrate.result if integrate else None
        wf.execution.integration_branch = result.branch if result else None
        wf.execution.integration_commit = result.commit if result else None
        wf.execution.finished_at = now
        remaining = []
        for stage in wf.route_plan.stages:
            if stage.capability == "development":
                stage.status = "completed"
            elif stage.status == "planned":
                remaining.append(stage.agent)
        events.append(
            workflow_event(
                wf,
                EventType.WORKFLOW_EXECUTION_FINISHED,
                outcome="completed",
                integration_branch=wf.execution.integration_branch,
                integration_commit=wf.execution.integration_commit,
                remaining_stages=remaining,
            )
        )
        if remaining:
            wf.execution.note = (
                "Development is complete on the integration branch. Next stages are not "
                f"implemented yet: {', '.join(remaining)}."
            )
            self._move_workflow(wf, WorkflowStatus.PAUSED, events, now)
        else:
            self._move_workflow(wf, WorkflowStatus.COMPLETED, events, now)

    def _pause_stuck(self, wf: Workflow, tasks: list[Task], events: list[Event], now: datetime):
        failed = [t for t in tasks if t.status == TaskStatus.FAILED]
        cancelled = [t for t in tasks if t.status == TaskStatus.CANCELLED]
        parts = [f"{t.key} failed after {t.attempt} attempt(s): {t.error}" for t in failed]
        parts += [f"{t.key} was cancelled" for t in cancelled]
        wf.error = "; ".join(parts) or "No task can make progress."
        if wf.route_plan:
            for stage in wf.route_plan.stages:
                if stage.capability == "development":
                    stage.status = "failed"
        self._move_workflow(wf, WorkflowStatus.PAUSED, events, now)

    # ---------------------------------------------------------- manual control

    async def retry_task(self, task_id: str) -> Task:
        task = await self.store.get_task(task_id)
        wf = await self.store.get_workflow(task.workflow_id)
        if task.status != TaskStatus.FAILED:
            raise ValidationFailed(f"only FAILED tasks can be retried (task is {task.status})")
        if wf.status not in (*EXECUTING_STATES, WorkflowStatus.PAUSED):
            raise ValidationFailed(f"workflow is {wf.status}; tasks cannot be retried")
        now = self.clock()
        changes = TaskChanges()
        task.max_attempts = max(task.max_attempts, task.attempt + 1)
        task.retryable = True
        task.not_before = now
        changes.move(task, TaskStatus.RETRYING, EventType.TASK_RETRYING, now, manual=True)
        events: list[Event] = []
        revision = wf.revision
        wf_changed = False
        if wf.status == WorkflowStatus.PAUSED:
            wf.error = None
            if wf.route_plan:
                for stage in wf.route_plan.stages:
                    if stage.capability == "development":
                        stage.status = "running"
            self._move_workflow(wf, WorkflowStatus.EXECUTING, events, now)
            wf_changed = True
        result = await self.store.commit(
            Commit(
                workflow=wf if wf_changed else None,
                expected_revision=revision,
                tasks=changes.task_writes(),
                events=changes.events + events,
            )
        )
        await self.tick(wf.workflow_id)
        return result.tasks[task_id]

    async def cancel_task(self, task_id: str) -> Task:
        task = await self.store.get_task(task_id)
        if task.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
            raise ValidationFailed(f"task is already {task.status}")
        changes = TaskChanges()
        changes.move(
            task, TaskStatus.CANCELLED, EventType.TASK_CANCELLED, self.clock(), manual=True
        )
        result = await self.store.commit(Commit(tasks=changes.task_writes(), events=changes.events))
        await self.tick(task.workflow_id)
        return result.tasks[task_id]

    # ------------------------------------------------------------------ reaper

    async def reap(self) -> set[str]:
        """Recover lost dispatches and dead workers (spec section 48).

        Returns workflow ids that should be ticked.
        """
        now = self.clock()
        to_tick: set[str] = set()
        stale_dispatch = now - timedelta(seconds=self.settings.dispatch_timeout_seconds)
        for t in await self.store.find_tasks(
            [TaskStatus.DISPATCHED], updated_before=stale_dispatch
        ):
            changes = TaskChanges()
            changes.touch(t, now)
            changes.events.append(
                task_event(
                    t,
                    EventType.TASK_DISPATCHED,
                    attempt=t.attempt,
                    redispatch=True,
                    idempotency_key=f"{t.workflow_id}:{t.task_id}:dispatch:{t.attempt}",
                )
            )
            await self._commit_quietly(Commit(tasks=changes.task_writes(), events=changes.events))

        stale_heartbeat = now - timedelta(seconds=self.settings.heartbeat_stale_seconds)
        for t in await self.store.find_tasks([TaskStatus.RUNNING]):
            last = t.heartbeat_at or t.started_at or t.updated_at
            if last >= stale_heartbeat:
                continue
            changes = TaskChanges()
            t.error = "worker heartbeat lost"
            t.retryable = True
            changes.move(
                t, TaskStatus.FAILED, EventType.TASK_FAILED, now, error=t.error, retryable=True
            )
            if await self._commit_quietly(
                Commit(tasks=changes.task_writes(), events=changes.events)
            ):
                to_tick.add(t.workflow_id)

        for t in await self.store.find_tasks([TaskStatus.RETRYING]):
            if t.not_before is None or t.not_before <= now:
                to_tick.add(t.workflow_id)
        return to_tick

    async def _commit_quietly(self, change: Commit) -> bool:
        try:
            await self.store.commit(change)
            return True
        except ConcurrencyConflict:
            return False
