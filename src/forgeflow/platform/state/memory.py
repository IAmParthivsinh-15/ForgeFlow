"""In-memory WorkflowStore with the same semantics as the Mongo store. Used by tests."""

from __future__ import annotations

import asyncio
from datetime import datetime

from forgeflow.core.errors import ConcurrencyConflict, NotFoundError
from forgeflow.platform.state.store import Commit, CommitResult, StoredEvent
from forgeflow.schemas.events import Event
from forgeflow.schemas.requirement import ClarificationQuestion, RequirementSpecification
from forgeflow.schemas.task import Task, TaskStatus, Workspace
from forgeflow.schemas.verification import A2AMessage
from forgeflow.schemas.workflow import AgentRunRecord, Workflow


class InMemoryWorkflowStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.workflows: dict[str, Workflow] = {}
        self.specs: dict[tuple[str, int], RequirementSpecification] = {}
        self.questions: dict[str, ClarificationQuestion] = {}
        self.agent_runs: dict[str, AgentRunRecord] = {}
        self.tasks: dict[str, Task] = {}
        self.workspaces: dict[str, Workspace] = {}
        self.a2a: dict[str, A2AMessage] = {}
        self.counters: dict[str, int] = {}
        self.events: list[tuple[StoredEvent, bool]] = []

    async def commit(self, change: Commit) -> CommitResult:
        async with self._lock:
            # Validate every guard before mutating anything (atomicity).
            wf = change.workflow
            if wf is not None:
                existing = self.workflows.get(wf.workflow_id)
                if change.create_workflow:
                    if existing is not None:
                        raise ConcurrencyConflict(f"workflow {wf.workflow_id} already exists")
                elif existing is None:
                    raise NotFoundError(f"workflow {wf.workflow_id} not found")
                elif existing.revision != change.expected_revision:
                    raise ConcurrencyConflict(f"workflow {wf.workflow_id} changed concurrently")
            for write in change.tasks:
                current = self.tasks.get(write.task.task_id)
                if write.expected_revision is None:
                    if current is not None:
                        raise ConcurrencyConflict(f"task {write.task.task_id} already exists")
                elif current is None:
                    raise NotFoundError(f"task {write.task.task_id} not found")
                elif current.revision != write.expected_revision:
                    raise ConcurrencyConflict(f"task {write.task.task_id} changed concurrently")

            stored_wf = None
            if wf is not None:
                revision = 1 if change.create_workflow else (change.expected_revision or 0) + 1
                stored_wf = wf.model_copy(update={"revision": revision}, deep=True)
                self.workflows[wf.workflow_id] = stored_wf
            stored_tasks: dict[str, Task] = {}
            for write in change.tasks:
                revision = (write.expected_revision or 0) + 1
                stored = write.task.model_copy(update={"revision": revision}, deep=True)
                self.tasks[stored.task_id] = stored
                stored_tasks[stored.task_id] = stored.model_copy(deep=True)
            for spec in change.specifications:
                self.specs[(spec.workflow_id, spec.version)] = spec.model_copy(deep=True)
            for question in change.questions:
                self.questions[question.question_id] = question.model_copy(deep=True)
            for run in change.agent_runs:
                self.agent_runs[run.run_id] = run.model_copy(deep=True)
            for ws in change.workspaces:
                self.workspaces[ws.workspace_id] = ws.model_copy(deep=True)
            for msg in change.a2a_messages:
                self.a2a[msg.message_id] = msg.model_copy(deep=True)
            for event in change.events:
                seq = self.counters.get(event.workflow_id, 0) + 1
                self.counters[event.workflow_id] = seq
                self.events.append((StoredEvent(seq=seq, event=event), False))
            return CommitResult(
                stored_wf.model_copy(deep=True) if stored_wf else None, stored_tasks
            )

    async def get_workflow(self, workflow_id: str) -> Workflow:
        wf = self.workflows.get(workflow_id)
        if wf is None:
            raise NotFoundError(f"workflow {workflow_id} not found")
        return wf.model_copy(deep=True)

    async def list_workflows(self, limit: int = 50) -> list[Workflow]:
        ordered = sorted(self.workflows.values(), key=lambda w: w.created_at, reverse=True)
        return [w.model_copy(deep=True) for w in ordered[:limit]]

    async def get_specification(
        self, workflow_id: str, version: int | None = None
    ) -> RequirementSpecification | None:
        versions = [v for (wid, v) in self.specs if wid == workflow_id]
        if not versions:
            return None
        key = (workflow_id, version if version is not None else max(versions))
        spec = self.specs.get(key)
        return spec.model_copy(deep=True) if spec else None

    async def list_specifications(self, workflow_id: str) -> list[RequirementSpecification]:
        return [
            s.model_copy(deep=True)
            for (wid, _), s in sorted(self.specs.items(), key=lambda kv: kv[0][1])
            if wid == workflow_id
        ]

    async def get_question(self, question_id: str) -> ClarificationQuestion:
        q = self.questions.get(question_id)
        if q is None:
            raise NotFoundError(f"question {question_id} not found")
        return q.model_copy(deep=True)

    async def list_questions(self, workflow_id: str) -> list[ClarificationQuestion]:
        items = [q for q in self.questions.values() if q.workflow_id == workflow_id]
        return [q.model_copy(deep=True) for q in sorted(items, key=lambda q: q.question_id)]

    async def list_agent_runs(self, workflow_id: str) -> list[AgentRunRecord]:
        items = [r for r in self.agent_runs.values() if r.workflow_id == workflow_id]
        return sorted(items, key=lambda r: r.started_at)

    async def get_task(self, task_id: str) -> Task:
        task = self.tasks.get(task_id)
        if task is None:
            raise NotFoundError(f"task {task_id} not found")
        return task.model_copy(deep=True)

    async def list_tasks(self, workflow_id: str) -> list[Task]:
        items = [t for t in self.tasks.values() if t.workflow_id == workflow_id]
        return [t.model_copy(deep=True) for t in sorted(items, key=lambda t: t.created_at)]

    async def find_tasks(
        self, statuses: list[TaskStatus], updated_before: datetime | None = None
    ) -> list[Task]:
        return [
            t.model_copy(deep=True)
            for t in self.tasks.values()
            if t.status in statuses and (updated_before is None or t.updated_at < updated_before)
        ]

    async def touch_task(self, task_id: str, worker_id: str, at: datetime) -> bool:
        async with self._lock:
            task = self.tasks.get(task_id)
            if task is None or task.status != TaskStatus.RUNNING or task.worker_id != worker_id:
                return False
            task.heartbeat_at = at
            return True

    async def get_workspace(self, workspace_id: str) -> Workspace:
        ws = self.workspaces.get(workspace_id)
        if ws is None:
            raise NotFoundError(f"workspace {workspace_id} not found")
        return ws.model_copy(deep=True)

    async def list_workspaces(self, workflow_id: str | None = None) -> list[Workspace]:
        return [
            w.model_copy(deep=True)
            for w in sorted(self.workspaces.values(), key=lambda w: w.created_at)
            if workflow_id is None or w.workflow_id == workflow_id
        ]

    async def list_a2a_messages(self, workflow_id: str) -> list[A2AMessage]:
        items = [m for m in self.a2a.values() if m.workflow_id == workflow_id]
        return sorted(items, key=lambda m: m.created_at)

    async def list_events(
        self, workflow_id: str, after_seq: int = 0, limit: int = 500
    ) -> list[StoredEvent]:
        items = [
            se
            for se, _ in self.events
            if se.event.workflow_id == workflow_id and se.seq > after_seq
        ]
        return sorted(items, key=lambda se: se.seq)[:limit]

    async def fetch_unpublished_events(self, limit: int = 100) -> list[Event]:
        return [se.event for se, published in self.events if not published][:limit]

    async def mark_events_published(self, event_ids: list[str]) -> None:
        ids = set(event_ids)
        self.events = [(se, published or se.event.event_id in ids) for se, published in self.events]

    async def ping(self) -> None:
        return None
