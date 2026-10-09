"""Emergency stop, resume and explicit re-run (additional.md section 5).

stop    persist the request -> block spawns and external actions at once (the guards read
        the run status) -> halt active tasks -> reject pending approvals -> checkpoint ->
        STOPPED. Idempotent.
resume  only STOPPED / PAUSED_BY_GUARDRAIL runs, only by an operator, only if the action
        profile is still the contract's and budgets allow; re-reads GitHub first, then
        re-queues exactly the halted tasks. Side effects stay idempotent (PR creation
        finds the existing PR, pushes are the same commit, comments carry trace markers).
rerun   a new run (new trace id) for the same source item; never a silent restart.
"""

from __future__ import annotations

import time
from typing import Any

from forgeflow.autonomy.alerts import Alerts
from forgeflow.autonomy.guard import _exceeded
from forgeflow.autonomy.intake import eligibility, intake, source_item
from forgeflow.autonomy.runs import RunStore
from forgeflow.core.errors import ValidationFailed
from forgeflow.core.ids import utcnow
from forgeflow.integrations.github.client import GitHubError
from forgeflow.observability import metrics
from forgeflow.schemas.autonomy import TERMINAL_RUN, Run, RunStatus, StopRecord
from forgeflow.schemas.task import TaskStatus


class RunControl:
    def __init__(self, container: Any, runs: RunStore, alerts: Alerts, commander: Any) -> None:
        self.c = container
        self.runs = runs
        self.alerts = alerts
        self.commander = commander

    async def stop(self, trace_id: str, actor: str, reason: str) -> Run:
        started = time.perf_counter()
        for _ in range(5):
            run = await self.runs.by_trace(trace_id)
            if run.status in (RunStatus.STOPPING, RunStatus.STOPPED):
                return run  # idempotent
            if run.status in TERMINAL_RUN:
                raise ValidationFailed(f"run {trace_id} already finished ({run.status})")
            record = StopRecord(actor=actor, reason=reason[:500], requested_at=utcnow())
            if await self.runs.transition(run, RunStatus.STOPPING, stop=record):
                break
        else:
            raise ValidationFailed("the run kept changing; try the stop again")
        metrics.L4_STOPS.inc()
        await self.runs.event(trace_id, "run.stop_requested", {"reason": reason}, actor=actor)
        # From here the spawn and gateway guards refuse new work and external actions.
        record.acknowledged_at = utcnow()
        await self.runs.event(
            trace_id,
            "run.stop_acknowledged",
            {
                "new_spawns": "blocked",
                "external_actions": "blocked",
                "note": "requests already in flight at this moment may still complete",
            },
            actor="commander",
        )
        if run.workflow_id:
            record.cancelled_tasks = await self.c.execution.halt_for_stop(run.workflow_id, reason)
            ext = self.c.extensibility
            for approval in await ext.store.find(
                "approvals", {"workflow_id": run.workflow_id, "status": "pending"}
            ):
                try:
                    await ext.approvals.decide(
                        approval["approval_id"],
                        False,
                        approval["owner_id"],
                        note=f"rejected by emergency stop ({actor}): {reason}"[:300],
                    )
                    record.rejected_approvals.append(approval["approval_id"])
                except ValidationFailed:
                    pass  # decided concurrently
            tasks = await self.c.store.list_tasks(run.workflow_id)
            wf = await self.c.store.get_workflow(run.workflow_id)
            record.checkpoint = {
                "workflow_status": str(wf.status),
                "target_commit": wf.execution.target_commit if wf.execution else None,
                "tasks": {
                    t.task_id: {
                        "status": str(t.status),
                        "attempt": t.attempt,
                        "commit": t.result.commit if t.result else None,
                    }
                    for t in tasks
                },
                "plan_hash": run.plan.hash if run.plan else None,
                "counters": run.counters.model_dump(),
            }
        record.stopped_at = utcnow()
        stopping = await self.runs.by_trace(trace_id)
        await self.runs.transition(stopping, RunStatus.STOPPED, stop=record)
        latency = time.perf_counter() - started
        metrics.L4_STOP_LATENCY.observe(latency)
        await self.runs.event(
            trace_id,
            "run.stopped",
            {
                "cancelled_tasks": record.cancelled_tasks,
                "rejected_approvals": record.rejected_approvals,
                "checkpoint": record.checkpoint,
                "stop_latency_seconds": round(latency, 3),
            },
            actor=actor,
        )
        await self.alerts.raise_alert(
            "emergency_stop", f"{trace_id} stopped by {actor}: {reason}", trace_id, "critical"
        )
        return await self.runs.by_trace(trace_id)

    async def resume(self, trace_id: str, actor: str) -> Run:
        run = await self.runs.by_trace(trace_id)
        if run.status not in (RunStatus.STOPPED, RunStatus.PAUSED_BY_GUARDRAIL):
            raise ValidationFailed(
                f"only stopped or guardrail-paused runs can be resumed ({run.status})"
            )
        contract = self.commander.contract()

        async def refuse(reason: str) -> Run:
            metrics.L4_RESUMES.labels("refused").inc()
            await self.runs.event(trace_id, "run.resume_refused", {"reason": reason}, actor=actor)
            raise ValidationFailed(f"cannot resume: {reason}")

        if (
            contract.action_profile.id != run.action_profile_id
            or contract.version != run.contract_version
        ):
            return await refuse("the contract or action profile changed; start a new run")
        if (detail := _exceeded(run)) is not None:
            return await refuse(f"{detail}; budgets cannot be raised mid-run - start a new run")
        client = await self.commander.github(contract)
        if client is None:
            return await refuse("the source system cannot be read (no service-identity connector)")
        try:
            issue = await client.get_issue(run.source_item.repository, run.source_item.number)
        except GitHubError as exc:
            return await refuse(f"re-reading the source failed: {exc}")
        item = source_item(issue)
        eligible, reason = eligibility(item, contract)
        await self.runs.event(
            trace_id,
            "source.reverified",
            {
                "state": item.state,
                "labels": item.labels,
                "eligible": eligible,
                "content_hash": item.content_hash,
                "changed": item.content_hash != run.source_item.content_hash,
            },
            actor=actor,
        )
        if not eligible:
            return await refuse(f"the issue is no longer eligible: {reason}")

        target = (
            RunStatus.EXECUTING
            if run.plan and run.plan.status == "VALIDATED_AND_SAVED"
            else (RunStatus.PLANNING if run.workflow_id else RunStatus.RECEIVED)
        )
        if not await self.runs.transition(run, target, resumes=run.resumes + 1, guardrail=None):
            return await refuse("the run changed concurrently")
        requeued = []
        if run.workflow_id and target == RunStatus.EXECUTING:
            wf = await self.c.store.get_workflow(run.workflow_id)
            if wf.execution is None:
                await self.c.execution.start_execution(run.workflow_id)
            for t in await self.c.store.list_tasks(run.workflow_id):
                if t.status == TaskStatus.FAILED and (t.error or "").startswith("emergency stop"):
                    await self.c.execution.retry_task(t.task_id)
                    requeued.append(t.task_id)
            await self.c.execution.tick(run.workflow_id)
        metrics.L4_RESUMES.labels("resumed").inc()
        await self.runs.event(
            trace_id, "run.resumed", {"requeued": requeued, "status": str(target)}, actor=actor
        )
        return await self.runs.by_trace(trace_id)

    async def rerun(self, trace_id: str, actor: str) -> Run:
        run = await self.runs.by_trace(trace_id)
        if run.status not in (*TERMINAL_RUN, RunStatus.STOPPED):
            raise ValidationFailed("only finished or stopped runs can be re-run")
        contract = self.commander.contract()
        same_item = [
            r
            for r in await self.runs.list_runs(limit=1000)
            if r.source_item.repository == run.source_item.repository
            and r.source_item.number == run.source_item.number
        ]
        generation = max(1, len(same_item))
        new, _ = await intake(self.runs, contract, run.source_item, "manual", generation=generation)
        await self.runs.event(trace_id, "run.rerun", {"new_trace_id": new.trace_id}, actor=actor)
        await self.runs.event(
            new.trace_id, "run.rerun_of", {"previous_trace_id": trace_id}, actor=actor
        )
        return new
