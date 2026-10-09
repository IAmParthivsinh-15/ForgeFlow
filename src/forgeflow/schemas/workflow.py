"""Workflow and routing documents (spec sections 18, 177)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from forgeflow.schemas.extensibility import CapabilitySnapshot
from forgeflow.schemas.verification import FinalReport


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
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    PUBLISHING = "PUBLISHING"
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
    # Commit currently being verified (integration commit, or base when nothing is developed).
    target_commit: str | None = None
    verification_round: int = 0
    repair_attempts: int = 0
    # Extra repair rounds granted by a human after the automatic limit was reached.
    extra_repairs_allowed: int = 0
    # Set when the workflow is PAUSED waiting for a human decision on open blockers.
    awaiting_decision: bool = False
    accepted_risks: list[str] = Field(default_factory=list)
    note: str | None = None
    started_at: datetime
    finished_at: datetime | None = None


class PullRequestInfo(BaseModel):
    repository: str
    number: int
    url: str
    branch: str
    base: str
    state: str = "open"
    draft: bool = False
    opened_at: datetime


class Workflow(BaseModel):
    workflow_id: str
    request: str
    repository_path: str | None = None
    project_id: str | None = None
    status: WorkflowStatus
    intake: IntakeAssessment | None = None
    requirement_version: int = 0
    clarification_round: int = 0
    route_plan: RoutePlan | None = None
    execution: ExecutionInfo | None = None
    report: FinalReport | None = None
    capability_snapshot: CapabilitySnapshot | None = None
    pull_request: PullRequestInfo | None = None
    # L4 autonomy: the run that owns this workflow (additional.md section 3).
    trace_id: str | None = None
    autonomous: bool = False
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
    # Usage reported by the provider (0 when unknown, e.g. fake agents).
    input_tokens: int = 0
    output_tokens: int = 0


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
