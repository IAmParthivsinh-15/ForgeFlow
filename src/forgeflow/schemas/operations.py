"""Artifacts and deployment checks (spec sections 155, 157)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Artifact(BaseModel):
    """Evidence kept outside the database (screenshots, reports); metadata only here."""

    artifact_id: str
    workflow_id: str | None
    task_id: str | None
    type: Literal["screenshot", "test-report", "security-report", "build-log", "other"]
    name: str
    content_type: str
    path: str = Field(description="Relative to ARTIFACTS_ROOT")
    size_bytes: int
    source: str = Field(default="", description="e.g. playwright.browser_take_screenshot")
    created_at: datetime


class DeploymentProbe(BaseModel):
    name: str
    target: str
    passed: bool
    detail: str = ""
    latency_ms: int = 0


class DeploymentCheck(BaseModel):
    check_id: str
    owner_id: str
    project_id: str
    application: str
    status: Literal["healthy", "unhealthy", "unknown"]
    sync_status: str | None = None
    health_status: str | None = None
    revision: str | None = None
    # Previous successfully deployed history entry, offered for rollback.
    rollback_to: int | None = None
    probes: list[DeploymentProbe] = Field(default_factory=list)
    summary: str = ""
    rollback: Literal["not_needed", "available", "requested", "done", "rejected", "failed"] = (
        "not_needed"
    )
    rollback_detail: str | None = None
    created_at: datetime
    updated_at: datetime
