"""Artifact system (spec section 155): evidence files on disk, metadata in the document store.

Files live under ARTIFACTS_ROOT/<workflow>/<task>/<artifact_id>.<ext>. Only ids
chosen by ForgeFlow appear in paths, so nothing an agent or MCP server returns
can steer where a file is written.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from forgeflow.core.errors import NotFoundError, ValidationFailed
from forgeflow.core.ids import new_id, utcnow
from forgeflow.extensibility.store import DocumentStore
from forgeflow.schemas.operations import Artifact

EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "text/plain": "txt",
    "application/json": "json",
    "application/zip": "zip",
    "text/html": "html",
}


def _segment(value: str | None) -> str:
    cleaned = "".join(c for c in (value or "none") if c.isalnum() or c in "-_.")
    return cleaned.strip(".")[:80] or "none"


class ArtifactStore:
    def __init__(self, documents: DocumentStore, root: Path, max_bytes: int = 5_000_000) -> None:
        self.documents = documents
        self.root = root
        self.max_bytes = max_bytes

    async def save(
        self,
        *,
        workflow_id: str | None,
        task_id: str | None,
        type: str,
        name: str,
        content: bytes,
        content_type: str,
        source: str = "",
    ) -> Artifact:
        if content_type not in EXTENSIONS:
            raise ValidationFailed(f"unsupported artifact type {content_type}")
        if len(content) > self.max_bytes:
            raise ValidationFailed(f"artifact exceeds {self.max_bytes} bytes")
        artifact_id = new_id("art")
        relative = (
            Path(_segment(workflow_id))
            / _segment(task_id)
            / (f"{artifact_id}.{EXTENSIONS[content_type]}")
        )
        target = self.root / relative
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, content)
        artifact = Artifact(
            artifact_id=artifact_id,
            workflow_id=workflow_id,
            task_id=task_id,
            type=type,  # type: ignore[arg-type]
            name=name[:200],
            content_type=content_type,
            path=relative.as_posix(),
            size_bytes=len(content),
            source=source[:200],
            created_at=utcnow(),
        )
        await self.documents.put("artifacts", artifact)
        return artifact

    async def get(self, artifact_id: str) -> tuple[Artifact, Path]:
        doc = await self.documents.get("artifacts", artifact_id)
        if doc is None:
            raise NotFoundError(f"artifact {artifact_id} not found")
        artifact = Artifact.model_validate(doc)
        path = (self.root / artifact.path).resolve()
        if not path.is_relative_to(self.root.resolve()) or not path.is_file():
            raise NotFoundError(f"artifact {artifact_id} file is missing")
        return artifact, path

    async def list_for(self, workflow_id: str, task_id: str | None = None) -> list[Artifact]:
        query = {"workflow_id": workflow_id, **({"task_id": task_id} if task_id else {})}
        docs = await self.documents.find("artifacts", query, sort="created_at")
        return [Artifact.model_validate(d) for d in docs]
