from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from forgeflow.apps.api.deps import ContainerDep
from forgeflow.platform.orchestration.service import MAX_REQUEST_CHARS
from forgeflow.schemas.requirement import ClarificationQuestion, RequirementSpecification
from forgeflow.schemas.workflow import AgentRunRecord, Workflow

router = APIRouter(prefix="/api/v1", tags=["workflows"])

STREAM_POLL_SECONDS = 1.0
STREAM_HEARTBEAT_SECONDS = 15.0


class CreateWorkflowRequest(BaseModel):
    request: str = Field(min_length=1, max_length=MAX_REQUEST_CHARS)
    repository_path: str | None = Field(
        default=None, description="Repository directory, relative to REPOS_ROOT."
    )


class WorkflowDetail(BaseModel):
    workflow: Workflow
    specification: RequirementSpecification | None
    questions: list[ClarificationQuestion]
    agent_runs: list[AgentRunRecord]


class EventOut(BaseModel):
    seq: int
    event_id: str
    event_type: str
    timestamp: str
    payload: dict


@router.post("/workflows", status_code=201)
async def create_workflow(body: CreateWorkflowRequest, c: ContainerDep) -> Workflow:
    return await c.service.create_workflow(body.request, body.repository_path)


@router.get("/workflows")
async def list_workflows(c: ContainerDep, limit: int = Query(50, ge=1, le=200)) -> list[Workflow]:
    return await c.store.list_workflows(limit)


@router.get("/workflows/{workflow_id}")
async def get_workflow(workflow_id: str, c: ContainerDep) -> WorkflowDetail:
    workflow = await c.store.get_workflow(workflow_id)
    return WorkflowDetail(
        workflow=workflow,
        specification=await c.store.get_specification(workflow_id),
        questions=await c.store.list_questions(workflow_id),
        agent_runs=await c.store.list_agent_runs(workflow_id),
    )


@router.post("/workflows/{workflow_id}/cancel")
async def cancel_workflow(workflow_id: str, c: ContainerDep) -> Workflow:
    return await c.service.cancel_workflow(workflow_id)


@router.get("/workflows/{workflow_id}/requirements")
async def list_requirements(workflow_id: str, c: ContainerDep) -> list[RequirementSpecification]:
    await c.store.get_workflow(workflow_id)
    return await c.store.list_specifications(workflow_id)


@router.get("/workflows/{workflow_id}/questions")
async def list_questions(workflow_id: str, c: ContainerDep) -> list[ClarificationQuestion]:
    await c.store.get_workflow(workflow_id)
    return await c.store.list_questions(workflow_id)


@router.get("/workflows/{workflow_id}/events")
async def list_events(
    workflow_id: str, c: ContainerDep, after: int = Query(0, ge=0)
) -> list[EventOut]:
    await c.store.get_workflow(workflow_id)
    return [_event_out(se.seq, se.event) for se in await c.store.list_events(workflow_id, after)]


@router.get("/workflows/{workflow_id}/stream")
async def stream_events(
    workflow_id: str,
    request: Request,
    c: ContainerDep,
    after: int = Query(0, ge=0),
    last_event_id: str | None = Header(default=None),
) -> StreamingResponse:
    """Server-Sent Events. Kafka stays internal; the browser reads the persisted event log."""
    await c.store.get_workflow(workflow_id)
    cursor = int(last_event_id) if last_event_id and last_event_id.isdigit() else after

    async def generate() -> AsyncIterator[str]:
        nonlocal cursor
        idle = 0.0
        while not await request.is_disconnected():
            events = await c.store.list_events(workflow_id, cursor)
            for se in events:
                cursor = se.seq
                data = _event_out(se.seq, se.event).model_dump_json()
                yield f"id: {se.seq}\nevent: {se.event.event_type}\ndata: {data}\n\n"
            if events:
                idle = 0.0
                continue
            await asyncio.sleep(STREAM_POLL_SECONDS)
            idle += STREAM_POLL_SECONDS
            if idle >= STREAM_HEARTBEAT_SECONDS:
                idle = 0.0
                yield ": heartbeat\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _event_out(seq: int, event) -> EventOut:
    return EventOut(
        seq=seq,
        event_id=event.event_id,
        event_type=event.event_type,
        timestamp=event.timestamp.isoformat(),
        payload=json.loads(json.dumps(event.payload, default=str)),
    )
