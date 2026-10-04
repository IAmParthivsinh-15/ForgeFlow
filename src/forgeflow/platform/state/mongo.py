"""MongoDB WorkflowStore. Requires a replica set (transactions) - see docker-compose.yml."""

from __future__ import annotations

from typing import Any

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from forgeflow.core.errors import ConcurrencyConflict, NotFoundError
from forgeflow.core.ids import utcnow
from forgeflow.platform.state.store import Commit, StoredEvent
from forgeflow.schemas.events import Event
from forgeflow.schemas.requirement import ClarificationQuestion, RequirementSpecification
from forgeflow.schemas.workflow import AgentRunRecord, Workflow


def _doc(model: Any, id_field: str) -> dict[str, Any]:
    data = model.model_dump(mode="python")
    data["_id"] = data[id_field]
    return data


def _strip(doc: dict[str, Any]) -> dict[str, Any]:
    doc.pop("_id", None)
    return doc


class MongoWorkflowStore:
    def __init__(self, client: AsyncMongoClient, database: str) -> None:
        self.client = client
        self.db: AsyncDatabase = client[database]
        self.workflows = self.db["workflows"]
        self.requirements = self.db["requirements"]
        self.questions = self.db["clarification_questions"]
        self.agent_runs = self.db["runs"]
        self.events = self.db["events"]

    async def ensure_indexes(self) -> None:
        await self.workflows.create_index([("created_at", DESCENDING)])
        await self.requirements.create_index(
            [("workflow_id", ASCENDING), ("version", ASCENDING)], unique=True
        )
        await self.questions.create_index([("workflow_id", ASCENDING)])
        await self.agent_runs.create_index([("workflow_id", ASCENDING)])
        await self.events.create_index(
            [("workflow_id", ASCENDING), ("seq", ASCENDING)], unique=True
        )
        await self.events.create_index([("published", ASCENDING), ("timestamp", ASCENDING)])

    async def commit(self, change: Commit) -> Workflow:
        async with self.client.start_session() as session:
            return await session.with_transaction(lambda s: self._apply(s, change))

    async def _apply(self, session: AsyncClientSession, change: Commit) -> Workflow:
        wf = change.workflow
        if change.expected_revision is None:
            base_revision, base_seq = 0, 0
        else:
            current = await self.workflows.find_one(
                {"_id": wf.workflow_id}, {"revision": 1, "event_seq": 1}, session=session
            )
            if current is None:
                raise NotFoundError(f"workflow {wf.workflow_id} not found")
            if current["revision"] != change.expected_revision:
                raise ConcurrencyConflict(f"workflow {wf.workflow_id} changed concurrently")
            base_revision, base_seq = current["revision"], current["event_seq"]

        stored = wf.model_copy(
            update={"revision": base_revision + 1, "event_seq": base_seq + len(change.events)}
        )
        doc = _doc(stored, "workflow_id")
        if change.expected_revision is None:
            try:
                await self.workflows.insert_one(doc, session=session)
            except DuplicateKeyError as exc:
                raise ConcurrencyConflict(f"workflow {wf.workflow_id} already exists") from exc
        else:
            result = await self.workflows.replace_one(
                {"_id": wf.workflow_id, "revision": change.expected_revision}, doc, session=session
            )
            if result.matched_count != 1:
                raise ConcurrencyConflict(f"workflow {wf.workflow_id} changed concurrently")

        for spec in change.specifications:
            spec_doc = spec.model_dump(mode="python")
            spec_doc["_id"] = f"{spec.workflow_id}:v{spec.version}"
            await self.requirements.insert_one(spec_doc, session=session)
        for question in change.questions:
            await self.questions.replace_one(
                {"_id": question.question_id},
                _doc(question, "question_id"),
                upsert=True,
                session=session,
            )
        for run in change.agent_runs:
            await self.agent_runs.replace_one(
                {"_id": run.run_id}, _doc(run, "run_id"), upsert=True, session=session
            )
        if change.events:
            await self.events.insert_many(
                [
                    {
                        **_doc(event, "event_id"),
                        "seq": base_seq + offset,
                        "published": False,
                        "published_at": None,
                    }
                    for offset, event in enumerate(change.events, start=1)
                ],
                session=session,
            )
        return stored

    async def get_workflow(self, workflow_id: str) -> Workflow:
        doc = await self.workflows.find_one({"_id": workflow_id})
        if doc is None:
            raise NotFoundError(f"workflow {workflow_id} not found")
        return Workflow.model_validate(_strip(doc))

    async def list_workflows(self, limit: int = 50) -> list[Workflow]:
        cursor = self.workflows.find().sort("created_at", DESCENDING).limit(limit)
        return [Workflow.model_validate(_strip(d)) async for d in cursor]

    async def get_specification(
        self, workflow_id: str, version: int | None = None
    ) -> RequirementSpecification | None:
        query: dict[str, Any] = {"workflow_id": workflow_id}
        if version is not None:
            query["version"] = version
        doc = await self.requirements.find_one(query, sort=[("version", DESCENDING)])
        return RequirementSpecification.model_validate(_strip(doc)) if doc else None

    async def list_specifications(self, workflow_id: str) -> list[RequirementSpecification]:
        cursor = self.requirements.find({"workflow_id": workflow_id}).sort("version", ASCENDING)
        return [RequirementSpecification.model_validate(_strip(d)) async for d in cursor]

    async def get_question(self, question_id: str) -> ClarificationQuestion:
        doc = await self.questions.find_one({"_id": question_id})
        if doc is None:
            raise NotFoundError(f"question {question_id} not found")
        return ClarificationQuestion.model_validate(_strip(doc))

    async def list_questions(self, workflow_id: str) -> list[ClarificationQuestion]:
        cursor = self.questions.find({"workflow_id": workflow_id}).sort("_id", ASCENDING)
        return [ClarificationQuestion.model_validate(_strip(d)) async for d in cursor]

    async def list_agent_runs(self, workflow_id: str) -> list[AgentRunRecord]:
        cursor = self.agent_runs.find({"workflow_id": workflow_id}).sort("started_at", ASCENDING)
        return [AgentRunRecord.model_validate(_strip(d)) async for d in cursor]

    async def list_events(
        self, workflow_id: str, after_seq: int = 0, limit: int = 500
    ) -> list[StoredEvent]:
        cursor = (
            self.events.find({"workflow_id": workflow_id, "seq": {"$gt": after_seq}})
            .sort("seq", ASCENDING)
            .limit(limit)
        )
        return [StoredEvent(seq=d["seq"], event=_event(d)) async for d in cursor]

    async def fetch_unpublished_events(self, limit: int = 100) -> list[Event]:
        cursor = self.events.find({"published": False}).sort("timestamp", ASCENDING).limit(limit)
        return [_event(d) async for d in cursor]

    async def mark_events_published(self, event_ids: list[str]) -> None:
        if event_ids:
            await self.events.update_many(
                {"_id": {"$in": event_ids}},
                {"$set": {"published": True, "published_at": utcnow()}},
            )

    async def ping(self) -> None:
        await self.db.command("ping")


def _event(doc: dict[str, Any]) -> Event:
    fields = {k: v for k, v in doc.items() if k in Event.model_fields}
    return Event.model_validate(fields)
