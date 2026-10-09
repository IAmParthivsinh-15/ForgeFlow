"""Immutable, versioned event envelope (spec sections 31, 82)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from forgeflow.core.ids import new_id, utcnow

SCHEMA_VERSION = 1


class EventType:
    WORKFLOW_CREATED = "workflow.created"
    WORKFLOW_STATUS_CHANGED = "workflow.status_changed"
    ANALYSIS_REQUESTED = "requirement.analysis_requested"
    SPECIFICATION_CREATED = "requirement.specification_created"
    CLARIFICATION_REQUESTED = "clarification.requested"
    CLARIFICATION_ANSWERED = "clarification.answered"
    WORKFLOW_ROUTED = "workflow.routed"
    WORKFLOW_FAILED = "workflow.failed"
    AGENT_RUN_COMPLETED = "agent.run.completed"
    AGENT_RUN_FAILED = "agent.run.failed"
    WORKFLOW_EXECUTION_STARTED = "workflow.execution_started"
    WORKFLOW_EXECUTION_FINISHED = "workflow.execution_finished"

    # Task lifecycle (spec sections 2.4, 94). Every task transition emits one of these.
    TASK_CREATED = "task.created"
    TASK_READY = "task.ready"
    TASK_DISPATCHED = "task.dispatched"
    TASK_STARTED = "task.started"
    TASK_PROGRESS = "task.progress"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_BLOCKED = "task.blocked"
    TASK_UNBLOCKED = "task.unblocked"
    TASK_RETRYING = "task.retrying"
    TASK_CANCELLED = "task.cancelled"
    WORKSPACE_CREATED = "workspace.created"
    CHECK_COMPLETED = "test.completed"
    INTEGRATION_CONFLICT = "integration.conflict"
    VERIFICATION_ROUND_STARTED = "verification.round_started"
    REPAIR_REQUESTED = "repair.requested"
    WORKFLOW_AWAITING_DECISION = "workflow.awaiting_decision"
    WORKFLOW_COMPLETED = "workflow.completed"
    A2A_EXCHANGE = "a2a.exchange"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"
    PULL_REQUEST_OPENED = "github.pull_request_opened"
    # A capability (MCP tool, connector operation) was used - browser actions included.
    CAPABILITY_USED = "capability.used"
    ARTIFACT_STORED = "artifact.stored"
    PREVIEW_STARTED = "preview.started"
    CI_BUILD_COMPLETED = "ci.build_completed"


class Topics:
    WORKFLOW = "forgeflow.workflow.events"
    AGENT = "forgeflow.agent.events"
    TASK = "forgeflow.task.events"


class Event(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = SCHEMA_VERSION
    event_id: str = Field(default_factory=lambda: new_id("evt"))
    event_type: str
    topic: str = Topics.WORKFLOW
    workflow_id: str
    task_id: str | None = None
    agent_id: str | None = None
    timestamp: datetime = Field(default_factory=utcnow)
    payload: dict[str, Any] = Field(default_factory=dict)
