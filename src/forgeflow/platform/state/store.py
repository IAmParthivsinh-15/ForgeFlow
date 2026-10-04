"""Durable workflow state interface.

All state changes go through `commit()`, a unit of work that atomically:
- writes the workflow (guarded by its optimistic-concurrency `revision`),
- inserts specification versions, upserts clarification questions and agent runs,
- appends events to the outbox with a per-workflow sequence number.

Outbox events are later relayed to Kafka (spec section 136).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from forgeflow.schemas.events import Event
from forgeflow.schemas.requirement import ClarificationQuestion, RequirementSpecification
from forgeflow.schemas.workflow import AgentRunRecord, Workflow


@dataclass
class Commit:
    workflow: Workflow
    # Revision the caller loaded. None means "insert a new workflow".
    expected_revision: int | None
    specifications: list[RequirementSpecification] = field(default_factory=list)
    questions: list[ClarificationQuestion] = field(default_factory=list)
    agent_runs: list[AgentRunRecord] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)


@dataclass(frozen=True)
class StoredEvent:
    seq: int
    event: Event


class WorkflowStore(Protocol):
    async def commit(self, change: Commit) -> Workflow:
        """Apply atomically. Raises ConcurrencyConflict if the revision moved."""
        ...

    async def get_workflow(self, workflow_id: str) -> Workflow: ...

    async def list_workflows(self, limit: int = 50) -> list[Workflow]: ...

    async def get_specification(
        self, workflow_id: str, version: int | None = None
    ) -> RequirementSpecification | None: ...

    async def list_specifications(self, workflow_id: str) -> list[RequirementSpecification]: ...

    async def get_question(self, question_id: str) -> ClarificationQuestion: ...

    async def list_questions(self, workflow_id: str) -> list[ClarificationQuestion]: ...

    async def list_agent_runs(self, workflow_id: str) -> list[AgentRunRecord]: ...

    async def list_events(
        self, workflow_id: str, after_seq: int = 0, limit: int = 500
    ) -> list[StoredEvent]: ...

    async def fetch_unpublished_events(self, limit: int = 100) -> list[Event]: ...

    async def mark_events_published(self, event_ids: list[str]) -> None: ...

    async def ping(self) -> None: ...
