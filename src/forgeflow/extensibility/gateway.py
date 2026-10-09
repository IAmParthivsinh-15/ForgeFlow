"""Capability gateway: the single path through which agents use external capabilities.

    request -> revocation check -> policy (AUTO / ASK / DENY) -> approval if ASK
            -> execute -> audit (spec sections 202, 237, 238, 245)

Connector operations and MCP tool calls go through `invoke`. A denial is an
ordinary outcome (CapabilityDenied), not a crash, so the calling agent can adapt.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from forgeflow.core.errors import ForgeFlowError
from forgeflow.core.ids import new_id, utcnow
from forgeflow.core.redaction import redact
from forgeflow.extensibility.approvals import ApprovalService
from forgeflow.extensibility.policy import decide
from forgeflow.extensibility.store import DocumentStore
from forgeflow.platform.state.store import Commit
from forgeflow.schemas.events import Event, EventType, Topics
from forgeflow.schemas.extensibility import Capability, CapabilityAudit, Policy, Project

# status lookup for revocation: (collection, id) -> current status
SourceStatus = Callable[[], Awaitable[str]]
# L4 run guard: (invocation, capability) -> reason the call must be blocked, or None.
RunGuard = Callable[["Invocation", Capability], Awaitable[str | None]]


class CapabilityDenied(ForgeFlowError):
    """The capability may not be used (policy, rejection, expiry, or revocation)."""


@dataclass
class Invocation:
    owner_id: str
    workflow_id: str | None
    task_id: str | None
    agent: str
    project: Project | None = None
    # L4 autonomy: the run's action profile (AUTO / ASK / DENY by capability id).
    action_profile: Any = None
    trace_id: str | None = None


class CapabilityGateway:
    def __init__(self, store: DocumentStore, approvals: ApprovalService) -> None:
        self.store = store
        self.approvals = approvals
        # Installed by the autonomy layer: blocks calls of stopped/paused/observe runs.
        self.run_guard: RunGuard | None = None

    async def invoke[T](
        self,
        inv: Invocation,
        capability: Capability,
        action: Callable[[], Awaitable[T]],
        *,
        summary: str,
        details: dict[str, Any] | None = None,
        source_policy: Policy | None = None,
        tool_override: Policy | None = None,
        source_status: SourceStatus | None = None,
        approval_timeout: float | None = None,
    ) -> T:
        started = time.perf_counter()
        if self.run_guard is not None:
            blocked = await self.run_guard(inv, capability)
            if blocked is not None:
                await self._audit(
                    inv, capability, summary, "denied_by_policy", "denied", started, blocked
                )
                raise CapabilityDenied(f"{capability.name}: {blocked}")
        if inv.action_profile is not None:
            tier = inv.action_profile.tier(capability.capability_id)
            if tier == "deny":
                await self._audit(
                    inv,
                    capability,
                    summary,
                    "denied_by_policy",
                    "denied",
                    started,
                    f"DENY in action profile {inv.action_profile.id}",
                )
                raise CapabilityDenied(
                    f"{capability.name} is denied by action profile {inv.action_profile.id}"
                )
            # The approved contract is explicit authority: AUTO replaces the catalog's
            # default; ASK (or anything not listed) needs a human.
            tool_override = "auto" if tier == "auto" else "ask"
        if source_status is not None:
            status = await source_status()
            if status not in ("active",):
                await self._audit(
                    inv,
                    capability,
                    summary,
                    "not_required",
                    "denied",
                    started,
                    f"source is {status}",
                )
                raise CapabilityDenied(f"{capability.name} is unavailable: its source is {status}")

        decision = decide(capability, inv.agent, inv.project, source_policy, tool_override)
        approval_state = "auto"
        if decision.policy == "deny":
            await self._audit(
                inv, capability, summary, "denied_by_policy", "denied", started, decision.reason
            )
            raise CapabilityDenied(f"{capability.name}: {decision.reason}")
        if decision.policy == "ask":
            approval = await self.approvals.request(
                owner_id=inv.owner_id,
                workflow_id=inv.workflow_id,
                task_id=inv.task_id,
                agent=inv.agent,
                capability_id=capability.capability_id,
                action=capability.name,
                summary=summary,
                risk=capability.risk,
                details=details,
                timeout_seconds=approval_timeout,
            )
            approval = await self.approvals.wait(approval.approval_id)
            if approval.status != "approved":
                state = "user_rejected" if approval.status == "rejected" else "expired"
                await self._audit(
                    inv,
                    capability,
                    summary,
                    state,
                    "denied",
                    started,
                    f"approval {approval.status}",
                )
                raise CapabilityDenied(f"{capability.name} was not approved ({approval.status})")
            approval_state = "user_approved"
            # Re-check revocation: the source may have been revoked while waiting.
            if source_status is not None and (status := await source_status()) != "active":
                await self._audit(
                    inv,
                    capability,
                    summary,
                    approval_state,
                    "denied",
                    started,
                    f"source is {status}",
                )
                raise CapabilityDenied(f"{capability.name} was revoked while awaiting approval")
        try:
            result = await action()
        except Exception as exc:
            await self._audit(
                inv,
                capability,
                summary,
                approval_state,
                "error",
                started,
                f"{type(exc).__name__}: {exc}"[:500],
            )
            raise
        await self._audit(inv, capability, summary, approval_state, "success", started)
        return result

    async def record_skill_use(self, inv: Invocation, skill_id: str, version: str, level: str):
        await self.store.put(
            "capability_audit",
            CapabilityAudit(
                audit_id=new_id("aud"),
                timestamp=utcnow(),
                owner_id=inv.owner_id,
                workflow_id=inv.workflow_id,
                task_id=inv.task_id,
                agent=inv.agent,
                capability_id=f"skill.{skill_id}",
                capability_type="skill",
                source_id=skill_id,
                skill_version=version,
                action=f"skill.injected:{level}",
                approval="not_required",
                result="success",
            ),
        )

    async def _audit(
        self,
        inv: Invocation,
        capability: Capability,
        summary: str,
        approval: str,
        result: str,
        started: float,
        error: str | None = None,
    ) -> None:
        latency_ms = int((time.perf_counter() - started) * 1000)
        await self.store.put(
            "capability_audit",
            CapabilityAudit(
                audit_id=new_id("aud"),
                timestamp=utcnow(),
                owner_id=inv.owner_id,
                workflow_id=inv.workflow_id,
                task_id=inv.task_id,
                agent=inv.agent,
                capability_id=capability.capability_id,
                capability_type=capability.type,
                source_id=capability.source_id,
                action=summary[:300],
                approval=approval,  # type: ignore[arg-type]
                result=result,  # type: ignore[arg-type]
                latency_ms=latency_ms,
                error=error,
            ),
        )
        await self._event(inv, capability, summary, approval, result, latency_ms, error)

    async def _event(
        self,
        inv: Invocation,
        capability: Capability,
        summary: str,
        approval: str,
        result: str,
        latency_ms: int,
        error: str | None,
    ) -> None:
        """Capability use as a workflow event, so the UI can replay it (spec section 28)."""
        workflows = self.approvals.workflows
        if workflows is None or inv.workflow_id is None:
            return
        await workflows.commit(
            Commit(
                events=[
                    Event(
                        event_type=EventType.CAPABILITY_USED,
                        topic=Topics.AGENT,
                        workflow_id=inv.workflow_id,
                        task_id=inv.task_id,
                        payload={
                            "agent": inv.agent,
                            "capability_id": capability.capability_id,
                            "capability": capability.name,
                            "type": capability.type,
                            "summary": redact(summary)[:300],
                            "approval": approval,
                            "result": result,
                            "latency_ms": latency_ms,
                            "error": redact(error)[:300] if error else None,
                        },
                    )
                ]
            )
        )
