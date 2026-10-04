"""Durable workflow state interface.

All state changes go through `commit()`, a unit of work that atomically:
- inserts or updates the workflow (guarded by its optimistic-concurrency `revision`),
- inserts or updates tasks (each guarded by its own `revision`),
- inserts specification versions, upserts questions, agent runs and workspaces,
- appends events to the outbox with a per-workflow sequence number.

Outbox events are later relayed to Kafka (spec section 136).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from forgeflow.schemas.events import Event
from forgeflow.schemas.requirement import ClarificationQuestion, RequirementSpecification
from forgeflow.schemas.task import Task, TaskStatus, Workspace
from forgeflow.schemas.verification import A2AMessage
from forgeflow.schemas.workflow import AgentRunRecord, Workflow


@dataclass
class TaskWrite:
    task: Task
    # Revision the caller loaded. None means "insert a new task".
    expected_revision: int | None


@dataclass
class Commit:
    workflow: Workflow | None = None
    # Revision the caller loaded; ignored when create_workflow is True.
    expected_revision: int | None = None
    create_workflow: bool = False
    specifications: list[RequirementSpecification] = field(default_factory=list)
    questions: list[ClarificationQuestion] = field(default_factory=list)
    agent_runs: list[AgentRunRecord] = field(default_factory=list)
    tasks: list[TaskWrite] = field(default_factory=list)
    workspaces: list[Workspace] = field(default_factory=list)
    a2a_messages: list[A2AMessage] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)


@dataclass
class CommitResult:
    workflow: Workflow | None
    tasks: dict[str, Task]


@dataclass(frozen=True)
class StoredEvent:
    seq: int
    event: Event


class WorkflowStore(Protocol):
    async def commit(self, change: Commit) -> CommitResult:
        """Apply atomically. Raises ConcurrencyConflict if any revision moved."""
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

    async def get_task(self, task_id: str) -> Task: ...

    async def list_tasks(self, workflow_id: str) -> list[Task]: ...

    async def find_tasks(
        self, statuses: list[TaskStatus], updated_before: datetime | None = None
    ) -> list[Task]: ...

    async def touch_task(self, task_id: str, worker_id: str, at: datetime) -> bool:
        """Heartbeat: set heartbeat_at if the task is still RUNNING on this worker.

        Does not bump the revision. Returns False if the task is no longer ours.
        """
        ...

    async def get_workspace(self, workspace_id: str) -> Workspace: ...

    async def list_workspaces(self, workflow_id: str | None = None) -> list[Workspace]: ...

    async def list_a2a_messages(self, workflow_id: str) -> list[A2AMessage]: ...

    async def list_events(
        self, workflow_id: str, after_seq: int = 0, limit: int = 500
    ) -> list[StoredEvent]: ...

    async def fetch_unpublished_events(self, limit: int = 100) -> list[Event]: ...

    async def mark_events_published(self, event_ids: list[str]) -> None: ...

    async def ping(self) -> None: ...
