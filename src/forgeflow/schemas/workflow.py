"""Workflow and routing documents (spec sections 18, 177)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class WorkflowStatus(StrEnum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    AWAITING_CLARIFICATION = "AWAITING_CLARIFICATION"
    PLANNED = "PLANNED"
    EXECUTING = "EXECUTING"
    INTEGRATING = "INTEGRATING"
    REVIEWING = "REVIEWING"
    TESTING = "TESTING"
    CI = "CI"
    DEPLOYING = "DEPLOYING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"


Intent = Literal[
    "feature", "bugfix", "code_review", "qa", "ci", "security", "investigation", "other"
]


class IntakeAssessment(BaseModel):
    """Orchestrator's first-pass understanding of the request."""

    intent: Intent
    summary: str
    is_engineering_request: bool = True
    repository_required: bool = True
    notes: str = ""


Capability = Literal["development", "code_review", "security", "qa", "ci"]


class RouteStage(BaseModel):
    stage_id: str
    capability: Capability
    agent: str
    depends_on: list[str] = Field(default_factory=list)
    status: Literal["planned", "running", "completed", "failed", "skipped"] = "planned"
    implemented: bool = False
    reason: str


class RoutePlan(BaseModel):
    stages: list[RouteStage]
    skipped: list[Capability]
    rationale: str


class ExecutionInfo(BaseModel):
    """Git context of a workflow's code-writing execution."""

    base_commit: str
    base_ref: str
    integration_branch: str | None = None
    integration_commit: str | None = None
    note: str | None = None
    started_at: datetime
    finished_at: datetime | None = None


class Workflow(BaseModel):
    workflow_id: str
    request: str
    repository_path: str | None = None
    status: WorkflowStatus
    intake: IntakeAssessment | None = None
    requirement_version: int = 0
    clarification_round: int = 0
    route_plan: RoutePlan | None = None
    execution: ExecutionInfo | None = None
    error: str | None = None
    # Optimistic-concurrency revision; maintained by the store.
    revision: int = 0
    created_at: datetime
    updated_at: datetime


class ProviderAttempt(BaseModel):
    provider: str
    model: str
    status: Literal["succeeded", "failed"]
    latency_ms: int
    fallback_reason: str | None = None


class AgentRunRecord(BaseModel):
    """Spec section 81 'Agent Run' plus provider-fallback telemetry (section 141)."""

    run_id: str
    workflow_id: str
    task_id: str | None = None
    agent_type: str
    prompt_version: str
    status: Literal["completed", "failed"]
    attempts: list[ProviderAttempt]
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    started_at: datetime
    completed_at: datetime
