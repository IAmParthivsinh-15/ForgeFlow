"""MongoDB WorkflowStore. Requires a replica set (transactions) - see docker-compose.yml."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient, ReturnDocument
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from forgeflow.core.errors import ConcurrencyConflict, NotFoundError
from forgeflow.core.ids import utcnow
from forgeflow.platform.state.store import Commit, CommitResult, StoredEvent
from forgeflow.schemas.events import Event
from forgeflow.schemas.requirement import ClarificationQuestion, RequirementSpecification
from forgeflow.schemas.task import Task, TaskStatus, Workspace
from forgeflow.schemas.verification import A2AMessage
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
        self.tasks = self.db["tasks"]
        self.workspaces = self.db["workspaces"]
        self.counters = self.db["counters"]
        self.a2a = self.db["a2a_messages"]
        self.events = self.db["events"]

    async def ensure_indexes(self, event_retention_days: int = 30) -> None:
        await self.workflows.create_index([("created_at", DESCENDING)])
        await self.requirements.create_index(
            [("workflow_id", ASCENDING), ("version", ASCENDING)], unique=True
        )
        await self.questions.create_index([("workflow_id", ASCENDING)])
        await self.agent_runs.create_index([("workflow_id", ASCENDING)])
        await self.tasks.create_index([("workflow_id", ASCENDING), ("created_at", ASCENDING)])
        await self.tasks.create_index([("status", ASCENDING), ("updated_at", ASCENDING)])
        await self.workspaces.create_index([("workflow_id", ASCENDING)])
        await self.a2a.create_index([("workflow_id", ASCENDING), ("created_at", ASCENDING)])
        await self.events.create_index(
            [("workflow_id", ASCENDING), ("seq", ASCENDING)], unique=True
        )
        await self.events.create_index([("published", ASCENDING), ("timestamp", ASCENDING)])
        # Bounded storage (e.g. MongoDB Atlas free tier): published events expire. Events
        # still waiting in the outbox have no published_at and are never removed.
        await self.events.create_index(
            [("published_at", ASCENDING)], expireAfterSeconds=event_retention_days * 86400
        )
        await self._migrate_event_counters()

    async def _migrate_event_counters(self) -> None:
        """Seed per-workflow event counters from existing events (pre-Milestone-2 data)."""
        pipeline = [{"$group": {"_id": "$workflow_id", "max_seq": {"$max": "$seq"}}}]
        async for row in await self.events.aggregate(pipeline):
            await self.counters.update_one(
                {"_id": row["_id"]}, {"$max": {"seq": row["max_seq"]}}, upsert=True
            )

    async def commit(self, change: Commit) -> CommitResult:
        async with self.client.start_session() as session:
            return await session.with_transaction(lambda s: self._apply(s, change))

    async def _apply(self, session: AsyncClientSession, change: Commit) -> CommitResult:
        stored_wf: Workflow | None = None
        wf = change.workflow
        if wf is not None:
            if change.create_workflow:
                stored_wf = wf.model_copy(update={"revision": 1})
                try:
                    await self.workflows.insert_one(_doc(stored_wf, "workflow_id"), session=session)
                except DuplicateKeyError as exc:
                    raise ConcurrencyConflict(f"workflow {wf.workflow_id} already exists") from exc
            else:
                stored_wf = wf.model_copy(update={"revision": (change.expected_revision or 0) + 1})
                result = await self.workflows.replace_one(
                    {"_id": wf.workflow_id, "revision": change.expected_revision},
                    _doc(stored_wf, "workflow_id"),
                    session=session,
                )
                if result.matched_count != 1:
                    raise ConcurrencyConflict(f"workflow {wf.workflow_id} changed concurrently")

        stored_tasks: dict[str, Task] = {}
        for write in change.tasks:
            task = write.task.model_copy(update={"revision": (write.expected_revision or 0) + 1})
            if write.expected_revision is None:
                try:
                    await self.tasks.insert_one(_doc(task, "task_id"), session=session)
                except DuplicateKeyError as exc:
                    raise ConcurrencyConflict(f"task {task.task_id} already exists") from exc
            else:
                result = await self.tasks.replace_one(
                    {"_id": task.task_id, "revision": write.expected_revision},
                    _doc(task, "task_id"),
                    session=session,
                )
                if result.matched_count != 1:
                    raise ConcurrencyConflict(f"task {task.task_id} changed concurrently")
            stored_tasks[task.task_id] = task

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
        for ws in change.workspaces:
            await self.workspaces.replace_one(
                {"_id": ws.workspace_id}, _doc(ws, "workspace_id"), upsert=True, session=session
            )

        for msg in change.a2a_messages:
            await self.a2a.replace_one(
                {"_id": msg.message_id}, _doc(msg, "message_id"), upsert=True, session=session
            )

        by_workflow: dict[str, list[Event]] = {}
        for event in change.events:
            by_workflow.setdefault(event.workflow_id, []).append(event)
        for workflow_id, events in by_workflow.items():
            counter = await self.counters.find_one_and_update(
                {"_id": workflow_id},
                {"$inc": {"seq": len(events)}},
                upsert=True,
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            assert counter is not None  # upsert + ReturnDocument.AFTER always yields a doc
            first = counter["seq"] - len(events) + 1
            await self.events.insert_many(
                [
                    {
                        **_doc(event, "event_id"),
                        "seq": first + offset,
                        "published": False,
                        "published_at": None,
                    }
                    for offset, event in enumerate(events)
                ],
                session=session,
            )
        return CommitResult(stored_wf, stored_tasks)

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

    async def get_task(self, task_id: str) -> Task:
        doc = await self.tasks.find_one({"_id": task_id})
        if doc is None:
            raise NotFoundError(f"task {task_id} not found")
        return Task.model_validate(_strip(doc))

    async def list_tasks(self, workflow_id: str) -> list[Task]:
        cursor = self.tasks.find({"workflow_id": workflow_id}).sort("created_at", ASCENDING)
        return [Task.model_validate(_strip(d)) async for d in cursor]

    async def find_tasks(
        self, statuses: list[TaskStatus], updated_before: datetime | None = None
    ) -> list[Task]:
        query: dict[str, Any] = {"status": {"$in": [str(s) for s in statuses]}}
        if updated_before is not None:
            query["updated_at"] = {"$lt": updated_before}
        return [Task.model_validate(_strip(d)) async for d in self.tasks.find(query)]

    async def touch_task(self, task_id: str, worker_id: str, at: datetime) -> bool:
        result = await self.tasks.update_one(
            {"_id": task_id, "status": str(TaskStatus.RUNNING), "worker_id": worker_id},
            {"$set": {"heartbeat_at": at}},
        )
        return result.matched_count == 1

    async def get_workspace(self, workspace_id: str) -> Workspace:
        doc = await self.workspaces.find_one({"_id": workspace_id})
        if doc is None:
            raise NotFoundError(f"workspace {workspace_id} not found")
        return Workspace.model_validate(_strip(doc))

    async def list_workspaces(self, workflow_id: str | None = None) -> list[Workspace]:
        query = {"workflow_id": workflow_id} if workflow_id else {}
        cursor = self.workspaces.find(query).sort("created_at", ASCENDING)
        return [Workspace.model_validate(_strip(d)) async for d in cursor]

    async def list_a2a_messages(self, workflow_id: str) -> list[A2AMessage]:
        cursor = self.a2a.find({"workflow_id": workflow_id}).sort("created_at", ASCENDING)
        return [A2AMessage.model_validate(_strip(d)) async for d in cursor]

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
