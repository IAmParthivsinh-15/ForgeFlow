"""Knowledge service: repository indexing, history indexing and retrieval.

Used primarily by the Developer (planning and repair) and CI agents (spec section 101).
Elasticsearch being down never fails a workflow: calls degrade to "unavailable",
are retried after a back-off, and agents are told history search is unavailable.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from forgeflow.core.config import Settings
from forgeflow.core.ids import utcnow
from forgeflow.core.logging import log_event
from forgeflow.extensibility.store import DocumentStore
from forgeflow.knowledge.chunking import MAX_FILES, chunk_file, indexable
from forgeflow.knowledge.documents import task_documents
from forgeflow.knowledge.embeddings import Embedder
from forgeflow.knowledge.index import KINDS, Hit, KnowledgeIndex, KnowledgeUnavailable
from forgeflow.schemas.task import Task, TaskStatus
from forgeflow.schemas.workflow import AgentRunRecord
from forgeflow.tools.git.client import GitClient

logger = logging.getLogger(__name__)
BACKOFF_SECONDS = 30
EMBEDDED_KINDS = ("code", "failures")


class KnowledgeService:
    def __init__(
        self,
        index: KnowledgeIndex | None,
        documents: DocumentStore,
        git: GitClient,
        settings: Settings,
        embedder: Embedder | None = None,
    ) -> None:
        self.index = index
        self.documents = documents
        self.git = git
        self.settings = settings
        self.embedder = embedder
        self._down_until = 0.0
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return self.index is not None

    def available(self) -> bool:
        return self.index is not None and time.monotonic() >= self._down_until

    def _failed(self, exc: Exception) -> None:
        self._down_until = time.monotonic() + BACKOFF_SECONDS
        self.last_error = str(exc)[:300]
        log_event(logger, "knowledge index unavailable", logging.WARNING, error=self.last_error)

    async def _embed(self, kind: str, docs: list[dict[str, Any]]) -> None:
        if self.embedder is None or kind not in EMBEDDED_KINDS or not docs or self.index is None:
            return
        try:
            vectors = await self.embedder.embed(
                [f"{d.get('title', '')}\n{d.get('text', '')}" for d in docs]
            )
        except KnowledgeUnavailable as exc:
            log_event(
                logger,
                "embedding failed; indexing without vectors",
                logging.WARNING,
                error=str(exc)[:200],
            )
            return
        if vectors:
            await self.index.ensure_vectors(kind, len(vectors[0]))
            for doc, vector in zip(docs, vectors, strict=False):
                doc["embedding"] = vector

    async def _query_vector(self, query: str) -> list[float] | None:
        if self.embedder is None:
            return None
        try:
            return (await self.embedder.embed([query]))[0]
        except (KnowledgeUnavailable, IndexError):
            return None

    # -------------------------------------------------------------------- status

    async def status(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "enabled": self.enabled,
            "available": False,
            "embeddings": self.embedder.model if self.embedder else None,
            "counts": {},
            "repositories": await self.documents.find(
                "knowledge_repositories", sort="-indexed_at", limit=50
            ),
            "last_error": self.last_error,
        }
        if self.index is None:
            return info
        if not await self.index.ping():
            info["last_error"] = self.last_error or "Elasticsearch did not respond"
            return info
        info["available"] = True
        try:
            info["counts"] = {k: await self.index.count(k) for k in KINDS}
        except KnowledgeUnavailable as exc:
            info["last_error"] = str(exc)[:300]
        return info

    # ---------------------------------------------------------------- repository

    async def index_repository(
        self, repository_id: str, repo: Path, commit: str, branch: str | None = None
    ) -> dict[str, Any] | None:
        """Index the repository at `commit`. Skips work if that commit is already indexed."""
        if not self.available() or self.index is None:
            return None
        marker = await self.documents.get("knowledge_repositories", repository_id)
        if marker and marker.get("commit") == commit and marker.get("status") == "indexed":
            return marker
        started = time.perf_counter()
        try:
            files = [
                (path, size)
                for path, size in await self.git.tree_files(repo, commit)
                if indexable(path, size, self.settings.max_index_file_bytes)
            ][:MAX_FILES]
            blobs = await self.git.read_blobs(repo, commit, [p for p, _ in files])
            docs = []
            for path, raw in blobs.items():
                if b"\0" in raw[:8000]:
                    continue  # binary
                for chunk in chunk_file(path, raw.decode("utf-8", errors="replace")):
                    title = f"{path}:{chunk.start_line}-{chunk.end_line}"
                    docs.append(
                        {
                            "id": f"{repository_id}:{path}:{chunk.start_line}",
                            "repository_id": repository_id,
                            "path": path,
                            "language": chunk.language,
                            "category": chunk.category,
                            "symbol": chunk.symbol,
                            "commit": commit,
                            "branch": branch,
                            "start_line": chunk.start_line,
                            "end_line": chunk.end_line,
                            "title": f"{title} {chunk.symbol}" if chunk.symbol else title,
                            "text": chunk.text,
                            "timestamp": utcnow().isoformat(),
                        }
                    )
            await self._embed("code", docs)
            await self.index.put_many("code", docs)
            # Keep only the current commit's chunks for this repository.
            await self.index.delete_where(
                "code", {"repository_id": repository_id}, keep={"commit": commit}
            )
        except KnowledgeUnavailable as exc:
            self._failed(exc)
            return None
        summary = {
            "repository_id": repository_id,
            "commit": commit,
            "branch": branch,
            "files": len(blobs),
            "chunks": len(docs),
            "embeddings": bool(self.embedder),
            "status": "indexed",
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "indexed_at": utcnow(),
        }
        await self.documents.put("knowledge_repositories", summary)
        log_event(
            logger,
            "repository indexed",
            repository_id=repository_id,
            commit=commit,
            files=summary["files"],
            chunks=summary["chunks"],
            duration_ms=summary["duration_ms"],
        )
        return summary

    # ------------------------------------------------------------------- history

    async def index_task(
        self, task: Task, repository_id: str, commit: str | None, runs: list[AgentRunRecord]
    ) -> int:
        if not self.available() or self.index is None:
            return 0
        docs = task_documents(task, repository_id, commit, runs)
        written = 0
        try:
            for kind, items in docs.items():
                if items:
                    await self._embed(kind, items)
                    await self.index.put_many(kind, items)
                    written += len(items)
        except KnowledgeUnavailable as exc:
            self._failed(exc)
        return written

    async def mark_resolved(self, workflow_id: str, tasks: list[Task]) -> int:
        """A workflow completed: failures from earlier rounds whose stage later passed were
        fixed by the repairs in between. Record how, for the next agent that sees them."""
        if not self.available() or self.index is None:
            return 0
        verification = [t for t in tasks if t.kind in ("review", "security", "qa", "ci")]
        repairs = {t.round: t for t in tasks if t.kind == "repair" and t.result}
        resolved = 0
        try:
            hits = await self.index.find("failures", {"workflow_id": workflow_id}, size=500)
            for hit in hits:
                src = hit.source
                if src.get("resolved") or src.get("source") != "verification":
                    continue
                later = [
                    t
                    for t in verification
                    if t.kind == src.get("stage")
                    and t.round > int(src.get("round") or 0)
                    and t.status == TaskStatus.COMPLETED
                    and t.result
                    and not t.result.blocking
                ]
                if not later:
                    continue
                repair = repairs.get(int(src.get("round") or 0))
                fix = (
                    repair.result.summary if repair and repair.result else "fixed in a later round"
                )
                files = ", ".join(
                    (repair.result.files_changed if repair and repair.result else [])[:10]
                )
                await self.index.update(
                    "failures",
                    hit.id,
                    {
                        "resolved": True,
                        "status": "resolved",
                        "resolution": f"{fix}" + (f" (files: {files})" if files else ""),
                    },
                )
                resolved += 1
        except KnowledgeUnavailable as exc:
            self._failed(exc)
        return resolved

    # ----------------------------------------------------------------- retrieval

    async def search(
        self,
        query: str,
        kinds: list[str] | None = None,
        filters: dict[str, Any] | None = None,
        size: int = 10,
    ) -> list[Hit]:
        if not query.strip() or not self.available() or self.index is None:
            return []
        chosen = [k for k in (kinds or list(KINDS)) if k in KINDS] or list(KINDS)
        vector = await self._query_vector(query)
        try:
            return await self.index.search(chosen, query, filters, size, vector)
        except KnowledgeUnavailable as exc:
            self._failed(exc)
            return []

    async def similar_failures(
        self,
        text: str,
        repository_id: str | None,
        exclude_workflow: str | None = None,
        size: int = 3,
    ) -> list[Hit]:
        filters = {"repository_id": repository_id} if repository_id else None
        hits = await self.search(text[:2000], ["failures", "builds"], filters, size * 3)
        return [h for h in hits if h.source.get("workflow_id") != exclude_workflow][:size]

    async def search_code(self, query: str, repository_id: str, size: int = 8) -> list[Hit]:
        return await self.search(query, ["code"], {"repository_id": repository_id}, size)


def format_hits(hits: list[Hit], max_chars: int = 600) -> str:
    """Compact rendering for agent tools and prompts (retrieved text is data, not orders)."""
    if not hits:
        return "No matching history."
    lines = []
    for h in hits:
        s = h.source
        where = s.get("path") or s.get("stage") or h.kind
        header = f"[{h.kind}] {s.get('title', h.id)}"
        meta = ", ".join(
            str(v)
            for v in (
                where,
                s.get("status"),
                f"workflow {s['workflow_id']}" if s.get("workflow_id") else None,
                s.get("timestamp", "")[:10] if isinstance(s.get("timestamp"), str) else None,
            )
            if v
        )
        body = (s.get("text") or "")[:max_chars]
        lines.append(f"{header} ({meta})\n{body}")
        if s.get("resolution"):
            lines.append(f"  resolved by: {s['resolution'][:300]}")
    return "\n\n".join(lines)
