"""Task graph, workspace and development-agent contracts (spec sections 16-22, 50, 182-185)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from forgeflow.schemas.verification import (
    ChangeAnalysis,
    CIReport,
    QAReport,
    ReviewReport,
    SecurityReport,
)

# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    DISPATCHED = "DISPATCHED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    RETRYING = "RETRYING"


TaskKind = Literal[
    "decompose", "implement", "integrate", "repair", "review", "security", "qa", "ci"
]
VERIFICATION_KINDS: frozenset[str] = frozenset({"review", "security", "qa", "ci"})
DEVELOPMENT_KINDS: frozenset[str] = frozenset({"decompose", "implement", "integrate", "repair"})


class CheckRun(BaseModel):
    """An executed, allowlisted repository command (test/lint/build/...)."""

    kind: str
    command: str
    exit_code: int | None
    passed: bool
    duration_ms: int
    output: str = Field(default="", description="Tail of combined stdout/stderr.")
    timed_out: bool = False


class TaskResult(BaseModel):
    """Structured agent result (spec section 50)."""

    summary: str = ""
    files_changed: list[str] = Field(default_factory=list)
    commit: str | None = None
    branch: str | None = None
    checks: list[CheckRun] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    merged_tasks: list[str] = Field(default_factory=list)
    conflicts_resolved: list[str] = Field(default_factory=list)
    # Verification outcome, decided by ForgeFlow (not by the agent).
    verdict: Literal["pass", "fail", "uncertain"] | None = None
    blocking: bool = False
    blocking_reasons: list[str] = Field(default_factory=list)
    change_analysis: ChangeAnalysis | None = None
    review: ReviewReport | None = None
    security: SecurityReport | None = None
    qa: QAReport | None = None
    ci: CIReport | None = None
    a2a_messages: int = 0


class Task(BaseModel):
    task_id: str
    workflow_id: str
    key: str
    title: str
    kind: TaskKind
    agent_type: str
    instructions: str = ""
    status: TaskStatus
    dependencies: list[str] = Field(default_factory=list, description="task_ids")
    file_scope: list[str] = Field(default_factory=list)
    resource_scope: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list, description="AC ids")
    priority: int = 50
    # Verification round this task belongs to (0 = development).
    round: int = 0
    attempt: int = 0
    max_attempts: int = 2
    retryable: bool = False
    not_before: datetime | None = None
    wait_reason: str | None = None
    workspace_id: str | None = None
    worker_id: str | None = None
    result: TaskResult | None = None
    error: str | None = None
    revision: int = 0
    dispatched_at: datetime | None = None
    started_at: datetime | None = None
    heartbeat_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @property
    def terminal_failure(self) -> bool:
        return self.status == TaskStatus.FAILED and (
            not self.retryable or self.attempt >= self.max_attempts
        )


class Workspace(BaseModel):
    """Workspace metadata (spec section 22)."""

    workspace_id: str
    workflow_id: str
    task_id: str
    repository_path: str
    path: str
    branch: str
    base_commit: str
    status: Literal["active", "removed"] = "active"
    created_at: datetime


# ---------------------------------------------------------------------------
# Developer agent output: subtask decomposition
# ---------------------------------------------------------------------------


class SubtaskSpec(BaseModel):
    key: str = Field(description="Short unique id such as 'backend' or 'frontend'.")
    title: str
    instructions: str = Field(description="Concrete implementation instructions.")
    file_scope: list[str] = Field(
        min_length=1,
        description="Repository-relative paths or globs this subtask may write, "
        "e.g. 'src/auth/**'.",
    )
    depends_on: list[str] = Field(default_factory=list, description="Keys of other subtasks.")
    acceptance_criteria: list[str] = Field(default_factory=list, description="AC ids covered.")

    @field_validator("key")
    @classmethod
    def _key(cls, v: str) -> str:
        v = v.strip().lower().replace(" ", "-")
        if not v or not all(c.isalnum() or c in "-_" for c in v) or len(v) > 40:
            raise ValueError("key must be 1-40 characters of letters, digits, '-' or '_'")
        return v

    @field_validator("file_scope")
    @classmethod
    def _scope(cls, v: list[str]) -> list[str]:
        cleaned = []
        for raw in v:
            s = raw.strip().replace("\\", "/")
            while s.startswith("./"):
                s = s[2:]
            s = s.lstrip("/")
            if s:
                cleaned.append(s)
        if not cleaned:
            raise ValueError("file_scope must contain at least one path or glob")
        for s in cleaned:
            if ".." in s.split("/"):
                raise ValueError(f"file_scope must not contain '..': {s}")
        return cleaned


class DevelopmentPlan(BaseModel):
    summary: str
    subtasks: list[SubtaskSpec] = Field(min_length=1)
    notes: str = ""


# ---------------------------------------------------------------------------
# Subagent / integrator outputs
# ---------------------------------------------------------------------------


class ImplementationReport(BaseModel):
    summary: str = Field(description="What was changed and why.")
    risks: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)


class ResolutionReport(BaseModel):
    summary: str
    resolved_files: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
