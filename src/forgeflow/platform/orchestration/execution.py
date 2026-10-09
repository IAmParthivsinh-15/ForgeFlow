"""Execution orchestration: the Task Graph Engine's scheduler (spec sections 16-20, 46-57).

The orchestrator never runs code itself. It creates logical tasks, decides which
are ready, serializes conflicting ones, and dispatches the rest by emitting
`task.dispatched` events. Agent workers execute them (platform/execution).

    workflow.routed
        development routed  -> EXECUTING, decompose task T1 -> subtasks -> integration
        otherwise           -> verification of the repository's current HEAD
    verification round r    -> V{r}-review, V{r}-security, V{r}-qa, V{r}-ci
                               (only stages in the route; dependencies follow the route)
    round has blockers      -> repair task F{r}-repair (bounded) -> round r+1
                               re-runs the failed stages, everything after them, and review
    blockers after the limit -> PAUSED awaiting a human decision (accept / one more repair)
    no blockers             -> final report -> COMPLETED

    tick():  FAILED(retryable) -> RETRYING -> READY (after backoff)
             PENDING -> READY when dependencies COMPLETED; -> BLOCKED if one is broken
             READY -> DISPATCHED (respecting conflicts and the parallelism limit)
             workflow status follows the active stage
             nothing can progress because a task failed -> PAUSED (retry or cancel)
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from forgeflow.core.config import Settings
from forgeflow.core.errors import ConcurrencyConflict, GitError, ValidationFailed
from forgeflow.core.ids import utcnow
from forgeflow.core.logging import bind_context, log_event
from forgeflow.platform.orchestration.report import build_report
from forgeflow.platform.orchestration.routing import plan_route
from forgeflow.platform.orchestration.state_machine import EXECUTION_STATES
from forgeflow.platform.orchestration.state_machine import ensure_transition as ensure_wf
from forgeflow.platform.state.store import Commit, TaskWrite, WorkflowStore
from forgeflow.platform.task_graph.conflicts import conflict_reason
from forgeflow.platform.task_graph.graph import dependency_state
from forgeflow.platform.task_graph.state_machine import ACTIVE, ensure_transition
from forgeflow.platform.verification.change_analyzer import analyze_changes
from forgeflow.platform.worktrees.manager import WorktreeManager
from forgeflow.schemas.events import Event, EventType, Topics
from forgeflow.schemas.extensibility import CapabilitySnapshot
from forgeflow.schemas.requirement import RequiredCapabilities
from forgeflow.schemas.task import VERIFICATION_KINDS, Task, TaskStatus
from forgeflow.schemas.workflow import (
    Capability,
    ExecutionInfo,
    PullRequestInfo,
    RouteStage,
    Workflow,
    WorkflowStatus,
)
from forgeflow.tools.git.client import GitClient

logger = logging.getLogger(__name__)

EXECUTING_STATES = EXECUTION_STATES
TICK_RETRIES = 5
# Agents whose capability manifests are pinned at execution start.
SNAPSHOT_AGENTS = ("developer", "developer_subagent", "code_review", "security", "qa")
PROGRESSING = frozenset(
    {TaskStatus.READY, TaskStatus.DISPATCHED, TaskStatus.RUNNING, TaskStatus.RETRYING}
)
STAGE_KIND: dict[Capability, str] = {
    "code_review": "review",
    "security": "security",
    "qa": "qa",
    "ci": "ci",
}
KIND_STAGE = {kind: stage for stage, kind in STAGE_KIND.items()}
STATUS_FOR_STAGE: dict[str, WorkflowStatus] = {
    "code_review": WorkflowStatus.REVIEWING,
    "security": WorkflowStatus.REVIEWING,
    "qa": WorkflowStatus.TESTING,
    "ci": WorkflowStatus.CI,
}
# Workflow status while a task of this kind is active (highest priority first).
_STATUS_BY_KIND = (
    ("publish", WorkflowStatus.PUBLISHING),
    ("ci", WorkflowStatus.CI),
    ("qa", WorkflowStatus.TESTING),
    ("review", WorkflowStatus.REVIEWING),
    ("security", WorkflowStatus.REVIEWING),
    ("integrate", WorkflowStatus.INTEGRATING),
    ("repair", WorkflowStatus.EXECUTING),
    ("implement", WorkflowStatus.EXECUTING),
    ("decompose", WorkflowStatus.EXECUTING),
)


def task_event(task: Task, event_type: str, **payload: Any) -> Event:
    base: dict[str, Any] = {
        "key": task.key,
        "status": str(task.status),
        "kind": task.kind,
        "agent": task.agent_type,
    }
    if task.started_at and task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
        ended = task.completed_at or task.updated_at
        base["duration_ms"] = max(0, int((ended - task.started_at).total_seconds() * 1000))
    return Event(
        event_type=event_type,
        topic=Topics.TASK,
        workflow_id=task.workflow_id,
        task_id=task.task_id,
        payload={**base, **payload},
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
        extensibility: Any = None,
    ) -> None:
        self.store = store
        self.git = git
        self.worktrees = worktrees
        self.settings = settings
        self.clock = clock
        self.extensibility = extensibility
        # L4 guardrails, installed by the autonomy layer (additional.md sections 3-4):
        #   start_guard(wf)            -> reason execution may not start, or None
        #   spawn_guard(wf, active)    -> (how many tasks may still start, reason)
        # Enforced here, outside any LLM.
        self.start_guard: Callable[[Workflow], Awaitable[str | None]] | None = None
        self.spawn_guard: Callable[[Workflow, int], Awaitable[tuple[int, str | None]]] | None = None

    # ------------------------------------------------------------------ start

    async def start_execution(self, workflow_id: str) -> Workflow | None:
        """Begin execution of a PLANNED workflow's routed stages. Idempotent."""
        bind_context(workflow_id=workflow_id)
        wf = await self.store.get_workflow(workflow_id)
        if wf.status != WorkflowStatus.PLANNED or wf.execution is not None or wf.route_plan is None:
            return None
        if not wf.route_plan.stages:
            return None
        if self.start_guard is not None:
            blocked = await self.start_guard(wf)
            if blocked is not None:
                log_event(logger, "execution not started", reason=blocked)
                return None
        revision = wf.revision
        now = self.clock()

        blocker = await self._repository_blocker(wf)
        if blocker is not None:
            for stage in wf.route_plan.stages:
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
        wf.execution = ExecutionInfo(
            base_commit=sha, base_ref=ref, target_commit=sha, started_at=now
        )
        await self._snapshot_capabilities(wf, now)
        events = [
            workflow_event(wf, EventType.WORKFLOW_EXECUTION_STARTED, base_commit=sha, base_ref=ref)
        ]
        changes = TaskChanges()
        if self._has_development(wf):
            self._set_stage(wf, "development", "running")
            self._move_workflow(wf, WorkflowStatus.EXECUTING, events, now)
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
        else:
            # Verification-only request (review/audit/QA/CI of the current HEAD).
            stages = self._verification_stages(wf)
            self._move_workflow(wf, STATUS_FOR_STAGE[stages[0].capability], events, now)
            self._create_round(wf, 1, stages, changes, events, now)
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
            return "Execution requires a repository; none is attached to this workflow."
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
        now = self.clock()
        changes = TaskChanges()
        wf_events: list[Event] = []
        wf_revision = wf.revision
        snapshot = wf.model_dump()

        self._apply_retry_policy(tasks, changes, now)
        self._resolve_dependencies(tasks, {t.task_id: t for t in tasks}, changes, now)

        # Nothing left to run: either something failed for good, or the phase is done.
        if not any(t.status in PROGRESSING or t.status == TaskStatus.PENDING for t in tasks):
            if any(t.status != TaskStatus.COMPLETED for t in tasks):
                self._pause_stuck(wf, tasks, wf_events, now)
            else:
                spec = await self.store.get_specification(workflow_id)
                new = await self._advance(wf, tasks, spec, changes, wf_events, now)
                tasks = tasks + new
                self._resolve_dependencies(new, {t.task_id: t for t in tasks}, changes, now)

        allowance: int | None = None
        if self.spawn_guard is not None and wf.status in EXECUTING_STATES:
            active = sum(1 for t in tasks if t.status in ACTIVE)
            allowance, why = await self.spawn_guard(wf, active)
            if allowance <= 0:
                for t in tasks:
                    if t.status == TaskStatus.READY:
                        self._set_wait(t, f"guardrail: {why}", changes, now)
        dispatched = (
            self._dispatch(workflow_id, tasks, changes, now, allowance)
            if wf.status in EXECUTING_STATES and (allowance is None or allowance > 0)
            else []
        )
        await self._sync_status(wf, tasks, wf_events, now)

        wf_changed = wf.model_dump() != snapshot
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

    def _apply_retry_policy(self, tasks: list[Task], changes: TaskChanges, now: datetime) -> None:
        """Spec section 49."""
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

    @staticmethod
    def _resolve_dependencies(
        tasks: list[Task], by_id: dict[str, Task], changes: TaskChanges, now: datetime
    ) -> None:
        """Spec section 19."""
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

    def _dispatch(
        self,
        workflow_id: str,
        tasks: list[Task],
        changes: TaskChanges,
        now: datetime,
        allowance: int | None = None,
    ) -> list[str]:
        """Conflict-aware dispatch (spec sections 20, 113, 185), capped by the L4 guard."""
        active = [t for t in tasks if t.status in ACTIVE]
        dispatched: list[str] = []
        ready = sorted(
            (t for t in tasks if t.status == TaskStatus.READY),
            key=lambda t: (-t.priority, t.created_at),
        )
        for t in ready:
            if allowance is not None and len(dispatched) >= allowance:
                self._set_wait(t, "guardrail: worker cap reached", changes, now)
                continue
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
        return dispatched

    async def _sync_status(
        self, wf: Workflow, tasks: list[Task], events: list[Event], now: datetime
    ) -> None:
        """The workflow status follows the stage that is currently active.

        A pending human approval takes precedence (spec section 30: WAITING_FOR_APPROVAL).
        """
        if wf.status not in EXECUTING_STATES:
            return
        if self.extensibility is not None:
            pending = await self.extensibility.store.find(
                "approvals", {"workflow_id": wf.workflow_id, "status": "pending"}, limit=1
            )
            if pending:
                if wf.status != WorkflowStatus.WAITING_FOR_APPROVAL:
                    self._move_workflow(wf, WorkflowStatus.WAITING_FOR_APPROVAL, events, now)
                return
        active_kinds = {t.kind for t in tasks if t.status in ACTIVE}
        for kind, status in _STATUS_BY_KIND:
            if kind in active_kinds:
                if wf.status != status:
                    self._move_workflow(wf, status, events, now)
                return

    # -------------------------------------------------------------- progression

    async def _advance(
        self,
        wf: Workflow,
        tasks: list[Task],
        spec: Any,
        changes: TaskChanges,
        events: list[Event],
        now: datetime,
    ) -> list[Task]:
        """Everything so far is COMPLETED: start the next phase. Returns new tasks."""
        assert wf.execution is not None and wf.route_plan is not None
        ex = wf.execution
        before = set(changes.writes)
        verification = [t for t in tasks if t.kind in VERIFICATION_KINDS]
        last_round = max((t.round for t in verification), default=0)
        last_repair = max((t.round for t in tasks if t.kind == "repair"), default=0)

        publish = next((t for t in tasks if t.kind == "publish"), None)
        if publish is not None:
            if publish.result and publish.result.pull_request:
                wf.pull_request = PullRequestInfo.model_validate(publish.result.pull_request)
            self._finish(wf, spec, tasks, events, now)
            return []

        integrate = next((t for t in tasks if t.kind == "integrate"), None)
        if integrate and integrate.result and ex.integration_commit is None:
            ex.integration_branch = integrate.result.branch
            ex.integration_commit = integrate.result.commit
            ex.target_commit = integrate.result.commit
            self._set_stage(wf, "development", "completed")
            events.append(
                workflow_event(
                    wf,
                    EventType.WORKFLOW_EXECUTION_FINISHED,
                    outcome="developed",
                    integration_branch=ex.integration_branch,
                    integration_commit=ex.integration_commit,
                )
            )

        if last_round == 0:
            if self._has_development(wf):
                await self._route_by_change(wf, events)
            stages = self._verification_stages(wf)
            if not stages:
                await self._complete(wf, spec, tasks, changes, events, now)
            else:
                self._create_round(wf, 1, stages, changes, events, now)
        elif last_repair == last_round:
            repair = max((t for t in tasks if t.kind == "repair"), key=lambda t: t.round)
            self._set_stage(wf, "development", "completed")
            if repair.result and repair.result.commit:
                ex.target_commit = repair.result.commit
                ex.integration_commit = repair.result.commit
            failed = {
                KIND_STAGE[t.kind]
                for t in verification
                if t.round == last_round and t.result and t.result.blocking
            }
            self._create_round(
                wf, last_round + 1, self._rerun_stages(wf, failed), changes, events, now
            )
        else:
            round_tasks = [t for t in verification if t.round == last_round]
            for t in round_tasks:
                self._set_stage(
                    wf,
                    KIND_STAGE[t.kind],
                    "failed" if t.result and t.result.blocking else "completed",
                )
            blockers = [t for t in round_tasks if t.result and t.result.blocking]
            if not blockers or not self._has_development(wf) or ex.accepted_risks:
                # A pure review/audit reports its findings; there is nothing to repair.
                # Accepted risks were decided by a human (resolve_decision).
                await self._complete(wf, spec, tasks, changes, events, now)
            elif ex.repair_attempts < self.settings.max_repair_attempts + ex.extra_repairs_allowed:
                self._request_repair(wf, tasks, blockers, last_round, changes, events, now)
            else:
                self._await_decision(wf, blockers, events, now)
        return [
            w.task
            for tid, w in changes.writes.items()
            if tid not in before and w.expected_revision is None
        ]

    async def _route_by_change(self, wf: Workflow, events: list[Event]) -> None:
        """Dynamic review routing (spec sections 2.5, 53, 189): add Security if needed."""
        assert wf.execution is not None and wf.route_plan is not None
        if any(s.capability == "security" for s in wf.route_plan.stages):
            return
        ex = wf.execution
        if not ex.integration_commit or ex.integration_commit == ex.base_commit:
            return
        repo = self.worktrees.repository(wf.repository_path or "")
        files = await self.git.files_between(repo, ex.base_commit, ex.integration_commit)
        change = analyze_changes(files)
        if "security" not in change.reviewers:
            return
        statuses = {s.capability: s.status for s in wf.route_plan.stages}
        required = RequiredCapabilities(**{c: True for c in statuses}, security=True)
        plan = plan_route(required)
        for stage in plan.stages:
            stage.status = statuses.get(stage.capability, "planned")  # type: ignore[assignment]
            if stage.capability == "security":
                domains = sorted(
                    set(change.domains) & {"auth", "infra", "dependencies", "database"}
                )
                stage.reason = f"Added by the change analyzer: {', '.join(domains)} files changed."
        plan.rationale += " (security added by the change analyzer)"
        wf.route_plan = plan
        events.append(
            workflow_event(
                wf,
                EventType.WORKFLOW_ROUTED,
                stages=[s.agent for s in plan.stages],
                skipped=list(plan.skipped),
                reason="change analyzer",
            )
        )

    def _create_round(
        self,
        wf: Workflow,
        round_no: int,
        stages: list[RouteStage],
        changes: TaskChanges,
        events: list[Event],
        now: datetime,
    ) -> None:
        assert wf.execution is not None
        wf.execution.verification_round = round_no
        included = {s.stage_id for s in stages}
        ids = {s.stage_id: f"{wf.workflow_id}.V{round_no}-{s.capability}" for s in stages}
        for stage in stages:
            kind = STAGE_KIND[stage.capability]
            task = Task(
                task_id=ids[stage.stage_id],
                workflow_id=wf.workflow_id,
                key=f"V{round_no}-{stage.capability}",
                title=f"{stage.agent.replace('_', ' ').title()} (round {round_no})",
                kind=kind,  # type: ignore[arg-type]
                agent_type=stage.agent,
                instructions=stage.reason,
                status=TaskStatus.PENDING,
                dependencies=[ids[d] for d in stage.depends_on if d in included],
                round=round_no,
                max_attempts=self.settings.task_max_attempts,
                priority=60,
                created_at=now,
                updated_at=now,
            )
            changes.insert(task)
            changes.events.append(
                task_event(task, EventType.TASK_CREATED, kind=kind, round=round_no)
            )
            self._set_stage(wf, stage.capability, "running")
        events.append(
            workflow_event(
                wf,
                EventType.VERIFICATION_ROUND_STARTED,
                round=round_no,
                stages=[s.capability for s in stages],
                commit=wf.execution.target_commit,
            )
        )

    def _rerun_stages(self, wf: Workflow, failed: set[Capability]) -> list[RouteStage]:
        """Failed stages, every stage after the earliest of them, and code review."""
        stages = self._verification_stages(wf)
        order = [s.capability for s in stages]
        first = min((order.index(c) for c in failed if c in order), default=0)
        keep = set(order[first:]) | ({"code_review"} & set(order))
        return [s for s in stages if s.capability in keep]

    def _request_repair(
        self,
        wf: Workflow,
        tasks: list[Task],
        blockers: list[Task],
        round_no: int,
        changes: TaskChanges,
        events: list[Event],
        now: datetime,
    ) -> None:
        assert wf.execution is not None
        wf.execution.repair_attempts += 1
        reasons = [r for t in blockers if t.result for r in t.result.blocking_reasons]
        lines = [
            f"Fix the blocking verification findings from round {round_no}.",
            "Change only what is needed to resolve them; keep all acceptance criteria working.",
            "",
        ]
        for t in blockers:
            assert t.result is not None
            lines.append(f"## {KIND_STAGE[t.kind]}")
            lines += [f"- {r}" for r in t.result.blocking_reasons]
            if t.result.review:
                lines += [
                    f"  suggestion ({f.file}:{f.line}): {f.suggestion}"
                    for f in t.result.review.findings
                    if f.suggestion
                ]
            if t.result.security:
                lines += [
                    f"  remediation ({f.file}:{f.line}): {f.remediation}"
                    for f in t.result.security.findings
                    if f.severity in ("high", "critical")
                ]
            if t.result.ci and t.result.ci.analysis:
                a = t.result.ci.analysis
                lines += [f"  cause: {a.suspected_cause}", f"  fix: {a.recommended_fix}"]
                lines += [f"  log: {e}" for e in a.evidence[:8]]
            if t.result.qa:
                lines += [
                    f"  {c.id}: {c.evidence}" for c in t.result.qa.criteria if c.status == "FAIL"
                ]
            lines.append("")
        scope = self._repair_scope(tasks, blockers)
        repair = Task(
            task_id=f"{wf.workflow_id}.F{round_no}-repair",
            workflow_id=wf.workflow_id,
            key=f"F{round_no}-repair",
            title=f"Repair verification findings (round {round_no})",
            kind="repair",
            agent_type="developer_subagent",
            instructions="\n".join(lines),
            status=TaskStatus.PENDING,
            file_scope=scope,
            round=round_no,
            max_attempts=self.settings.task_max_attempts,
            priority=90,
            created_at=now,
            updated_at=now,
        )
        changes.insert(repair)
        changes.events.append(
            task_event(repair, EventType.TASK_CREATED, kind="repair", round=round_no)
        )
        self._set_stage(wf, "development", "running")
        events.append(
            workflow_event(
                wf,
                EventType.REPAIR_REQUESTED,
                round=round_no,
                attempt=wf.execution.repair_attempts,
                reasons=reasons[:20],
            )
        )

    @staticmethod
    def _repair_scope(tasks: list[Task], blockers: list[Task]) -> list[str]:
        scope: list[str] = []
        for t in tasks:
            if t.kind == "implement":
                scope += t.file_scope
            if t.kind in ("integrate", "repair") and t.result:
                scope += t.result.files_changed
        for t in blockers:
            if t.result and t.result.review:
                scope += [f.file for f in t.result.review.findings if f.file]
            if t.result and t.result.security:
                scope += [f.file for f in t.result.security.findings if f.file]
        unique = list(dict.fromkeys(s for s in scope if s))
        return unique or ["**"]

    def _await_decision(
        self, wf: Workflow, blockers: list[Task], events: list[Event], now: datetime
    ) -> None:
        """Repair limit reached (spec section 57: WAITING_FOR_HUMAN)."""
        assert wf.execution is not None
        reasons = [r for t in blockers if t.result for r in t.result.blocking_reasons]
        wf.execution.awaiting_decision = True
        wf.error = (
            f"Blocking findings remain after {wf.execution.repair_attempts} repair round(s): "
            + "; ".join(reasons[:5])
        )
        events.append(workflow_event(wf, EventType.WORKFLOW_AWAITING_DECISION, reasons=reasons))
        self._move_workflow(wf, WorkflowStatus.PAUSED, events, now)

    async def _complete(
        self,
        wf: Workflow,
        spec: Any,
        tasks: list[Task],
        changes: TaskChanges,
        events: list[Event],
        now: datetime,
    ) -> None:
        """Verification is done: open the pull request if the project wants one, else finish."""
        if not await self._should_publish(wf):
            self._finish(wf, spec, tasks, events, now)
            return
        wf.report = build_report(wf, spec, tasks)  # the PR description comes from it
        task = Task(
            task_id=f"{wf.workflow_id}.P1-publish",
            workflow_id=wf.workflow_id,
            key="P1-publish",
            title="Open the pull request on GitHub",
            kind="publish",
            agent_type="github",
            instructions="Push the verified branch and open a pull request (requires approval).",
            status=TaskStatus.PENDING,
            max_attempts=self.settings.task_max_attempts,
            priority=100,
            created_at=now,
            updated_at=now,
        )
        changes.insert(task)
        changes.events.append(task_event(task, EventType.TASK_CREATED, kind="publish"))

    async def _should_publish(self, wf: Workflow) -> bool:
        ext = self.extensibility
        ex = wf.execution
        if ext is None or ex is None or not self._has_development(wf):
            return False
        if not ex.target_commit or ex.target_commit == ex.base_commit:
            return False  # nothing was changed
        project = await ext.projects.find_for_repository(wf.repository_path)
        if not (project and project.github and project.github.auto_pull_request):
            return False
        connector = await ext.store.get("connectors", project.github.connector_id)
        if connector is None or connector["status"] != "active":
            state = connector["status"] if connector else "deleted"
            ex.note = (
                f"Pull request not opened: the GitHub connector is {state}. "
                "Reconnect it and push the branch manually or re-run the workflow."
            )
            return False
        return True

    async def _snapshot_capabilities(self, wf: Workflow, now: datetime) -> None:
        """Pin skill versions and record MCP/connector configuration (spec section 244)."""
        ext = self.extensibility
        if ext is None or not wf.repository_path:
            return
        project = await ext.projects.ensure(self.settings.local_user_id, wf.repository_path)
        wf.project_id = project.project_id
        spec = await self.store.get_specification(wf.workflow_id)
        parts = [wf.request]
        if spec is not None:
            parts += [spec.summary, spec.goal, *(c.description for c in spec.checklist)]
            parts += [
                f"{ac.description} ({ac.verification.replace('_', ' ')})"
                for ac in spec.acceptance_criteria
            ]
        context = " ".join(parts)
        manifests = {}
        for agent in SNAPSHOT_AGENTS:
            resolution = await ext.resolver.resolve(
                owner_id=project.owner_id,
                workflow_id=wf.workflow_id,
                project=project,
                agent=agent,
                context=context,
            )
            manifests[agent] = resolution.manifest
        mcp_hashes = {}
        for mcp_id in project.enabled_mcp_ids:
            doc = await ext.store.get("mcp_servers", mcp_id)
            if doc:
                mcp_hashes[mcp_id] = doc.get("config_hash", "")
        scopes = {}
        if project.github:
            doc = await ext.store.get("connectors", project.github.connector_id)
            if doc:
                scopes[doc["connector_id"]] = list(doc.get("scopes", []))
        wf.capability_snapshot = CapabilitySnapshot(
            resolved_at=now,
            manifests=manifests,
            mcp_config_hashes=mcp_hashes,
            connector_scopes=scopes,
        )

    def _finish(
        self, wf: Workflow, spec: Any, tasks: list[Task], events: list[Event], now: datetime
    ) -> None:
        assert wf.execution is not None
        wf.execution.finished_at = now
        wf.execution.awaiting_decision = False
        wf.report = build_report(wf, spec, tasks)
        events.append(
            workflow_event(
                wf,
                EventType.WORKFLOW_COMPLETED,
                outcome=wf.report.outcome,
                summary=wf.report.summary,
            )
        )
        self._move_workflow(wf, WorkflowStatus.COMPLETED, events, now)

    def _pause_stuck(self, wf: Workflow, tasks: list[Task], events: list[Event], now: datetime):
        failed = [t for t in tasks if t.status == TaskStatus.FAILED]
        cancelled = [t for t in tasks if t.status == TaskStatus.CANCELLED]
        parts = [f"{t.key} failed after {t.attempt} attempt(s): {t.error}" for t in failed]
        parts += [f"{t.key} was cancelled" for t in cancelled]
        wf.error = "; ".join(parts) or "No task can make progress."
        for t in failed + cancelled:
            capability = KIND_STAGE.get(t.kind, "development")
            self._set_stage(wf, capability, "failed")
        self._move_workflow(wf, WorkflowStatus.PAUSED, events, now)

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _has_development(wf: Workflow) -> bool:
        return bool(wf.route_plan) and any(
            s.capability == "development"
            for s in wf.route_plan.stages  # type: ignore[union-attr]
        )

    @staticmethod
    def _verification_stages(wf: Workflow) -> list[RouteStage]:
        assert wf.route_plan is not None
        return [s for s in wf.route_plan.stages if s.capability in STAGE_KIND and s.implemented]

    @staticmethod
    def _set_stage(wf: Workflow, capability: str, status: str) -> None:
        if wf.route_plan:
            for stage in wf.route_plan.stages:
                if stage.capability == capability:
                    stage.status = status  # type: ignore[assignment]

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
                wf,
                EventType.WORKFLOW_STATUS_CHANGED,
                previous=str(previous),
                current=str(target),
                age_seconds=round((now - wf.created_at).total_seconds(), 3),
            )
        )

    # ---------------------------------------------------------- manual control

    async def resolve_decision(self, workflow_id: str, action: str) -> Workflow:
        """Human decision on blockers left after the repair limit (spec section 30)."""
        wf = await self.store.get_workflow(workflow_id)
        if (
            wf.status != WorkflowStatus.PAUSED
            or not wf.execution
            or not wf.execution.awaiting_decision
        ):
            raise ValidationFailed("this workflow is not waiting for a decision")
        if action not in ("accept", "repair"):
            raise ValidationFailed("action must be 'accept' or 'repair'")
        now = self.clock()
        revision = wf.revision
        events: list[Event] = []
        tasks = await self.store.list_tasks(workflow_id)
        ex = wf.execution
        ex.awaiting_decision = False
        wf.error = None
        if action == "accept":
            last = max((t.round for t in tasks if t.kind in VERIFICATION_KINDS), default=0)
            ex.accepted_risks = [
                r
                for t in tasks
                if t.round == last and t.result and t.result.blocking
                for r in t.result.blocking_reasons
            ] or ["blocking findings accepted by a human"]
        else:
            ex.extra_repairs_allowed += 1
        self._move_workflow(wf, WorkflowStatus.EXECUTING, events, now)
        await self.store.commit(Commit(workflow=wf, expected_revision=revision, events=events))
        await self.tick(workflow_id)
        return await self.store.get_workflow(workflow_id)

    async def retry_task(self, task_id: str) -> Task:
        task = await self.store.get_task(task_id)
        wf = await self.store.get_workflow(task.workflow_id)
        if task.status != TaskStatus.FAILED:
            raise ValidationFailed(f"only FAILED tasks can be retried (task is {task.status})")
        if wf.status not in (*EXECUTING_STATES, WorkflowStatus.PAUSED):
            raise ValidationFailed(f"workflow is {wf.status}; tasks cannot be retried")
        if wf.execution and wf.execution.awaiting_decision:
            raise ValidationFailed("the workflow is waiting for a decision on findings")
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
            self._set_stage(wf, KIND_STAGE.get(task.kind, "development"), "running")
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

    async def halt_for_stop(self, workflow_id: str, reason: str) -> list[str]:
        """Emergency stop (additional.md section 5): stop active work, keep it resumable.

        Dispatched/running tasks become FAILED (not retryable) so their workers see
        they lost ownership and no reaper retries them; queued tasks stay put because
        the spawn guard refuses to dispatch them. The workflow pauses. Resume re-queues
        the halted tasks explicitly.
        """
        wf = await self.store.get_workflow(workflow_id)
        now = self.clock()
        changes = TaskChanges()
        halted = []
        for t in await self.store.list_tasks(workflow_id):
            if t.status in ACTIVE:
                t.error = f"emergency stop: {reason}"[:500]
                t.retryable = False
                changes.move(
                    t, TaskStatus.FAILED, EventType.TASK_FAILED, now, error=t.error, halted=True
                )
                halted.append(t.task_id)
        events: list[Event] = []
        wf_changed = False
        if wf.status in EXECUTING_STATES:
            wf.error = f"Stopped: {reason}"
            self._move_workflow(wf, WorkflowStatus.PAUSED, events, now)
            wf_changed = True
        await self.store.commit(
            Commit(
                workflow=wf if wf_changed else None,
                expected_revision=wf.revision,
                tasks=changes.task_writes(),
                events=changes.events + events,
            )
        )
        return halted

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
