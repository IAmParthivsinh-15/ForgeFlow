"""Document store for extensibility records (connectors, projects, MCP, skills, approvals, audit).

These are simple, independently-updated documents, so a small generic store is
enough: put / get / find / delete plus compare-and-set for state changes that must
not race (e.g. two people deciding the same approval).
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from pydantic import BaseModel
from pymongo import ASCENDING, AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from forgeflow.core.errors import NotFoundError

COLLECTIONS = {
    "connectors": "connector_id",
    "projects": "project_id",
    "mcp_servers": "mcp_id",
    "mcp_tools": "tool_id",
    "skills": "skill_id",
    "skill_versions": "skill_version_id",
    "skill_installations": "installation_id",
    "approvals": "approval_id",
    "capability_audit": "audit_id",
    "capability_manifests": "manifest_id",
    "secrets": "ref",
    "knowledge_repositories": "repository_id",
    "artifacts": "artifact_id",
    "deployment_checks": "check_id",
    # L4 autonomy (additional.md)
    "autonomy_runs": "run_id",
    "run_keys": "key",
    "run_events": "event_id",
    "learning_records": "learning_id",
    "alerts": "alert_id",
}


class DocumentStore(Protocol):
    async def put(self, collection: str, doc: BaseModel | dict[str, Any]) -> None: ...

    async def get(self, collection: str, doc_id: str) -> dict[str, Any] | None: ...

    async def find(
        self,
        collection: str,
        query: dict[str, Any] | None = None,
        sort: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]: ...

    async def delete(self, collection: str, doc_id: str) -> bool: ...

    async def insert(self, collection: str, doc: BaseModel | dict[str, Any]) -> bool:
        """Create only if absent (atomic). False when the id already exists."""
        ...

    async def compare_and_set(
        self, collection: str, doc_id: str, expected: dict[str, Any], update: dict[str, Any]
    ) -> bool: ...


def _as_dict(collection: str, doc: BaseModel | dict[str, Any]) -> tuple[str, dict[str, Any]]:
    data = doc.model_dump(mode="python") if isinstance(doc, BaseModel) else dict(doc)
    return str(data[COLLECTIONS[collection]]), data


def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
    for key, expected in query.items():
        value = doc.get(key)
        if isinstance(expected, dict) and "$in" in expected:
            if isinstance(value, list):
                if not set(value) & set(expected["$in"]):
                    return False
            elif value not in expected["$in"]:
                return False
        elif isinstance(value, list) and not isinstance(expected, list):
            if expected not in value:
                return False
        elif value != expected:
            return False
    return True


async def require(store: DocumentStore, collection: str, doc_id: str, what: str) -> dict[str, Any]:
    doc = await store.get(collection, doc_id)
    if doc is None:
        raise NotFoundError(f"{what} {doc_id} not found")
    return doc


class InMemoryDocumentStore:
    def __init__(self) -> None:
        self.data: dict[str, dict[str, dict[str, Any]]] = {c: {} for c in COLLECTIONS}
        self._lock = asyncio.Lock()

    async def put(self, collection: str, doc: BaseModel | dict[str, Any]) -> None:
        doc_id, data = _as_dict(collection, doc)
        self.data[collection][doc_id] = data

    async def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        doc = self.data[collection].get(doc_id)
        return dict(doc) if doc is not None else None

    async def find(self, collection, query=None, sort=None, limit=1000):
        docs = [dict(d) for d in self.data[collection].values() if _matches(d, query or {})]
        if sort:
            reverse = sort.startswith("-")
            key = sort.lstrip("-")
            docs.sort(key=lambda d: (d.get(key) is None, d.get(key)), reverse=reverse)
        return docs[:limit]

    async def delete(self, collection: str, doc_id: str) -> bool:
        return self.data[collection].pop(doc_id, None) is not None

    async def insert(self, collection: str, doc: BaseModel | dict[str, Any]) -> bool:
        doc_id, data = _as_dict(collection, doc)
        async with self._lock:
            if doc_id in self.data[collection]:
                return False
            self.data[collection][doc_id] = data
            return True

    async def compare_and_set(self, collection, doc_id, expected, update) -> bool:
        async with self._lock:
            doc = self.data[collection].get(doc_id)
            if doc is None or not _matches(doc, expected):
                return False
            doc.update(update)
            return True


class MongoDocumentStore:
    def __init__(self, client: AsyncMongoClient, database: str) -> None:
        self.db: AsyncDatabase = client[database]

    async def ensure_indexes(self, audit_retention_days: int) -> None:
        await self.db["mcp_tools"].create_index([("mcp_id", ASCENDING)])
        await self.db["skills"].create_index([("slug", ASCENDING), ("owner_id", ASCENDING)])
        await self.db["skills"].create_index([("visibility", ASCENDING), ("status", ASCENDING)])
        await self.db["skill_versions"].create_index(
            [("skill_id", ASCENDING), ("version", ASCENDING)], unique=True
        )
        await self.db["skill_installations"].create_index([("user_id", ASCENDING)])
        await self.db["approvals"].create_index([("status", ASCENDING), ("workflow_id", ASCENDING)])
        await self.db["capability_audit"].create_index([("workflow_id", ASCENDING)])
        await self.db["capability_audit"].create_index(
            [("timestamp", ASCENDING)], expireAfterSeconds=audit_retention_days * 86400
        )
        await self.db["capability_manifests"].create_index([("workflow_id", ASCENDING)])
        await self.db["artifacts"].create_index([("workflow_id", ASCENDING)])
        await self.db["autonomy_runs"].create_index([("trace_id", ASCENDING)], unique=True)
        await self.db["autonomy_runs"].create_index([("status", ASCENDING)])
        await self.db["run_events"].create_index(
            [("trace_id", ASCENDING), ("seq", ASCENDING)], unique=True
        )
        await self.db["alerts"].create_index([("status", ASCENDING)])
        await self.db["deployment_checks"].create_index(
            [("project_id", ASCENDING), ("created_at", ASCENDING)]
        )

    async def put(self, collection: str, doc: BaseModel | dict[str, Any]) -> None:
        doc_id, data = _as_dict(collection, doc)
        await self.db[collection].replace_one({"_id": doc_id}, {**data, "_id": doc_id}, upsert=True)

    async def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        doc = await self.db[collection].find_one({"_id": doc_id})
        if doc is not None:
            doc.pop("_id", None)
        return doc

    async def find(self, collection, query=None, sort=None, limit=1000):
        cursor = self.db[collection].find(query or {})
        if sort:
            cursor = cursor.sort(sort.lstrip("-"), -1 if sort.startswith("-") else 1)
        docs = []
        async for doc in cursor.limit(limit):
            doc.pop("_id", None)
            docs.append(doc)
        return docs

    async def delete(self, collection: str, doc_id: str) -> bool:
        return (await self.db[collection].delete_one({"_id": doc_id})).deleted_count == 1

    async def insert(self, collection: str, doc: BaseModel | dict[str, Any]) -> bool:
        from pymongo.errors import DuplicateKeyError

        doc_id, data = _as_dict(collection, doc)
        try:
            await self.db[collection].insert_one({**data, "_id": doc_id})
        except DuplicateKeyError:
            return False
        return True

    async def compare_and_set(self, collection, doc_id, expected, update) -> bool:
        result = await self.db[collection].update_one({"_id": doc_id, **expected}, {"$set": update})
        return result.matched_count == 1
