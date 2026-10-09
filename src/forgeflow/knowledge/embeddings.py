"""Optional embeddings for semantic search (OpenAI-compatible `/embeddings`).

Without EMBEDDING_BASE_URL / EMBEDDING_API_KEY / EMBEDDING_MODEL, search is lexical
(BM25) only. Text sent for embedding is repository code and engineering history; it
never contains ForgeFlow credentials (those are not indexed).
"""

from __future__ import annotations

import logging
from typing import Protocol

import httpx

from forgeflow.core.config import Settings
from forgeflow.knowledge.index import KnowledgeUnavailable

logger = logging.getLogger(__name__)
MAX_INPUT_CHARS = 6000


class Embedder(Protocol):
    model: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAICompatibleEmbedder:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), 64):
            batch = [t[:MAX_INPUT_CHARS] or " " for t in texts[start : start + 64]]
            try:
                response = await self.client.post(
                    "/embeddings", json={"model": self.model, "input": batch}
                )
            except httpx.HTTPError as exc:
                raise KnowledgeUnavailable(f"embeddings: {type(exc).__name__}") from exc
            if response.status_code != 200:
                raise KnowledgeUnavailable(
                    f"embeddings: HTTP {response.status_code} {response.text[:200]}"
                )
            data = sorted(response.json().get("data", []), key=lambda d: d.get("index", 0))
            vectors += [d["embedding"] for d in data]
        return vectors


def build_embedder(settings: Settings) -> Embedder | None:
    if not (
        settings.embedding_base_url and settings.embedding_api_key and settings.embedding_model
    ):
        return None
    return OpenAICompatibleEmbedder(
        settings.embedding_base_url, settings.embedding_api_key, settings.embedding_model
    )
