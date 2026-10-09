"""Search index backends: Elasticsearch (REST over httpx) and an in-memory twin for tests.

One index per document kind (spec section 36):

    <prefix>-code          repository chunks (README, source, tests, config, manifests)
    <prefix>-failures      blocking verification findings and failed tasks
    <prefix>-builds        CI builds
    <prefix>-tests         executed repository checks (test/lint/build)
    <prefix>-reviews       code review findings
    <prefix>-security      security findings
    <prefix>-agent-events  agent runs

Search is BM25 over `title`, `symbol`, `path` and `text`. When documents carry an
`embedding`, a kNN query runs as well and both rankings are fused with reciprocal
rank fusion (client-side, so it needs no paid Elasticsearch feature).
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

KINDS = ("code", "failures", "builds", "tests", "reviews", "security", "agent-events")
HISTORY_KINDS = tuple(k for k in KINDS if k != "code")
RRF_K = 60

_KEYWORDS = (
    "repository_id",
    "workflow_id",
    "task_id",
    "stage",
    "language",
    "path",
    "status",
    "severity",
    "category",
    "agent",
    "commit",
    "branch",
    "source",
    "signature",
)
MAPPING: dict[str, Any] = {
    "dynamic": False,
    "properties": {
        **{k: {"type": "keyword"} for k in _KEYWORDS},
        "title": {"type": "text"},
        "symbol": {"type": "text", "fields": {"raw": {"type": "keyword"}}},
        "text": {"type": "text"},
        "resolution": {"type": "text"},
        "resolved": {"type": "boolean"},
        "round": {"type": "integer"},
        "start_line": {"type": "integer"},
        "end_line": {"type": "integer"},
        "timestamp": {"type": "date"},
    },
}


class KnowledgeUnavailable(Exception):
    """The search backend cannot be reached or rejected the request."""


@dataclass
class Hit:
    kind: str
    id: str
    score: float
    source: dict[str, Any]


class KnowledgeIndex(Protocol):
    async def ping(self) -> bool: ...

    async def ensure(self, kind: str) -> None: ...

    async def ensure_vectors(self, kind: str, dims: int) -> None: ...

    async def put_many(self, kind: str, docs: list[dict[str, Any]]) -> None: ...

    async def update(self, kind: str, doc_id: str, fields: dict[str, Any]) -> None: ...

    async def delete_where(
        self, kind: str, match: dict[str, Any], keep: dict[str, Any] | None = None
    ) -> None: ...

    async def find(self, kind: str, match: dict[str, Any], size: int = 100) -> list[Hit]: ...

    async def search(
        self,
        kinds: list[str],
        query: str,
        filters: dict[str, Any] | None = None,
        size: int = 10,
        vector: list[float] | None = None,
    ) -> list[Hit]: ...

    async def count(self, kind: str) -> int: ...

    async def close(self) -> None: ...


def fuse(rankings: list[list[Hit]], size: int) -> list[Hit]:
    """Reciprocal rank fusion of several rankings of the same documents."""
    scores: dict[tuple[str, str], float] = {}
    hits: dict[tuple[str, str], Hit] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking):
            key = (hit.kind, hit.id)
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank + 1)
            hits.setdefault(key, hit)
    ordered = sorted(scores, key=lambda k: scores[k], reverse=True)[:size]
    return [Hit(k[0], k[1], scores[k], hits[k].source) for k in ordered]


# --------------------------------------------------------------------------- memory

_TOKEN = re.compile(r"[a-z0-9_]+")


def tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if len(t) > 1]


def _matches(doc: dict[str, Any], match: dict[str, Any]) -> bool:
    for key, value in match.items():
        actual = doc.get(key)
        if isinstance(value, list):
            if actual not in value:
                return False
        elif actual != value:
            return False
    return True


class InMemoryKnowledgeIndex:
    """BM25-like ranking over in-process documents. Used by tests and as a reference."""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, dict[str, Any]]] = {k: {} for k in KINDS}

    async def ping(self) -> bool:
        return True

    async def ensure(self, kind: str) -> None:
        self.docs.setdefault(kind, {})

    async def ensure_vectors(self, kind: str, dims: int) -> None:
        return None

    async def put_many(self, kind: str, docs: list[dict[str, Any]]) -> None:
        for doc in docs:
            self.docs[kind][doc["id"]] = dict(doc)

    async def update(self, kind: str, doc_id: str, fields: dict[str, Any]) -> None:
        if doc_id in self.docs[kind]:
            self.docs[kind][doc_id].update(fields)

    async def delete_where(
        self, kind: str, match: dict[str, Any], keep: dict[str, Any] | None = None
    ) -> None:
        for doc_id, doc in list(self.docs[kind].items()):
            if _matches(doc, match) and not (keep and _matches(doc, keep)):
                del self.docs[kind][doc_id]

    async def find(self, kind: str, match: dict[str, Any], size: int = 100) -> list[Hit]:
        found = [d for d in self.docs[kind].values() if _matches(d, match)]
        return [Hit(kind, d["id"], 1.0, d) for d in found[:size]]

    async def search(
        self,
        kinds: list[str],
        query: str,
        filters: dict[str, Any] | None = None,
        size: int = 10,
        vector: list[float] | None = None,
    ) -> list[Hit]:
        terms = tokens(query)
        candidates = [
            (kind, d)
            for kind in kinds
            for d in self.docs.get(kind, {}).values()
            if _matches(d, filters or {})
        ]
        total = len(candidates) or 1
        frequency: Counter[str] = Counter()
        bags = []
        for _, doc in candidates:
            bag = Counter(
                tokens(
                    " ".join(
                        str(doc.get(f, ""))
                        for f in ("title", "symbol", "path", "text", "resolution")
                    )
                )
            )
            bags.append(bag)
            frequency.update(set(bag))
        lexical = []
        for (kind, doc), bag in zip(candidates, bags, strict=True):
            score = sum(
                (bag[t] / (bag[t] + 1.2)) * math.log(1 + total / (1 + frequency[t]))
                for t in terms
                if t in bag
            )
            if score > 0:
                lexical.append(Hit(kind, doc["id"], score, doc))
        lexical.sort(key=lambda h: h.score, reverse=True)
        if vector is None:
            return lexical[:size]
        semantic = [
            Hit(kind, d["id"], _cosine(vector, d["embedding"]), d)
            for kind, d in candidates
            if d.get("embedding")
        ]
        semantic.sort(key=lambda h: h.score, reverse=True)
        return fuse([lexical[: size * 3], semantic[: size * 3]], size)

    async def count(self, kind: str) -> int:
        return len(self.docs.get(kind, {}))

    async def close(self) -> None:
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


# --------------------------------------------------------------------- elasticsearch


def _term_filters(match: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"terms": {k: v}} if isinstance(v, list) else {"term": {k: v}} for k, v in match.items()
    ]


class ElasticsearchIndex:
    """Minimal Elasticsearch 8 client: index/bulk/update/delete-by-query/search."""

    def __init__(
        self,
        url: str,
        prefix: str = "forgeflow",
        timeout: float = 10,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.prefix = prefix
        self.client = httpx.AsyncClient(
            base_url=url.rstrip("/"), timeout=timeout, transport=transport
        )
        self._ensured: set[str] = set()
        self._vectors: set[str] = set()

    def name(self, kind: str) -> str:
        return f"{self.prefix}-{kind}"

    async def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise KnowledgeUnavailable(f"{type(exc).__name__}: {exc}") from exc
        if response.status_code >= 500:
            raise KnowledgeUnavailable(f"elasticsearch {response.status_code}")
        return response

    async def ping(self) -> bool:
        try:
            response = await self._call("GET", "/_cluster/health")
        except KnowledgeUnavailable:
            return False
        return response.status_code == 200 and response.json().get("status") in (
            "green",
            "yellow",
        )

    async def ensure(self, kind: str) -> None:
        if kind in self._ensured:
            return
        name = self.name(kind)
        head = await self._call("HEAD", f"/{name}")
        if head.status_code == 404:
            created = await self._call(
                "PUT",
                f"/{name}",
                json={
                    "settings": {"number_of_shards": 1, "number_of_replicas": 0},
                    "mappings": MAPPING,
                },
            )
            # Another process may have created it at the same moment.
            if created.status_code >= 400 and "resource_already_exists" not in created.text:
                raise KnowledgeUnavailable(f"cannot create {name}: {created.text[:300]}")
        self._ensured.add(kind)

    async def ensure_vectors(self, kind: str, dims: int) -> None:
        """Add the dense_vector field the first time embeddings are written."""
        if kind in self._vectors:
            return
        await self.ensure(kind)
        response = await self._call(
            "PUT",
            f"/{self.name(kind)}/_mapping",
            json={
                "properties": {
                    "embedding": {
                        "type": "dense_vector",
                        "dims": dims,
                        "index": True,
                        "similarity": "cosine",
                    }
                }
            },
        )
        if response.status_code >= 400 and "mapper" not in response.text:
            raise KnowledgeUnavailable(f"cannot add vectors: {response.text[:300]}")
        self._vectors.add(kind)

    async def put_many(self, kind: str, docs: list[dict[str, Any]]) -> None:
        if not docs:
            return
        await self.ensure(kind)
        name = self.name(kind)
        for start in range(0, len(docs), 500):
            lines = []
            for doc in docs[start : start + 500]:
                body = {k: v for k, v in doc.items() if k != "id"}
                lines.append(json.dumps({"index": {"_index": name, "_id": doc["id"]}}))
                lines.append(json.dumps(body, default=str))
            response = await self._call(
                "POST",
                "/_bulk",
                content="\n".join(lines) + "\n",
                headers={"Content-Type": "application/x-ndjson"},
            )
            result = response.json()
            if response.status_code >= 400 or result.get("errors"):
                failed = [
                    i["index"].get("error")
                    for i in result.get("items", [])
                    if "error" in i["index"]
                ]
                raise KnowledgeUnavailable(f"bulk index failed: {str(failed[:2])[:300]}")

    async def update(self, kind: str, doc_id: str, fields: dict[str, Any]) -> None:
        await self.ensure(kind)
        response = await self._call(
            "POST", f"/{self.name(kind)}/_update/{doc_id}", json={"doc": fields}
        )
        if response.status_code >= 400 and response.status_code != 404:
            raise KnowledgeUnavailable(f"update failed: {response.text[:300]}")

    async def delete_where(
        self, kind: str, match: dict[str, Any], keep: dict[str, Any] | None = None
    ) -> None:
        await self.ensure(kind)
        query: dict[str, Any] = {"bool": {"filter": _term_filters(match)}}
        if keep:
            query["bool"]["must_not"] = _term_filters(keep)
        await self._call(
            "POST",
            f"/{self.name(kind)}/_delete_by_query",
            params={"conflicts": "proceed", "refresh": "true"},
            json={"query": query},
        )

    async def find(self, kind: str, match: dict[str, Any], size: int = 100) -> list[Hit]:
        await self.ensure(kind)
        response = await self._call(
            "POST",
            f"/{self.name(kind)}/_search",
            json={"size": size, "query": {"bool": {"filter": _term_filters(match)}}},
        )
        return self._hits(response)

    async def search(
        self,
        kinds: list[str],
        query: str,
        filters: dict[str, Any] | None = None,
        size: int = 10,
        vector: list[float] | None = None,
    ) -> list[Hit]:
        for kind in kinds:
            await self.ensure(kind)
        names = ",".join(self.name(k) for k in kinds)
        lexical_query = {
            "bool": {
                "must": [
                    {
                        "multi_match": {
                            "query": query,
                            "fields": ["symbol^3", "title^2", "path^2", "text", "resolution"],
                        }
                    }
                ],
                "filter": _term_filters(filters or {}),
            }
        }
        response = await self._call(
            "POST",
            f"/{names}/_search",
            params={"ignore_unavailable": "true"},
            json={"size": size * 3 if vector else size, "query": lexical_query},
        )
        lexical = self._hits(response)
        if vector is None:
            return lexical[:size]
        vector_kinds = [k for k in kinds if await self._has_vectors(k)]
        if not vector_kinds:
            return lexical[:size]
        knn = {
            "field": "embedding",
            "query_vector": vector,
            "k": size * 3,
            "num_candidates": max(50, size * 10),
            "filter": _term_filters(filters or {}),
        }
        response = await self._call(
            "POST",
            f"/{','.join(self.name(k) for k in vector_kinds)}/_search",
            params={"ignore_unavailable": "true"},
            json={"size": size * 3, "knn": knn},
        )
        return fuse([lexical, self._hits(response)], size)

    async def _has_vectors(self, kind: str) -> bool:
        """Vectors may have been added by another process: check the mapping once."""
        if kind in self._vectors:
            return True
        response = await self._call("GET", f"/{self.name(kind)}/_mapping")
        if response.status_code != 200:
            return False
        body: dict[str, Any] = response.json()
        first: dict[str, Any] = next(iter(body.values()), {})
        props = first.get("mappings", {}).get("properties", {})
        if "embedding" in props:
            self._vectors.add(kind)
            return True
        return False

    def _hits(self, response: httpx.Response) -> list[Hit]:
        if response.status_code >= 400:
            raise KnowledgeUnavailable(f"search failed: {response.text[:300]}")
        hits = []
        for h in response.json().get("hits", {}).get("hits", []):
            kind = h["_index"].removeprefix(f"{self.prefix}-")
            source = {**h.get("_source", {}), "id": h["_id"]}
            source.pop("embedding", None)
            hits.append(Hit(kind, h["_id"], float(h.get("_score") or 0.0), source))
        return hits

    async def count(self, kind: str) -> int:
        response = await self._call("GET", f"/{self.name(kind)}/_count")
        if response.status_code == 404:
            return 0
        return int(response.json().get("count", 0))

    async def close(self) -> None:
        await self.client.aclose()
