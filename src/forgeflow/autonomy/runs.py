"""Run store: idempotent creation, guarded transitions, append-only evidence log.

- One run per source-item key (`run_keys`, created atomically): a webhook retried, the
  same issue seen again by the sweep, or both paths at once never create two runs.
- Status changes are compare-and-set on the previous status, so the commander and an
  operator's emergency stop cannot overwrite each other.
- `run_events` is append-only and keyed by trace_id: the evidence portfolio.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from forgeflow.core.errors import NotFoundError
from forgeflow.core.ids import new_id, utcnow
from forgeflow.core.redaction import redact
from forgeflow.extensibility.store import DocumentStore
from forgeflow.schemas.autonomy import TERMINAL_RUN, Run, RunEvent, RunStatus


def _clean(value: Any) -> Any:
    """Evidence never carries credentials (additional.md section 4)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


class RunStore:
    def __init__(self, store: DocumentStore) -> None:
        self.store = store

    # ---------------------------------------------------------------- create

    async def create(self, run: Run) -> tuple[Run, bool]:
        """(run, created). An existing run for the same key is returned instead."""
        claimed = await self.store.insert(
            "run_keys",
            {
                "key": run.idempotency_key,
                "run_id": run.run_id,
                "trace_id": run.trace_id,
                "created_at": run.created_at,
            },
        )
        if not claimed:
            key = await self.store.get("run_keys", run.idempotency_key)
            existing = await self.get(key["run_id"]) if key else None
            if existing is not None:
                await self.event(existing.trace_id, "trigger.duplicate", {"trigger": run.trigger})
                return existing, False
        await self.store.put("autonomy_runs", run)
        await self.event(
            run.trace_id,
            "run.created",
            {
                "trigger": run.trigger,
                "source_item": run.source_item.model_dump(),
                "contract_version": run.contract_version,
                "contract_hash": run.contract_hash,
                "policy_version": run.policy_version,
                "action_profile_id": run.action_profile_id,
                "mode": run.mode,
                "limits": run.limits.model_dump(),
            },
            actor=f"trigger:{run.trigger}",
        )
        return run, True

    # ----------------------------------------------------------------- reads

    async def get(self, run_id: str) -> Run | None:
        doc = await self.store.get("autonomy_runs", run_id)
        return Run.model_validate(doc) if doc else None

    async def by_trace(self, trace_id: str) -> Run:
        docs = await self.store.find("autonomy_runs", {"trace_id": trace_id}, limit=1)
        if not docs:
            raise NotFoundError(f"run {trace_id} not found")
        return Run.model_validate(docs[0])

    async def by_workflow(self, workflow_id: str) -> Run | None:
        docs = await self.store.find("autonomy_runs", {"workflow_id": workflow_id}, limit=1)
        return Run.model_validate(docs[0]) if docs else None

    async def list_runs(self, status: list[str] | None = None, limit: int = 50) -> list[Run]:
        query = {"status": {"$in": status}} if status else {}
        docs = await self.store.find("autonomy_runs", query, sort="-created_at", limit=limit)
        return [Run.model_validate(d) for d in docs]

    async def active(self) -> list[Run]:
        return await self.list_runs([s for s in RunStatus if s not in TERMINAL_RUN], limit=500)

    # ---------------------------------------------------------------- writes

    async def transition(self, run: Run, to: RunStatus, **fields: Any) -> bool:
        """Compare-and-set on the current status. False if someone else moved the run."""
        now = utcnow()
        update: dict[str, Any] = {"status": str(to), "updated_at": now, **fields}
        if to in TERMINAL_RUN:
            update["finished_at"] = now
        ok = await self.store.compare_and_set(
            "autonomy_runs", run.run_id, {"status": str(run.status)}, _dump(update)
        )
        if ok:
            previous = run.status
            for key, value in update.items():
                setattr(run, key, value if key != "status" else to)
            await self.event(run.trace_id, "run.status", {"from": str(previous), "to": str(to)})
        return ok

    async def save(self, run: Run) -> None:
        """Persist non-status fields (counters, closure, plan) of a run the caller holds."""
        current = await self.get(run.run_id)
        if current is not None:
            run.status = current.status  # never overwrite a concurrent status change
            run.stop = current.stop or run.stop
        run.updated_at = utcnow()
        await self.store.put("autonomy_runs", run)

    async def event(
        self, trace_id: str, type: str, data: dict[str, Any] | None = None, actor: str = "commander"
    ) -> RunEvent:
        for _ in range(20):
            seq = len(await self.store.find("run_events", {"trace_id": trace_id}, limit=100000)) + 1
            record = RunEvent(
                event_id=f"{trace_id}:{seq:06d}",
                trace_id=trace_id,
                seq=seq,
                type=type,
                timestamp=utcnow(),
                actor=actor,
                data=_clean(data or {}),
            )
            if await self.store.insert("run_events", record):
                return record
        raise RuntimeError("could not append run event")

    async def events(self, trace_id: str) -> list[RunEvent]:
        docs = await self.store.find("run_events", {"trace_id": trace_id}, sort="seq", limit=100000)
        return [RunEvent.model_validate(d) for d in docs]


def _dump(update: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in update.items():
        if hasattr(value, "model_dump"):
            out[key] = value.model_dump(mode="python")
        elif isinstance(value, list):
            out[key] = [
                v.model_dump(mode="python") if hasattr(v, "model_dump") else v for v in value
            ]
        elif (
            isinstance(value, datetime)
            or value is None
            or isinstance(value, (str, int, float, bool, dict))
        ):
            out[key] = value
        else:
            out[key] = str(value)
    return out


def new_trace_id() -> str:
    return new_id("trace")
