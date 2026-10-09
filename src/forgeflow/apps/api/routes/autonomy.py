"""L4 autonomy API (additional.md): triggers, runs, plans, evidence, stop/resume, review.

The webhook is the only unauthenticated-by-session endpoint and is authenticated by
GitHub's HMAC signature instead; everything else is the operator API.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from forgeflow.apps.api.deps import ContainerDep
from forgeflow.autonomy.facade import Autonomy
from forgeflow.autonomy.intake import handle_webhook, intake, source_item, sweep
from forgeflow.autonomy.learning import decide_candidate, review_last_runs
from forgeflow.core.errors import NotFoundError, ValidationFailed
from forgeflow.integrations.github.client import verify_webhook_signature
from forgeflow.schemas.autonomy import Run, RunEvent, TaskPlan

router = APIRouter(prefix="/api/v1/autonomy", tags=["autonomy"])


def _auto(c: Any) -> Autonomy:
    if c.autonomy is None:
        raise ValidationFailed("the autonomy layer is not configured")
    return c.autonomy


def _actor(c: Any) -> str:
    return f"user:{c.settings.local_user_id}"


@router.get("/contract")
async def get_contract(c: ContainerDep) -> dict[str, Any]:
    contract = _auto(c).commander.contract()
    return contract.model_dump(mode="json")


@router.get("/status")
async def status(c: ContainerDep) -> dict[str, Any]:
    auto = _auto(c)
    contract = auto.commander.contract()
    active = await auto.runs.active()
    by_status: dict[str, int] = {}
    for run in active:
        by_status[str(run.status)] = by_status.get(str(run.status), 0) + 1
    client = await auto.commander.github(contract)
    return {
        "mode": contract.mode,
        "contract_version": contract.version,
        "contract_hash": contract.hash,
        "responsibility": contract.responsibility,
        "source": contract.source.model_dump(),
        "source_ready": client is not None and bool(contract.source.repository),
        "webhook_secret_configured": bool(c.settings.github_webhook_secret),
        "active": by_status,
        "open_alerts": len(await auto.alerts.list_alerts("open")),
        "guardrails": {
            "max_workers": contract.limits.max_workers,
            "allowed_models": contract.limits.allowed_models,
            "budgets": contract.limits.budgets.model_dump(),
            "circuit_breakers": contract.circuit_breakers.model_dump(),
        },
    }


# --------------------------------------------------------------------- triggers


@router.post("/webhooks/github")
async def github_webhook(
    request: Request,
    c: ContainerDep,
    x_github_event: str = Header(default=""),
    x_hub_signature_256: str | None = Header(default=None),
) -> JSONResponse:
    """Primary trigger. Refused unless the HMAC signature matches GITHUB_WEBHOOK_SECRET."""
    body = await request.body()
    if not verify_webhook_signature(c.settings.github_webhook_secret, body, x_hub_signature_256):
        return JSONResponse({"detail": "invalid or missing signature"}, status_code=401)
    auto = _auto(c)
    try:
        payload = json.loads(body)
    except ValueError:
        return JSONResponse({"detail": "invalid JSON"}, status_code=400)
    result = await handle_webhook(auto.runs, auto.commander.contract(), x_github_event, payload)
    return JSONResponse(result, status_code=202)


class ManualIntake(BaseModel):
    number: int = Field(ge=1, description="Issue number in the contract's repository")


@router.post("/intake", status_code=202)
async def manual_intake(body: ManualIntake, c: ContainerDep) -> Run:
    """Same intake path as the webhook and the sweep (reads the issue from GitHub)."""
    auto = _auto(c)
    contract = auto.commander.contract()
    client = await auto.commander.github(contract)
    if client is None or not contract.source.repository:
        raise ValidationFailed(
            "configure source.repository and source.connector_id in the contract"
        )
    issue = await client.get_issue(contract.source.repository, body.number)
    run, _ = await intake(auto.runs, contract, source_item(issue), "manual")
    return run


@router.post("/sweep")
async def run_sweep(c: ContainerDep) -> dict[str, Any]:
    auto = _auto(c)
    contract = auto.commander.contract()
    client = await auto.commander.github(contract)
    if client is None or not contract.source.repository:
        raise ValidationFailed(
            "configure source.repository and source.connector_id in the contract"
        )
    return await sweep(auto.runs, contract, client)


# ------------------------------------------------------------------------- runs


@router.get("/runs")
async def list_runs(c: ContainerDep, status: str | None = None, limit: int = 50) -> list[Run]:
    return await _auto(c).runs.list_runs(status.split(",") if status else None, min(limit, 200))


@router.get("/runs/{trace_id}")
async def get_run(trace_id: str, c: ContainerDep) -> Run:
    return await _auto(c).runs.by_trace(trace_id)


@router.get("/runs/{trace_id}/plan")
async def get_plan(trace_id: str, c: ContainerDep) -> TaskPlan:
    """task_plan.json as saved before any worker started."""
    run = await _auto(c).runs.by_trace(trace_id)
    if run.plan is None:
        raise NotFoundError(f"run {trace_id} has no saved plan")
    return run.plan


@router.get("/runs/{trace_id}/events")
async def get_events(trace_id: str, c: ContainerDep) -> list[RunEvent]:
    return await _auto(c).runs.events(trace_id)


@router.get("/runs/{trace_id}/portfolio")
async def portfolio(trace_id: str, c: ContainerDep) -> dict[str, Any]:
    """Everything a reviewer needs for this run (additional.md section 10)."""
    auto = _auto(c)
    run = await auto.runs.by_trace(trace_id)
    ext = auto.runs
    out: dict[str, Any] = {
        "run": run.model_dump(mode="json"),
        "events": [e.model_dump(mode="json") for e in await auto.runs.events(trace_id)],
        "learning": await ext.store.find("learning_records", {"trace_id": trace_id}),
        "alerts": await ext.store.find("alerts", {"trace_id": trace_id}),
    }
    if run.workflow_id:
        out["workflow"] = (await c.store.get_workflow(run.workflow_id)).model_dump(mode="json")
        out["tasks"] = [
            t.model_dump(mode="json") for t in await c.store.list_tasks(run.workflow_id)
        ]
        out["agent_runs"] = [
            r.model_dump(mode="json") for r in await c.store.list_agent_runs(run.workflow_id)
        ]
        out["audit"] = await ext.store.find(
            "capability_audit", {"workflow_id": run.workflow_id}, limit=10000
        )
    return out


class StopBody(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


@router.post("/runs/{trace_id}/stop")
async def stop_run(trace_id: str, body: StopBody, c: ContainerDep) -> Run:
    return await _auto(c).control.stop(trace_id, _actor(c), body.reason)


@router.post("/runs/{trace_id}/resume")
async def resume_run(trace_id: str, c: ContainerDep) -> Run:
    return await _auto(c).control.resume(trace_id, _actor(c))


@router.post("/runs/{trace_id}/rerun", status_code=201)
async def rerun(trace_id: str, c: ContainerDep) -> Run:
    return await _auto(c).control.rerun(trace_id, _actor(c))


# ------------------------------------------------------------ alerts, learning


@router.get("/alerts")
async def alerts(c: ContainerDep, status: str | None = "open") -> list[dict[str, Any]]:
    return await _auto(c).alerts.list_alerts(status or None)


@router.post("/alerts/{alert_id}/ack")
async def ack_alert(alert_id: str, c: ContainerDep) -> dict[str, bool]:
    return {"acknowledged": await _auto(c).alerts.acknowledge(alert_id, _actor(c))}


@router.get("/learning")
async def learning(c: ContainerDep, limit: int = 50) -> list[dict[str, Any]]:
    return await _auto(c).runs.store.find("learning_records", sort="-created_at", limit=limit)


class CandidateDecision(BaseModel):
    decision: str = Field(pattern="^(accepted|rejected)$")
    note: str = ""


@router.post("/learning/{learning_id}/candidates/{index}")
async def decide_learning(
    learning_id: str, index: int, body: CandidateDecision, c: ContainerDep
) -> dict[str, Any]:
    """Records the decision only; accepted candidates are applied through versioned changes."""
    return await decide_candidate(
        _auto(c).runs.store, learning_id, index, body.decision, _actor(c), body.note
    )


@router.get("/review")
async def review(c: ContainerDep, last: int = 5) -> list[dict[str, Any]]:
    """Last-N-run review: named escalations vs avoidable human dependency."""
    auto = _auto(c)
    runs = await auto.runs.list_runs(limit=max(1, min(last, 50)))
    learnings = {
        d["trace_id"]: d
        for d in await _auto(c).runs.store.find("learning_records", sort="-created_at", limit=500)
    }
    return review_last_runs(runs, learnings)
