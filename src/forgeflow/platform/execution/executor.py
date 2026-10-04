"""Agent-worker task execution (spec sections 24, 47, 48, 50, 51).

    1. claim the dispatched task (DISPATCHED -> RUNNING, optimistic concurrency)
    2. heartbeat while running; stop if the task is cancelled elsewhere
    3. run the handler for the task kind:
         decompose  - Developer agent plans subtasks -> new tasks in the graph
         implement  - fresh worktree -> Developer Subagent edits -> ForgeFlow commits
         integrate  - integration worktree -> merge branches -> resolve conflicts -> checks
    4. persist the structured result (COMPLETED) or the error (FAILED, retryable or not)

The worker never changes workflow state; the orchestrator's scheduler does that
when it observes the task events.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from forgeflow.agents.context import AgentRuntimeContext
from forgeflow.core.config import Settings
from forgeflow.core.errors import (
    ConcurrencyConflict,
    InvalidTaskGraph,
    MergeConflictUnresolved,
    ValidationFailed,
)
from forgeflow.core.ids import new_id, utcnow
from forgeflow.core.logging import bind_context, clear_context, log_event
from forgeflow.platform.orchestration.execution import TaskChanges, task_event
from forgeflow.platform.orchestration.gateway import (
    AgentGateway,
    AgentOutcome,
    ConflictRequest,
    DevelopmentRequest,
    ImplementationRequest,
)
from forgeflow.platform.state.store import Commit, WorkflowStore
from forgeflow.platform.task_graph.conflicts import conflict_reason
from forgeflow.platform.task_graph.graph import ancestors, completed_in_order, validate_plan
from forgeflow.platform.worktrees.manager import WorktreeManager
from forgeflow.schemas.events import Event, EventType, Topics
from forgeflow.schemas.requirement import RequirementSpecification
from forgeflow.schemas.task import CheckRun, Task, TaskResult, TaskStatus, Workspace
from forgeflow.schemas.workflow import AgentRunRecord, Workflow
from forgeflow.tools.filesystem import globs
from forgeflow.tools.git.client import GitClient
from forgeflow.tools.shell.commands import CheckRunner

logger = logging.getLogger(__name__)

CONFLICT_MARKERS = ("<<<<<<<", ">>>>>>>")
POST_MERGE_CHECKS = ("test", "lint")


@dataclass
class HandlerOutput:
    result: TaskResult
    new_tasks: list[Task] = field(default_factory=list)
    workspaces: list[Workspace] = field(default_factory=list)
    agent_runs: list[AgentRunRecord] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    workspace_id: str | None = None


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (InvalidTaskGraph, MergeConflictUnresolved, ValidationFailed)):
        return False
    retryable = getattr(exc, "retryable", None)
    if retryable is not None:
        return bool(retryable)
    return isinstance(exc, (TimeoutError, ConnectionError, OSError))


class TaskExecutor:
    def __init__(
        self,
        store: WorkflowStore,
        gateway: AgentGateway,
        worktrees: WorktreeManager,
        git: GitClient,
        settings: Settings,
        worker_id: str | None = None,
    ) -> None:
        self.store = store
        self.gateway = gateway
        self.worktrees = worktrees
        self.git = git
        self.settings = settings
        self.worker_id = worker_id or new_id("worker")

    # ------------------------------------------------------------------- entry

    async def execute(self, task_id: str, attempt: int) -> TaskStatus | None:
        """Run one dispatched task attempt. Returns the final status, or None if skipped."""
        bind_context(task_id=task_id, agent_id=self.worker_id)
        try:
            task = await self._claim(task_id, attempt)
            if task is None:
                return None
            bind_context(workflow_id=task.workflow_id)
            return await self._run_claimed(task)
        finally:
            clear_context()

    async def _claim(self, task_id: str, attempt: int) -> Task | None:
        task = await self.store.get_task(task_id)
        if task.status != TaskStatus.DISPATCHED or task.attempt != attempt:
            return None  # duplicate or stale dispatch event
        now = utcnow()
        changes = TaskChanges()
        task.worker_id = self.worker_id
        task.started_at = now
        task.heartbeat_at = now
        task.error = None
        changes.move(
            task,
            TaskStatus.RUNNING,
            EventType.TASK_STARTED,
            now,
            attempt=attempt,
            worker_id=self.worker_id,
        )
        try:
            result = await self.store.commit(
                Commit(tasks=changes.task_writes(), events=changes.events)
            )
        except ConcurrencyConflict:
            return None
        return result.tasks[task_id]

    async def _run_claimed(self, task: Task) -> TaskStatus | None:
        wf = await self.store.get_workflow(task.workflow_id)
        work = asyncio.create_task(self._handle(wf, task))
        beat = asyncio.create_task(self._heartbeat(task, work))
        try:
            output = await asyncio.wait_for(work, self.settings.task_timeout_seconds)
        except asyncio.CancelledError:
            if beat.done() and beat.result() is False:
                log_event(logger, "task no longer owned by this worker; result discarded")
                return None
            raise
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                exc = TimeoutError(f"task exceeded {self.settings.task_timeout_seconds:.0f}s")
            logger.exception("task failed")
            return await self._finish_failed(task, exc)
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
        return await self._finish_completed(task, output)

    async def _heartbeat(self, task: Task, work: asyncio.Task) -> bool:
        while True:
            await asyncio.sleep(self.settings.heartbeat_interval_seconds)
            if not await self.store.touch_task(task.task_id, self.worker_id, utcnow()):
                work.cancel()
                return False

    async def _finish_completed(self, task: Task, output: HandlerOutput) -> TaskStatus | None:
        current = await self.store.get_task(task.task_id)
        if current.status != TaskStatus.RUNNING or current.worker_id != self.worker_id:
            return None
        now = utcnow()
        changes = TaskChanges()
        current.result = output.result
        current.completed_at = now
        current.workspace_id = output.workspace_id or current.workspace_id
        changes.move(
            current,
            TaskStatus.COMPLETED,
            EventType.TASK_COMPLETED,
            now,
            commit=output.result.commit,
            files_changed=len(output.result.files_changed),
            new_tasks=[t.key for t in output.new_tasks],
        )
        for new in output.new_tasks:
            changes.insert(new)
            changes.events.append(
                task_event(
                    new, EventType.TASK_CREATED, kind=new.kind, dependencies=new.dependencies
                )
            )
        try:
            await self.store.commit(
                Commit(
                    tasks=changes.task_writes(),
                    workspaces=output.workspaces,
                    agent_runs=output.agent_runs,
                    events=output.events + changes.events,
                )
            )
        except ConcurrencyConflict:
            return None
        log_event(logger, "task completed", key=task.key, commit=output.result.commit)
        return TaskStatus.COMPLETED

    async def _finish_failed(self, task: Task, exc: BaseException) -> TaskStatus | None:
        current = await self.store.get_task(task.task_id)
        if current.status != TaskStatus.RUNNING or current.worker_id != self.worker_id:
            return None
        now = utcnow()
        changes = TaskChanges()
        current.error = f"{type(exc).__name__}: {exc}"[:2000]
        current.retryable = is_retryable(exc)
        changes.move(
            current,
            TaskStatus.FAILED,
            EventType.TASK_FAILED,
            now,
            error=current.error,
            retryable=current.retryable,
            attempt=current.attempt,
        )
        runs = []
        attempts = getattr(exc, "attempts", None)
        if attempts:
            runs.append(
                self._run_record(
                    task, task.agent_type, "unknown", attempts, [], now, error=current.error
                )
            )
        try:
            await self.store.commit(
                Commit(tasks=changes.task_writes(), agent_runs=runs, events=changes.events)
            )
        except ConcurrencyConflict:
            return None
        return TaskStatus.FAILED

    # ---------------------------------------------------------------- handlers

    async def _handle(self, wf: Workflow, task: Task) -> HandlerOutput:
        if wf.execution is None or wf.repository_path is None:
            raise ValidationFailed("workflow has no execution context")
        spec = await self.store.get_specification(wf.workflow_id)
        if spec is None:
            raise ValidationFailed("workflow has no requirement specification")
        if task.kind == "decompose":
            return await self._decompose(wf, task, spec)
        if task.kind == "implement":
            return await self._implement(wf, task, spec)
        return await self._integrate(wf, task, spec)

    async def _decompose(
        self, wf: Workflow, task: Task, spec: RequirementSpecification
    ) -> HandlerOutput:
        started = utcnow()
        repo = self.worktrees.repository(wf.repository_path or "")
        ctx = AgentRuntimeContext(
            workflow_id=wf.workflow_id, task_id=task.task_id, repository_root=repo
        )
        outcome = await self.gateway.plan_development(
            ctx, DevelopmentRequest(spec, self.settings.max_subtasks)
        )
        plan = outcome.output
        ordered = validate_plan(plan.subtasks, self.settings.max_subtasks)
        known_ac = {ac.id for ac in spec.acceptance_criteria}

        now = utcnow()
        ids: dict[str, str] = {}
        new_tasks: list[Task] = []
        risks = [plan.notes] if plan.notes else []
        for n, sub in enumerate(ordered, start=2):
            key = f"T{n}-{sub.key}"
            ids[sub.key] = f"{wf.workflow_id}.{key}"
            unknown = [a for a in sub.acceptance_criteria if a not in known_ac]
            if unknown:
                risks.append(f"{key}: ignored unknown acceptance criteria {unknown}")
            new_tasks.append(
                Task(
                    task_id=ids[sub.key],
                    workflow_id=wf.workflow_id,
                    key=key,
                    title=sub.title,
                    kind="implement",
                    agent_type="developer_subagent",
                    instructions=sub.instructions,
                    status=TaskStatus.PENDING,
                    dependencies=[ids[d] for d in sub.depends_on],
                    file_scope=sub.file_scope,
                    acceptance_criteria=[a for a in sub.acceptance_criteria if a in known_ac],
                    max_attempts=self.settings.task_max_attempts,
                    created_at=now,
                    updated_at=now,
                )
            )
        integration_key = f"T{len(ordered) + 2}-integration"
        new_tasks.append(
            Task(
                task_id=f"{wf.workflow_id}.{integration_key}",
                workflow_id=wf.workflow_id,
                key=integration_key,
                title="Integrate subtask branches",
                kind="integrate",
                agent_type="integrator",
                instructions="Merge every subtask branch, resolve conflicts, run checks.",
                status=TaskStatus.PENDING,
                dependencies=[t.task_id for t in new_tasks],
                max_attempts=self.settings.task_max_attempts,
                priority=10,
                created_at=now,
                updated_at=now,
            )
        )
        uncovered = known_ac - {a for t in new_tasks for a in t.acceptance_criteria}
        if uncovered:
            risks.append(f"acceptance criteria not mapped to any subtask: {sorted(uncovered)}")
        result = TaskResult(
            summary=plan.summary,
            risks=risks,
            next_actions=[f"{t.key}: {t.title}" for t in new_tasks],
        )
        run = self._run_from_outcome(task, "developer", outcome, started)
        return HandlerOutput(
            result=result,
            new_tasks=new_tasks,
            agent_runs=[run],
            events=[self._agent_event(task, run)],
        )

    async def _implement(
        self, wf: Workflow, task: Task, spec: RequirementSpecification
    ) -> HandlerOutput:
        assert wf.execution is not None and wf.repository_path is not None
        all_tasks = await self.store.list_tasks(wf.workflow_id)
        by_id = {t.task_id: t for t in all_tasks}
        upstream_ids = ancestors(task, by_id)
        predecessors = [
            t
            for t in completed_in_order(all_tasks)
            if t.kind == "implement"
            and t.result is not None
            and t.result.commit
            and (t.task_id in upstream_ids or conflict_reason(task, t) is not None)
        ]
        prepared = await self.worktrees.create(
            workflow_id=wf.workflow_id,
            task_id=task.task_id,
            key=task.key,
            repository_path=wf.repository_path,
            base_commit=wf.execution.base_commit,
            merge_commits=[
                (p.key, p.result.commit) for p in predecessors if p.result and p.result.commit
            ],
        )
        events = [
            task_event(
                task,
                EventType.WORKSPACE_CREATED,
                workspace_id=prepared.workspace.workspace_id,
                branch=prepared.workspace.branch,
                merged=prepared.merged,
            )
        ]
        checks = CheckRunner(
            prepared.path, self.settings.check_timeout_seconds, exclude_path=sys.prefix
        )
        ctx = AgentRuntimeContext(
            workflow_id=wf.workflow_id,
            task_id=task.task_id,
            repository_root=prepared.path,
            file_scope=task.file_scope,
            checks=checks,
        )
        started = utcnow()
        outcome = await self.gateway.implement_subtask(
            ctx,
            ImplementationRequest(
                specification=spec,
                task=task,
                upstream=[
                    f"{p.key} ({p.title}): {p.result.summary}"
                    for p in predecessors
                    if p.result and p.key in prepared.merged
                ],
                available_checks=[k for k in checks.available() if k != "setup"],
                warnings=prepared.warnings,
            ),
        )
        risks = list(outcome.output.risks) + prepared.warnings

        changed = await self.git.changed_files(prepared.path)
        outside = [f for f in changed if not globs.matches_any(f, task.file_scope)]
        if outside:
            # Build/test artifacts or tool side effects; never commit outside the scope.
            await self.git.discard_paths(prepared.path, outside)
            risks.append(
                f"discarded {len(outside)} out-of-scope change(s): {', '.join(outside[:10])}"
            )
        commit = await self.git.commit_all(
            prepared.path,
            f"forgeflow({task.key}): {task.title}\n\n"
            f"Workflow: {wf.workflow_id}\nTask: {task.task_id}",
        )
        files = [f for f in changed if f not in outside]
        if commit is None:
            risks.append("the subagent made no file changes")
        risks += [
            f"{c.kind} check failed: {c.command}"
            for c in checks.runs
            if not c.passed and c.kind != "setup"
        ]
        result = TaskResult(
            summary=outcome.output.summary,
            files_changed=files,
            commit=commit,
            branch=prepared.workspace.branch,
            checks=checks.runs,
            risks=risks,
            next_actions=outcome.output.next_actions,
            merged_tasks=prepared.merged,
        )
        run = self._run_from_outcome(task, "developer_subagent", outcome, started)
        events += [self._check_event(task, c) for c in checks.runs]
        events.append(self._agent_event(task, run))
        return HandlerOutput(
            result=result,
            workspaces=[prepared.workspace],
            agent_runs=[run],
            events=events,
            workspace_id=prepared.workspace.workspace_id,
        )

    async def _integrate(
        self, wf: Workflow, task: Task, spec: RequirementSpecification
    ) -> HandlerOutput:
        assert wf.execution is not None and wf.repository_path is not None
        all_tasks = await self.store.list_tasks(wf.workflow_id)
        deps = set(task.dependencies)
        to_merge = [
            t
            for t in completed_in_order(all_tasks)
            if t.task_id in deps and t.result is not None and t.result.commit
        ]
        prepared = await self.worktrees.create(
            workflow_id=wf.workflow_id,
            task_id=task.task_id,
            key="integration",
            repository_path=wf.repository_path,
            base_commit=wf.execution.base_commit,
        )
        path = prepared.path
        events = [
            task_event(
                task,
                EventType.WORKSPACE_CREATED,
                workspace_id=prepared.workspace.workspace_id,
                branch=prepared.workspace.branch,
            )
        ]
        runs: list[AgentRunRecord] = []
        merged: list[str] = []
        resolved: list[str] = []
        risks: list[str] = []
        for t in to_merge:
            assert t.result is not None and t.result.commit
            conflicts = await self.git.merge(
                path, t.result.commit, f"forgeflow: integrate {t.key} ({t.title})"
            )
            if conflicts:
                events.append(
                    task_event(
                        task, EventType.INTEGRATION_CONFLICT, incoming=t.key, files=conflicts
                    )
                )
                outcome, started = await self._resolve(wf, task, spec, path, t, merged, conflicts)
                runs.append(self._run_from_outcome(task, "integrator", outcome, started))
                remaining = [f for f in conflicts if self._has_markers(path / f)]
                if remaining:
                    await self.git.abort_merge(path)
                    raise MergeConflictUnresolved(
                        f"conflict markers remain after resolution in: {', '.join(remaining)}"
                    )
                await self.git.conclude_merge(path)
                resolved += conflicts
                risks += outcome.output.risks
            merged.append(t.key)

        head = await self.git.rev_parse(path)
        checks = CheckRunner(path, self.settings.check_timeout_seconds, exclude_path=sys.prefix)
        available = checks.available()
        for kind in POST_MERGE_CHECKS:
            if kind in available:
                await checks.run(kind)
        risks += [
            f"post-merge {c.kind} failed: {c.command}"
            for c in checks.runs
            if not c.passed and c.kind != "setup"
        ]
        # Post-merge checks must not leave artifacts on the integration branch.
        leftovers = await self.git.changed_files(path)
        if leftovers:
            await self.git.discard_paths(path, leftovers)
        repo = self.worktrees.repository(wf.repository_path)
        files = await self.git.files_between(repo, wf.execution.base_commit, head)
        if not to_merge:
            risks.append("no subtask produced a commit; the integration branch equals the base")
        result = TaskResult(
            summary=f"Integrated {len(merged)} subtask branch(es) into {prepared.workspace.branch}"
            + (f"; resolved conflicts in {len(resolved)} file(s)" if resolved else ""),
            files_changed=files,
            commit=head,
            branch=prepared.workspace.branch,
            checks=checks.runs,
            risks=risks,
            merged_tasks=merged,
            conflicts_resolved=resolved,
        )
        events += [self._check_event(task, c) for c in checks.runs]
        events += [self._agent_event(task, r) for r in runs]
        return HandlerOutput(
            result=result,
            workspaces=[prepared.workspace],
            agent_runs=runs,
            events=events,
            workspace_id=prepared.workspace.workspace_id,
        )

    async def _resolve(
        self,
        wf: Workflow,
        task: Task,
        spec: RequirementSpecification,
        path: Path,
        incoming: Task,
        merged: list[str],
        conflicts: list[str],
    ) -> tuple[AgentOutcome, datetime]:
        all_tasks = {t.key: t for t in await self.store.list_tasks(wf.workflow_id)}
        ctx = AgentRuntimeContext(
            workflow_id=wf.workflow_id,
            task_id=task.task_id,
            repository_root=path,
            file_scope=conflicts,
        )
        started = utcnow()
        outcome = await self.gateway.resolve_conflicts(
            ctx,
            ConflictRequest(
                specification=spec,
                conflicted_files=conflicts,
                ours=[f"{k}: {all_tasks[k].title}" for k in merged if k in all_tasks],
                theirs=f"{incoming.key}: {incoming.title} - "
                + (incoming.result.summary if incoming.result else ""),
            ),
        )
        return outcome, started

    @staticmethod
    def _has_markers(file: Path) -> bool:
        if not file.is_file():
            return False
        text = file.read_text(encoding="utf-8", errors="replace")
        return any(line.startswith(CONFLICT_MARKERS) for line in text.splitlines())

    # ----------------------------------------------------------------- records

    def _run_from_outcome(
        self, task: Task, agent_type: str, outcome: AgentOutcome, started: datetime
    ) -> AgentRunRecord:
        return self._run_record(
            task, agent_type, outcome.prompt_version, outcome.attempts, outcome.tool_calls, started
        )

    @staticmethod
    def _run_record(
        task: Task,
        agent_type: str,
        prompt_version: str,
        attempts: list,
        tool_calls: list,
        started: datetime,
        error: str | None = None,
    ) -> AgentRunRecord:
        return AgentRunRecord(
            run_id=new_id("run"),
            workflow_id=task.workflow_id,
            task_id=task.task_id,
            agent_type=agent_type,
            prompt_version=prompt_version,
            status="failed" if error else "completed",
            attempts=attempts,
            tool_calls=tool_calls,
            error=error,
            started_at=started,
            completed_at=utcnow(),
        )

    @staticmethod
    def _agent_event(task: Task, run: AgentRunRecord) -> Event:
        return Event(
            event_type=EventType.AGENT_RUN_COMPLETED
            if run.status == "completed"
            else EventType.AGENT_RUN_FAILED,
            topic=Topics.AGENT,
            workflow_id=task.workflow_id,
            task_id=task.task_id,
            payload={
                "run_id": run.run_id,
                "agent_type": run.agent_type,
                "prompt_version": run.prompt_version,
                "providers": [f"{a.provider}:{a.model}:{a.status}" for a in run.attempts],
                "tool_calls": len(run.tool_calls),
                "error": run.error,
            },
        )

    @staticmethod
    def _check_event(task: Task, check: CheckRun) -> Event:
        return task_event(
            task,
            EventType.CHECK_COMPLETED,
            kind=check.kind,
            command=check.command,
            passed=check.passed,
            exit_code=check.exit_code,
            duration_ms=check.duration_ms,
        )
