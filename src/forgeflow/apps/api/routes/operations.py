"""Knowledge search, evidence artifacts and deployment checks (spec sections 36-37, 155, 157)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from forgeflow.apps.api.deps import ContainerDep
from forgeflow.core.errors import ValidationFailed
from forgeflow.knowledge.index import KINDS
from forgeflow.schemas.operations import Artifact, DeploymentCheck

router = APIRouter(prefix="/api/v1", tags=["operations"])


def _owner(c: Any) -> str:
    return c.settings.local_user_id


# ------------------------------------------------------------------ knowledge


class KnowledgeHit(BaseModel):
    kind: str
    id: str
    score: float
    title: str
    text: str
    path: str | None = None
    workflow_id: str | None = None
    status: str | None = None
    severity: str | None = None
    resolution: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    timestamp: str | None = None


@router.get("/knowledge/status")
async def knowledge_status(c: ContainerDep) -> dict[str, Any]:
    if c.knowledge is None:
        return {"enabled": False, "available": False}
    return await c.knowledge.status()


@router.get("/knowledge/search")
async def knowledge_search(
    c: ContainerDep,
    q: str,
    kind: str | None = None,
    project_id: str | None = None,
    size: int = 20,
) -> list[KnowledgeHit]:
    """'Have we seen this exception before?' (spec section 36)."""
    if c.knowledge is None:
        return []
    kinds = [kind] if kind in KINDS else None
    filters = {"repository_id": project_id} if project_id else None
    hits = await c.knowledge.search(q, kinds, filters, size=max(1, min(size, 50)))
    out = []
    for h in hits:
        src = h.source
        ts = src.get("timestamp")
        out.append(
            KnowledgeHit(
                kind=h.kind,
                id=h.id,
                score=round(h.score, 4),
                title=str(src.get("title") or h.id),
                text=str(src.get("text") or "")[:1500],
                path=src.get("path"),
                workflow_id=src.get("workflow_id"),
                status=src.get("status"),
                severity=src.get("severity"),
                resolution=src.get("resolution") or None,
                start_line=src.get("start_line"),
                end_line=src.get("end_line"),
                timestamp=str(ts) if ts else None,
            )
        )
    return out


class Reindex(BaseModel):
    force: bool = Field(default=False, description="Re-index even if this commit was indexed")


@router.post("/knowledge/projects/{project_id}/index")
async def reindex_project(project_id: str, body: Reindex, c: ContainerDep) -> dict[str, Any]:
    if c.knowledge is None or not c.knowledge.enabled:
        raise ValidationFailed("search is not configured (set ELASTICSEARCH_URL)")
    if c.extensibility is None:
        raise ValidationFailed("projects are not available")
    project = await c.extensibility.projects.get(_owner(c), project_id)
    repo = c.worktrees.repository(project.repository_path)
    sha, ref = await c.git.head(repo)
    if body.force:
        await c.knowledge.documents.delete("knowledge_repositories", project.project_id)
    summary = await c.knowledge.index_repository(project.project_id, repo, sha, ref)
    if summary is None:
        raise ValidationFailed(c.knowledge.last_error or "Elasticsearch is unavailable")
    return summary


# ------------------------------------------------------------------ artifacts


@router.get("/workflows/{workflow_id}/artifacts")
async def list_artifacts(workflow_id: str, c: ContainerDep) -> list[Artifact]:
    if c.artifacts is None:
        return []
    return await c.artifacts.list_for(workflow_id)


@router.get("/artifacts/{artifact_id}")
async def get_artifact(artifact_id: str, c: ContainerDep) -> FileResponse:
    if c.artifacts is None:
        raise ValidationFailed("artifacts are not configured")
    artifact, path = await c.artifacts.get(artifact_id)
    return FileResponse(
        path,
        media_type=artifact.content_type,
        filename=f"{artifact.artifact_id}{path.suffix}",
        content_disposition_type="inline",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"},
    )


# ---------------------------------------------------------------- deployments


@router.post("/projects/{project_id}/deployments/verify", status_code=201)
async def verify_deployment(project_id: str, c: ContainerDep) -> DeploymentCheck:
    assert c.deployments is not None
    return await c.deployments.verify(_owner(c), project_id)


@router.get("/deployments")
async def list_deployment_checks(
    c: ContainerDep, project_id: str | None = None
) -> list[DeploymentCheck]:
    assert c.deployments is not None
    return await c.deployments.list_checks(_owner(c), project_id)


@router.get("/deployments/{check_id}")
async def get_deployment_check(check_id: str, c: ContainerDep) -> DeploymentCheck:
    assert c.deployments is not None
    return await c.deployments.get(_owner(c), check_id)


class RollbackRequest(BaseModel):
    confirm: Literal[True] = Field(description="Explicitly confirm the rollback request")


@router.post("/deployments/{check_id}/rollback", status_code=202)
async def request_rollback(
    check_id: str, body: RollbackRequest, c: ContainerDep
) -> DeploymentCheck:
    """Creates an approval request; Argo CD rolls back only after it is approved."""
    assert c.deployments is not None
    return await c.deployments.request_rollback(_owner(c), check_id)
