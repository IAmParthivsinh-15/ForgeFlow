"""L4 autonomy contracts (additional.md): decision contract, runs, plans, evidence.

A *run* is one attempt by the commander to own one source item (e.g. a GitHub issue)
from trigger to a terminal decision. Everything a reviewer needs is keyed by its
`trace_id`: the contract and profile versions, the saved `task_plan.json`, worker
lifecycle, decisions with evidence, closure verification, counters, stop/resume.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# ------------------------------------------------------------------- contract


class ActionProfile(BaseModel):
    id: str
    auto: list[str] = Field(default_factory=list)
    ask: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)
    max_comments_per_run: int = Field(default=3, ge=0)

    def tier(self, action: str) -> Literal["auto", "ask", "deny"]:
        """DENY wins, then ASK; anything not explicitly authorised asks (spec 2.5)."""
        if action in self.deny:
            return "deny"
        if action in self.ask:
            return "ask"
        if action in self.auto:
            return "auto"
        return "ask"


class Budgets(BaseModel):
    max_tokens: int = Field(ge=0)
    max_cost_usd: float = Field(ge=0)
    max_runtime_seconds: int = Field(gt=0)
    max_retries: int = Field(ge=0)


class Limits(BaseModel):
    max_workers: int = Field(ge=1, le=20)
    allowed_models: list[str] = Field(min_length=1)
    budgets: Budgets
    model_prices_per_1k_tokens: dict[str, float] = Field(default_factory=dict)


class CircuitBreakers(BaseModel):
    max_failed_tasks: int = Field(ge=1)
    max_provider_failures: int = Field(ge=1)
    max_capability_errors: int = Field(ge=1)
    max_active_runs: int = Field(ge=1)


class SourceConfig(BaseModel):
    system: Literal["github"] = "github"
    repository: str = ""
    project_repository_path: str = ""
    connector_id: str = ""
    webhook_events: list[str] = Field(default_factory=lambda: ["issues"])
    sweep_interval_seconds: int = Field(default=300, ge=10)


class Eligibility(BaseModel):
    required_labels: list[str] = Field(default_factory=list)
    any_of_labels: list[str] = Field(default_factory=list)
    excluded_labels: list[str] = Field(default_factory=list)
    states: list[str] = Field(default_factory=lambda: ["open"])
    max_body_chars: int = 20000
    max_risk_level: Literal["low", "medium", "high", "critical"] = "medium"


class PlanRules(BaseModel):
    require_independent_reviewer: bool = True
    min_parallel_workers: int = Field(default=2, ge=1)
    max_replans: int = Field(default=1, ge=0)


class AlertConfig(BaseModel):
    run_stuck_after_seconds: int = 3600
    webhook_url_env: str = "FORGEFLOW_ALERT_WEBHOOK_URL"


class DecisionContract(BaseModel):
    version: str
    policy_version: str
    responsibility: str
    mode: Literal["observe", "autonomous"] = "observe"
    source: SourceConfig
    eligibility: Eligibility
    source_authority: list[str]
    action_profile: ActionProfile
    limits: Limits
    circuit_breakers: CircuitBreakers
    plan: PlanRules = Field(default_factory=PlanRules)
    closure_checks: list[str] = Field(min_length=1)
    escalation_conditions: dict[str, str]
    alerts: AlertConfig = Field(default_factory=AlertConfig)
    hash: str = ""

    @model_validator(mode="after")
    def _consistent(self) -> DecisionContract:
        overlap = (set(self.action_profile.auto) | set(self.action_profile.ask)) & set(
            self.action_profile.deny
        )
        if overlap:
            raise ValueError(f"actions cannot be both allowed and denied: {sorted(overlap)}")
        for required in ("emergency_stop", "circuit_breaker", "retry_limit", "verification_failed"):
            if required not in self.escalation_conditions:
                raise ValueError(f"escalation condition '{required}' must be defined")
        return self


# ------------------------------------------------------------------------ runs


class RunStatus(StrEnum):
    RECEIVED = "RECEIVED"
    PLANNING = "PLANNING"
    PLANNED = "PLANNED"  # plan validated and saved; workers may start
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    RETRYING = "RETRYING"
    PAUSED_BY_GUARDRAIL = "PAUSED_BY_GUARDRAIL"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    RESOLVED = "RESOLVED"
    CLOSED_NO_ACTION = "CLOSED_NO_ACTION"
    ESCALATED = "ESCALATED"


TERMINAL_RUN = frozenset({RunStatus.RESOLVED, RunStatus.CLOSED_NO_ACTION, RunStatus.ESCALATED})
# No worker may start in these states.
NO_SPAWN = frozenset(
    {
        RunStatus.RECEIVED,
        RunStatus.PLANNING,
        RunStatus.PAUSED_BY_GUARDRAIL,
        RunStatus.STOPPING,
        RunStatus.STOPPED,
        *TERMINAL_RUN,
    }
)
Decision = Literal["RESOLVE", "RETRY", "ESCALATE", "CLOSE_NO_ACTION"]


class SourceItem(BaseModel):
    system: Literal["github"] = "github"
    repository: str
    item_id: str = Field(description="e.g. issue_456")
    number: int
    url: str
    title: str
    body: str = ""
    labels: list[str] = Field(default_factory=list)
    state: str = "open"
    updated_at: str | None = None
    content_hash: str = ""


class PlannedModel(BaseModel):
    provider: str
    model: str
    version: str = "pinned"

    @property
    def ref(self) -> str:
        return f"{self.provider}/{self.model}"


class PlanSubtask(BaseModel):
    task_id: str
    purpose: str
    role: Literal["prepare", "do", "review", "learn", "supporting", "verify"]
    worker_role: str
    model: str = Field(description="provider/model, or 'none' for deterministic steps")
    expected_output: str
    dependencies: list[str] = Field(default_factory=list)
    parallel_group: str | None = None


class TaskPlan(BaseModel):
    """`task_plan.json` (additional.md section 3). Saved before any worker starts."""

    schema_version: Literal["1.0"] = "1.0"
    trace_id: str
    workflow_id: str
    objective: str
    source_item: dict[str, Any]
    subtasks: list[PlanSubtask] = Field(min_length=1)
    worker_count_requested: int = Field(ge=1)
    worker_count_max: int = Field(ge=1)
    parallelism_note: str = ""
    cost_estimate_usd: float = Field(ge=0)
    planned_models: list[PlannedModel]
    implementer_model: str
    reviewer_model: str
    action_profile_id: str
    policy_version: str
    contract_version: str
    planned_actions: list[str]
    budgets: dict[str, Any]
    closure_checks: list[str]
    created_at: datetime
    status: Literal["PROPOSED", "VALIDATED_AND_SAVED", "REJECTED"] = "PROPOSED"
    validation_errors: list[str] = Field(default_factory=list)
    hash: str = ""


class Counters(BaseModel):
    tokens: int = 0
    cost_usd: float = 0.0
    runtime_seconds: float = 0.0
    retries: int = 0
    failed_tasks: int = 0
    provider_failures: int = 0
    capability_errors: int = 0
    workers_spawned: int = 0
    max_parallel_observed: int = 0
    comments_posted: int = 0
    unpriced_models: list[str] = Field(default_factory=list)


class Escalation(BaseModel):
    condition: str
    rule: str
    summary: str
    decision_needed: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    attempted: list[str] = Field(default_factory=list)
    escalated_at: datetime
    delivered_to: list[str] = Field(default_factory=list)


class StopRecord(BaseModel):
    actor: str
    reason: str
    requested_at: datetime
    acknowledged_at: datetime | None = None
    stopped_at: datetime | None = None
    cancelled_tasks: list[str] = Field(default_factory=list)
    rejected_approvals: list[str] = Field(default_factory=list)
    checkpoint: dict[str, Any] = Field(default_factory=dict)


class ClosureCheck(BaseModel):
    name: str
    passed: bool
    detail: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)


class Run(BaseModel):
    run_id: str
    trace_id: str
    idempotency_key: str
    trigger: Literal["webhook", "sweep", "manual"]
    source_item: SourceItem
    status: RunStatus
    decision: Decision | None = None
    decision_reason: str | None = None
    contract_version: str
    contract_hash: str
    policy_version: str
    action_profile_id: str
    # Snapshot of the profile at run start; later contract edits do not change it.
    action_profile: ActionProfile
    mode: Literal["observe", "autonomous"]
    # Immutable snapshot taken at run start (additional.md section 4).
    limits: Limits
    circuit_breakers: CircuitBreakers
    workflow_id: str | None = None
    project_id: str | None = None
    plan: TaskPlan | None = None
    replans: int = 0
    counters: Counters = Field(default_factory=Counters)
    closure: list[ClosureCheck] = Field(default_factory=list)
    escalation: Escalation | None = None
    stop: StopRecord | None = None
    resumes: int = 0
    guardrail: str | None = None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None


class RunEvent(BaseModel):
    """Append-only evidence record (additional.md section 10)."""

    event_id: str
    trace_id: str
    seq: int
    type: str
    timestamp: datetime
    actor: str = "commander"
    data: dict[str, Any] = Field(default_factory=dict)


class LearningRecord(BaseModel):
    """Section 7: what a finished run teaches. Candidates are proposals, never applied."""

    learning_id: str
    trace_id: str
    decision: str
    outcome: str
    review_corrections: list[str] = Field(default_factory=list)
    failed_tests: list[str] = Field(default_factory=list)
    retries: int = 0
    root_causes: list[str] = Field(default_factory=list)
    human_interventions: list[dict[str, Any]] = Field(default_factory=list)
    blocked_policy_violations: list[str] = Field(default_factory=list)
    closure_mismatches: list[str] = Field(default_factory=list)
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime
