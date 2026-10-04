"""In-memory WorkflowStore with the same semantics as the Mongo store. Used by tests."""

from __future__ import annotations

import asyncio

from forgeflow.core.errors import ConcurrencyConflict, NotFoundError
from forgeflow.platform.state.store import Commit, StoredEvent
from forgeflow.schemas.events import Event
from forgeflow.schemas.requirement import ClarificationQuestion, RequirementSpecification
from forgeflow.schemas.workflow import AgentRunRecord, Workflow


class InMemoryWorkflowStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.workflows: dict[str, Workflow] = {}
        self.specs: dict[tuple[str, int], RequirementSpecification] = {}
        self.questions: dict[str, ClarificationQuestion] = {}
        self.agent_runs: dict[str, AgentRunRecord] = {}
        self.events: list[tuple[StoredEvent, bool]] = []

    async def commit(self, change: Commit) -> Workflow:
        async with self._lock:
            wf = change.workflow
            existing = self.workflows.get(wf.workflow_id)
            if change.expected_revision is None:
                if existing is not None:
                    raise ConcurrencyConflict(f"workflow {wf.workflow_id} already exists")
                base_revision, base_seq = 0, 0
            else:
                if existing is None:
                    raise NotFoundError(wf.workflow_id)
                if existing.revision != change.expected_revision:
                    raise ConcurrencyConflict(f"workflow {wf.workflow_id} changed concurrently")
                base_revision, base_seq = existing.revision, existing.event_seq
            stored = wf.model_copy(
                update={"revision": base_revision + 1, "event_seq": base_seq + len(change.events)},
                deep=True,
            )
            self.workflows[wf.workflow_id] = stored
            for spec in change.specifications:
                self.specs[(spec.workflow_id, spec.version)] = spec.model_copy(deep=True)
            for question in change.questions:
                self.questions[question.question_id] = question.model_copy(deep=True)
            for run in change.agent_runs:
                self.agent_runs[run.run_id] = run.model_copy(deep=True)
            for offset, event in enumerate(change.events, start=1):
                self.events.append((StoredEvent(seq=base_seq + offset, event=event), False))
            return stored.model_copy(deep=True)

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
