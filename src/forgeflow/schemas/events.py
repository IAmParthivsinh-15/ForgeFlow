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


class Topics:
    WORKFLOW = "forgeflow.workflow.events"
    AGENT = "forgeflow.agent.events"


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
