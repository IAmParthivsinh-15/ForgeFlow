"""Human approval of sensitive actions (spec sections 30, 236, 237).

    capability request (policy = ASK)
        -> approval PENDING  (event approval.requested; workflow -> WAITING_FOR_APPROVAL)
        -> user approves / rejects in the UI, or it expires
        -> event approval.resolved; the waiting agent continues or is refused

Decisions use compare-and-set, so an approval is decided exactly once.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from forgeflow.core.errors import NotFoundError, ValidationFailed
from forgeflow.core.ids import new_id, utcnow
from forgeflow.extensibility.store import DocumentStore
from forgeflow.platform.state.store import Commit, WorkflowStore
from forgeflow.schemas.events import Event, EventType
from forgeflow.schemas.extensibility import Approval, Risk


class ApprovalService:
    def __init__(
        self,
        store: DocumentStore,
        workflows: WorkflowStore | None,
        timeout_seconds: float,
        poll_seconds: float = 1.0,
    ) -> None:
        self.store = store
        self.workflows = workflows
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds

    async def request(
        self,
        *,
        owner_id: str,
        workflow_id: str | None,
        task_id: str | None,
        agent: str,
        capability_id: str,
        action: str,
        summary: str,
        risk: Risk,
        details: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> Approval:
        now = utcnow()
        approval = Approval(
            approval_id=new_id("apr"),
            owner_id=owner_id,
            workflow_id=workflow_id,
            task_id=task_id,
            agent=agent,
            capability_id=capability_id,
            action=action,
            summary=summary,
            details=details or {},
            risk=risk,
            status="pending",
            requested_at=now,
            expires_at=now + timedelta(seconds=timeout_seconds or self.timeout_seconds),
        )
        await self.store.put("approvals", approval)
        await self._event(approval, EventType.APPROVAL_REQUESTED)
        return approval

    async def wait(self, approval_id: str) -> Approval:
        """Block until decided or expired. Run inside the agent worker's task heartbeat."""
        while True:
            approval = await self.get(approval_id)
            if approval.status != "pending":
                return approval
            if utcnow() >= approval.expires_at:
                if await self.store.compare_and_set(
                    "approvals",
                    approval_id,
                    {"status": "pending"},
                    {"status": "expired", "decided_at": utcnow()},
                ):
                    expired = await self.get(approval_id)
                    await self._event(expired, EventType.APPROVAL_RESOLVED)
                    return expired
                continue
            await asyncio.sleep(self.poll_seconds)

    async def decide(
        self, approval_id: str, approve: bool, owner_id: str, note: str | None = None
    ) -> Approval:
        approval = await self.get(approval_id)
        if approval.owner_id != owner_id:
            raise NotFoundError(f"approval {approval_id} not found")
        if approval.status != "pending":
            raise ValidationFailed(f"approval is already {approval.status}")
        status = "approved" if approve else "rejected"
        won = await self.store.compare_and_set(
            "approvals",
            approval_id,
            {"status": "pending"},
            {"status": status, "decided_at": utcnow(), "decided_by": owner_id, "note": note},
        )
        if not won:
            raise ValidationFailed("approval was decided concurrently")
        decided = await self.get(approval_id)
        await self._event(decided, EventType.APPROVAL_RESOLVED)
        return decided

    async def get(self, approval_id: str) -> Approval:
        doc = await self.store.get("approvals", approval_id)
        if doc is None:
            raise NotFoundError(f"approval {approval_id} not found")
        return Approval.model_validate(doc)

    async def list_approvals(
        self, owner_id: str, status: str | None = None, workflow_id: str | None = None
    ) -> list[Approval]:
        query: dict[str, Any] = {"owner_id": owner_id}
        if status:
            query["status"] = status
        if workflow_id:
            query["workflow_id"] = workflow_id
        docs = await self.store.find("approvals", query, sort="-requested_at")
        return [Approval.model_validate(d) for d in docs]

    async def _event(self, approval: Approval, event_type: str) -> None:
        if self.workflows is None or approval.workflow_id is None:
            return
        await self.workflows.commit(
            Commit(
                events=[
                    Event(
                        event_type=event_type,
                        workflow_id=approval.workflow_id,
                        task_id=approval.task_id,
                        payload={
                            "approval_id": approval.approval_id,
                            "capability_id": approval.capability_id,
                            "summary": approval.summary,
                            "status": approval.status,
                            "risk": approval.risk,
                        },
                    )
                ]
            )
        )
