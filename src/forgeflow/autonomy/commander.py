"""The commander (additional.md sections 2-3, 6): owns each run from trigger to decision.

    RECEIVED   re-read the source, check eligibility        -> CLOSE_NO_ACTION | PLANNING
    PLANNING   requirement analysis (Prepare), scope check   -> ESCALATE | plan
               build task_plan.json, validate, SAVE          -> PLANNED (workers may start)
    EXECUTING  supervise: counters, circuit breakers, repair -> RETRY | ESCALATE | VERIFYING
    VERIFYING  resolution comment, re-read GitHub, closure   -> RESOLVED | ESCALATED

The engineer defined the contract once; the commander plans each run itself and never
asks for approval of an ordinary plan. Every step appends evidence under the trace id.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from forgeflow.autonomy import planner
from forgeflow.autonomy.alerts import Alerts
from forgeflow.autonomy.closure import verify_closure
from forgeflow.autonomy.contract import load_contract
from forgeflow.autonomy.guard import measure, tripped_breaker
from forgeflow.autonomy.intake import eligibility, risk_allowed, source_item
from forgeflow.autonomy.learning import build_learning
from forgeflow.autonomy.runs import RunStore
from forgeflow.core.config import Settings
from forgeflow.core.errors import ForgeFlowError
from forgeflow.core.ids import utcnow
from forgeflow.core.logging import bind_context, log_event
from forgeflow.extensibility.catalog import github_capabilities
from forgeflow.extensibility.gateway import CapabilityDenied, Invocation
from forgeflow.integrations.github.client import GitHubError
from forgeflow.observability import metrics
from forgeflow.platform.orchestration.routing import plan_route
from forgeflow.platform.state.store import Commit
from forgeflow.schemas.autonomy import (
    TERMINAL_RUN,
    DecisionContract,
    Escalation,
    Run,
    RunStatus,
)
from forgeflow.schemas.requirement import RequiredCapabilities
from forgeflow.schemas.task import TaskStatus
from forgeflow.schemas.workflow import WorkflowStatus

logger = logging.getLogger(__name__)
TRANSIENT = (
    "TimeoutError",
    "ConnectionError",
    "AllProvidersFailed",
    "CIUnavailable",
    "GitHubError",
)
RUNNING = (RunStatus.PLANNED, RunStatus.EXECUTING, RunStatus.RETRYING)


class Commander:
    def __init__(
        self,
        container: Any,
        runs: RunStore,
        alerts: Alerts,
        settings: Settings,
    ) -> None:
        self.c = container
        self.runs = runs
        self.alerts = alerts
        self.settings = settings

    # ----------------------------------------------------------- dependencies

    def contract(self) -> DecisionContract:
        return load_contract(self.settings.autonomy_contract_path)

    async def github(self, contract: DecisionContract) -> Any:
        """GitHub client for the contract's service identity, or None if not configured."""
        ext = self.c.extensibility
        if ext is None or not contract.source.connector_id:
            return None
        doc = await ext.store.get("connectors", contract.source.connector_id)
        if doc is None or doc.get("status") != "active":
            return None
        connector = await ext.connectors.get(doc["owner_id"], contract.source.connector_id)
        return await ext.connectors.client(connector)

    def _locked(self, workflow_id: str) -> Any:
        """The workflow lock the event consumers use, so analysis/start never run twice."""
        redis = getattr(self.c, "redis", None)
        if redis is None:
            return contextlib.nullcontext()
        from forgeflow.platform.events.coordination import Coordinator

        return Coordinator(redis).workflow_lock(workflow_id)

    async def _analyze(self, workflow_id: str) -> None:
        async with self._locked(workflow_id):
            wf = await self.c.store.get_workflow(workflow_id)
            if wf.status == WorkflowStatus.PLANNING:
                await self.c.service.process_analysis(workflow_id, wf.requirement_version + 1)

    async def _start(self, workflow_id: str) -> Any:
        async with self._locked(workflow_id):
            return await self.c.execution.start_execution(workflow_id)

    def _inv(self, run: Run, project: Any = None) -> Invocation:
        return Invocation(
            owner_id=self.settings.local_user_id,
            workflow_id=run.workflow_id,
            task_id=None,
            agent="commander",
            project=project,
            action_profile=run.action_profile,
            trace_id=run.trace_id,
        )

    # ------------------------------------------------------------------- step

    async def step(self, run: Run) -> Run:
        """Advance one run by one state. Safe to call repeatedly (idempotent)."""
        bind_context(trace_id=run.trace_id, workflow_id=run.workflow_id)
        fresh = await self.runs.get(run.run_id)
        if fresh is None or fresh.status in TERMINAL_RUN:
            return fresh or run
        run = fresh
        try:
            if run.status == RunStatus.RECEIVED:
                await self._prepare(run)
            elif run.status == RunStatus.PLANNING:
                await self._plan(run)
            elif run.status in RUNNING:
                await self._supervise(run)
            elif run.status == RunStatus.VERIFYING:
                await self._verify(run)
        except ForgeFlowError as exc:
            log_event(logger, "commander step failed", logging.WARNING, error=str(exc)[:300])
            await self.runs.event(run.trace_id, "commander.error", {"error": str(exc)[:500]})
        return await self.runs.get(run.run_id) or run

    # ---------------------------------------------------------------- prepare

    async def _prepare(self, run: Run) -> None:
        contract = self.contract()
        busy = [r for r in await self.runs.active() if r.status in (*RUNNING, RunStatus.VERIFYING)]
        if len(busy) >= run.circuit_breakers.max_active_runs:
            await self.alerts.raise_alert(
                "backlog", f"{len(busy)} runs active; {run.trace_id} waits", run.trace_id
            )
            return
        client = await self.github(contract)
        if client is None:
            await self._escalate(
                run,
                "dependency_unavailable",
                "The source system cannot be read: no active service-identity GitHub connector "
                "is configured in the contract (source.connector_id).",
                "Connect GitHub with the service identity and set source.connector_id.",
                {"connector_id": contract.source.connector_id},
            )
            return
        try:
            issue = await client.get_issue(run.source_item.repository, run.source_item.number)
        except GitHubError as exc:
            await self.runs.event(run.trace_id, "source.read_failed", {"error": str(exc)})
            await self.alerts.raise_alert("source_failure", str(exc), run.trace_id)
            return  # retried next tick; the sweep/webhook may also re-trigger
        item = source_item(issue)
        await self.runs.event(
            run.trace_id,
            "source.reverified",
            {
                "state": item.state,
                "labels": item.labels,
                "content_hash": item.content_hash,
                "updated_at": item.updated_at,
            },
        )
        run.source_item = item
        await self.runs.save(run)
        eligible, reason = eligibility(item, contract)
        if not eligible:
            await self._close_no_action(run, reason, {"state": item.state, "labels": item.labels})
            return

        ext = self.c.extensibility
        repo_path = contract.source.project_repository_path
        project = await ext.projects.ensure(self.settings.local_user_id, repo_path)
        if project.github is None or project.github.repository != item.repository:
            await ext.projects.update(
                self.settings.local_user_id,
                project.project_id,
                github={
                    "connector_id": contract.source.connector_id,
                    "repository": item.repository,
                    "auto_pull_request": True,
                    "draft": True,
                },
            )
        request = (
            f"{item.title}\n\n{item.body}\n\nSource: {item.url} "
            f"({item.repository}#{item.number}). Fix the bug with the smallest safe change "
            "and cover it with an automated test."
        )
        wf = await self.c.service.create_workflow(request[:8000], repo_path, trace_id=run.trace_id)
        if not await self.runs.transition(
            run, RunStatus.PLANNING, workflow_id=wf.workflow_id, project_id=project.project_id
        ):
            return
        await self.runs.event(run.trace_id, "workflow.created", {"workflow_id": wf.workflow_id})
        await self._analyze(wf.workflow_id)  # same lock and idempotency as the event path

    # ------------------------------------------------------------------- plan

    async def _plan(self, run: Run) -> None:
        contract = self.contract()
        assert run.workflow_id is not None
        wf = await self.c.store.get_workflow(run.workflow_id)
        if wf.status == WorkflowStatus.PLANNING:
            await self._analyze(wf.workflow_id)
            wf = await self.c.store.get_workflow(run.workflow_id)
        if wf.status == WorkflowStatus.FAILED:
            provider = "AllProvidersFailed" in (wf.error or "") or "provider" in (wf.error or "")
            await self._escalate(
                run,
                "dependency_unavailable" if provider else "conflicting_requirements",
                f"Requirement analysis failed: {wf.error}",
                "Fix the provider configuration or clarify the issue, then start a new run.",
                {"workflow_error": wf.error},
            )
            return
        if wf.status != WorkflowStatus.PLANNED:
            return
        spec = await self.c.store.get_specification(wf.workflow_id)
        await self.runs.event(
            run.trace_id,
            "prepare.completed",
            {
                "specification_version": spec.version if spec else None,
                "risk_level": spec.risk_level if spec else None,
                "acceptance_criteria": [ac.model_dump() for ac in spec.acceptance_criteria]
                if spec
                else [],
                "assumptions": spec.assumptions if spec else [],
            },
        )
        if spec is None or not spec.required_capabilities.development:
            await self._escalate(
                run,
                "conflicting_requirements",
                "The analysis found no code change to make; the model alone cannot prove the "
                "issue invalid, so a human must decide.",
                "Confirm the issue is invalid/duplicate (close it) or clarify what should change.",
                {"specification": spec.model_dump(mode="json") if spec else None},
            )
            return
        if not risk_allowed(spec.risk_level, contract) or spec.requires_human_approval:
            await self._escalate(
                run,
                "out_of_scope",
                f"Risk {spec.risk_level}"
                + (" and human approval required" if spec.requires_human_approval else "")
                + f" exceeds the contract's {contract.eligibility.max_risk_level} limit.",
                "Handle this issue manually or widen the contract (a new contract version).",
                {
                    "risk_level": spec.risk_level,
                    "requires_human_approval": spec.requires_human_approval,
                },
            )
            return

        # The contract requires every verification stage for a code change.
        wf.route_plan = plan_route(
            RequiredCapabilities(
                development=True, code_review=True, security=True, qa=True, ci=True
            )
        )
        wf.route_plan.rationale = f"L4 contract {run.contract_version}: full verification"
        await self.c.store.commit(Commit(workflow=wf, expected_revision=wf.revision))

        models = planner.role_models(self.settings.fake_llm, getattr(self.c, "registry", None))
        workers = None
        for attempt in range(contract.plan.max_replans + 1):
            plan = planner.build_plan(
                run,
                wf.workflow_id,
                spec,
                contract,
                models,
                self.settings.max_parallel_tasks,
                workers,
            )
            errors, authority = planner.validate_plan(plan, run, contract)
            if authority:
                plan.status, plan.validation_errors = "REJECTED", authority
                await self.runs.event(
                    run.trace_id, "plan.rejected", {"plan": plan.model_dump(mode="json")}
                )
                metrics.L4_PLANS.labels("rejected_authority").inc()
                await self._escalate(
                    run,
                    "action_requires_authority",
                    "The plan needs actions the action profile does not authorise: "
                    + "; ".join(authority),
                    "Approve a new action-profile version or handle the issue manually.",
                    {"plan_hash": plan.hash, "violations": authority},
                )
                return
            if not errors:
                break
            plan.status, plan.validation_errors = "REJECTED", errors
            run.replans = attempt + 1
            await self.runs.event(
                run.trace_id, "plan.rejected", {"plan": plan.model_dump(mode="json")}
            )
            metrics.L4_PLANS.labels("rejected").inc()
            workers = min(run.limits.max_workers, plan.worker_count_requested)
        else:
            await self._escalate(
                run,
                "plan_invalid",
                "Plan validation failed and the bounded re-plan also failed: " + "; ".join(errors),
                "Fix the model allow-list / limits in a new contract version, or handle manually.",
                {"errors": errors, "replans": run.replans},
            )
            return

        plan.status = "VALIDATED_AND_SAVED"
        run.plan = plan
        await self.runs.save(run)  # durably saved BEFORE any worker starts
        await self.runs.event(run.trace_id, "plan.saved", {"plan": plan.model_dump(mode="json")})
        metrics.L4_PLANS.labels("saved").inc()
        if not await self.runs.transition(run, RunStatus.PLANNED):
            return
        started = await self._start(wf.workflow_id)
        if started is not None and await self.runs.transition(run, RunStatus.EXECUTING):
            await self.runs.event(
                run.trace_id,
                "execution.started",
                {"base_commit": started.execution.base_commit if started.execution else None},
            )

    # -------------------------------------------------------------- supervise

    async def _supervise(self, run: Run) -> None:
        assert run.workflow_id is not None
        if run.status == RunStatus.PLANNED:
            await self._start(run.workflow_id)
            if (await self.c.store.get_workflow(run.workflow_id)).execution is not None:
                await self.runs.transition(run, RunStatus.EXECUTING)
        wf = await self.c.store.get_workflow(run.workflow_id)
        tasks = await self.c.store.list_tasks(wf.workflow_id)
        agent_runs = await self.c.store.list_agent_runs(wf.workflow_id)
        audit = await self.c.extensibility.store.find(
            "capability_audit", {"workflow_id": wf.workflow_id}, limit=10000
        )
        run.counters = measure(run, tasks, agent_runs, audit)
        await self.runs.save(run)

        if (breaker := tripped_breaker(run)) is not None:
            await self._guardrail(run, *breaker)
            return
        pending = await self.c.extensibility.store.find(
            "approvals", {"workflow_id": wf.workflow_id, "status": "pending"}
        )
        if pending and run.escalation is None:
            # An ASK action is waiting: escalate with the exact decision needed.
            await self._record_escalation(
                run,
                "action_requires_authority",
                f"{pending[0]['summary']} needs a human decision (ASK in {run.action_profile_id}).",
                f"Approve or reject approval {pending[0]['approval_id']}.",
                {
                    "approval_id": pending[0]["approval_id"],
                    "capability": pending[0]["capability_id"],
                },
            )
        if wf.status == WorkflowStatus.COMPLETED:
            if await self.runs.transition(run, RunStatus.VERIFYING):
                await self._verify(run)
            return
        if wf.status == WorkflowStatus.PAUSED and wf.execution and wf.execution.awaiting_decision:
            await self._escalate(
                run,
                "blocking_review",
                f"Blocking findings remain after the repair loop: {wf.error}",
                "Accept the risk, fix manually, or reject the change.",
                self._verification_evidence(tasks),
                attempted=[
                    f"{t.key}: {t.result.summary[:120]}"
                    for t in tasks
                    if t.kind == "repair" and t.result
                ],
            )
            return
        if wf.status == WorkflowStatus.PAUSED:
            await self._failed_tasks(run, tasks)
            return
        if wf.status in (WorkflowStatus.FAILED, WorkflowStatus.CANCELLED):
            await self._escalate(
                run,
                "verification_failed",
                f"The workflow ended {wf.status}: {wf.error}",
                "Inspect the workflow and decide whether to start a new run.",
                {"workflow_error": wf.error},
            )
            return
        if run.counters.runtime_seconds > self.contract().alerts.run_stuck_after_seconds:
            await self.alerts.raise_alert(
                "run_stuck",
                f"{run.trace_id} running for {run.counters.runtime_seconds:.0f}s",
                run.trace_id,
            )

    async def _failed_tasks(self, run: Run, tasks: list[Any]) -> None:
        failed = [t for t in tasks if t.status == TaskStatus.FAILED]
        if not failed:
            return
        transient = all(any(name in (t.error or "") for name in TRANSIENT) for t in failed)
        budget = run.counters.retries < run.limits.budgets.max_retries
        if transient and budget:
            run.decision, run.decision_reason = "RETRY", f"transient failure: {failed[0].error}"
            await self.runs.save(run)
            metrics.L4_DECISIONS.labels("RETRY").inc()
            await self.runs.event(
                run.trace_id,
                "decision.retry",
                {
                    "tasks": [t.task_id for t in failed],
                    "errors": [t.error for t in failed],
                    "retries": run.counters.retries,
                    "max_retries": run.limits.budgets.max_retries,
                },
            )
            for t in failed:
                await self.c.execution.retry_task(t.task_id)
            return
        await self._escalate(
            run,
            "retry_limit",
            f"{len(failed)} task(s) failed"
            + ("" if transient else " with non-transient errors")
            + f" after {run.counters.retries} retries: "
            + "; ".join(f"{t.key}: {t.error}" for t in failed)[:600],
            "Inspect the failure and fix the environment or the code, then resume or start "
            "a new run.",
            {
                "failed": [
                    {"task": t.task_id, "error": t.error, "attempt": t.attempt} for t in failed
                ]
            },
            attempted=[f"{t.key} attempt {t.attempt}" for t in failed],
        )

    # ----------------------------------------------------------------- verify

    async def _verify(self, run: Run) -> None:
        contract = self.contract()
        assert run.workflow_id is not None
        wf = await self.c.store.get_workflow(run.workflow_id)
        tasks = await self.c.store.list_tasks(wf.workflow_id)
        agent_runs = await self.c.store.list_agent_runs(wf.workflow_id)
        client = await self.github(contract)
        if run.mode == "observe":
            evidence = self._verification_evidence(tasks)
            evidence["would_resolve"] = all(
                not (t.result and t.result.blocking)
                for t in tasks
                if t.kind in ("review", "security", "qa", "ci")
            )
            await self._escalate(
                run,
                "action_requires_authority",
                "Observe mode: the change was built and verified locally; pushing, the draft "
                "PR and "
                "issue comments were not made. Recorded decision: "
                + ("would RESOLVE" if evidence["would_resolve"] else "would ESCALATE"),
                "Review the recorded decision; set `mode: autonomous` in a new contract version "
                "to let ForgeFlow act.",
                evidence,
            )
            return
        commit = (wf.execution.target_commit or "") if wf.execution else ""
        if wf.pull_request and client is not None:
            await self._comment(
                run,
                client,
                f"ForgeFlow opened draft PR {wf.pull_request.url} for this issue.\n\n"
                f"- trace: `{run.trace_id}`\n- commit: `{commit[:12]}`\n"
                f"- review, security, QA and CI passed; closure is verified by re-reading GitHub.",
                "resolution",
            )
        events = len(await self.runs.events(run.trace_id))
        run.closure = await verify_closure(run, wf, tasks, agent_runs, client, events)
        await self.runs.save(run)
        await self.runs.event(
            run.trace_id, "closure.verified", {"checks": [c.model_dump() for c in run.closure]}
        )
        failed = [c for c in run.closure if not c.passed]
        review = next((c for c in run.closure if c.name == "independent_review_passes"), None)
        if review is not None:
            metrics.L4_REVIEW.labels("passed" if review.passed else "failed").inc()
        if failed:
            for c in failed:
                metrics.L4_CLOSURE_MISMATCH.labels(c.name).inc()
            await self.alerts.raise_alert(
                "closure_mismatch", "; ".join(f"{c.name}: {c.detail}" for c in failed), run.trace_id
            )
            await self._escalate(
                run,
                "verification_failed",
                "Closure could not be established: "
                + "; ".join(f"{c.name}: {c.detail}" for c in failed),
                "Inspect the failed closure checks; nothing was marked resolved.",
                {"closure": [c.model_dump() for c in run.closure]},
            )
            return
        run.decision, run.decision_reason = "RESOLVE", "every closure check passed"
        await self.runs.save(run)
        if await self.runs.transition(
            run, RunStatus.RESOLVED, decision="RESOLVE", decision_reason=run.decision_reason
        ):
            await self.runs.event(
                run.trace_id,
                "decision.resolve",
                {
                    "pull_request": wf.pull_request.model_dump(mode="json")
                    if wf.pull_request
                    else None
                },
            )
            await self._finish(run)

    # --------------------------------------------------------------- outcomes

    async def _comment(self, run: Run, client: Any, body: str, kind: str) -> bool:
        connector_id = self.contract().source.connector_id
        item = run.source_item
        caps = {
            c.capability_id: c
            for c in github_capabilities(connector_id, self.settings.local_user_id)
        }
        ext = self.c.extensibility

        async def connector_status() -> str:
            return await ext.connectors.status(connector_id)

        try:
            posted = await ext.gateway.invoke(
                self._inv(run),
                caps["github.issue.comment"],
                lambda: client.comment_once(
                    run.source_item.repository, run.source_item.number, body, run.trace_id, kind
                ),
                summary=f"Comment ({kind}) on {item.repository}#{item.number}",
                source_status=connector_status,
            )
        except CapabilityDenied as exc:
            await self.runs.event(
                run.trace_id,
                "action.blocked",
                {"action": "github.issue.comment", "kind": kind, "reason": str(exc)},
            )
            return False
        except GitHubError as exc:
            await self.runs.event(
                run.trace_id, "action.failed", {"action": "github.issue.comment", "error": str(exc)}
            )
            return False
        if posted:
            run.counters.comments_posted += 1
            await self.runs.save(run)
        await self.runs.event(run.trace_id, "action.comment", {"kind": kind, "posted": posted})
        return True

    async def _close_no_action(self, run: Run, reason: str, evidence: dict[str, Any]) -> None:
        if await self.runs.transition(
            run, RunStatus.CLOSED_NO_ACTION, decision="CLOSE_NO_ACTION", decision_reason=reason
        ):
            metrics.L4_DECISIONS.labels("CLOSE_NO_ACTION").inc()
            await self.runs.event(
                run.trace_id, "decision.close_no_action", {"reason": reason, "evidence": evidence}
            )
            await self._finish(run)

    async def _record_escalation(
        self,
        run: Run,
        condition: str,
        summary: str,
        decision_needed: str,
        evidence: dict[str, Any],
        attempted: list[str] | None = None,
    ) -> Escalation:
        contract = self.contract()
        escalation = Escalation(
            condition=condition,
            rule=contract.escalation_conditions.get(condition, condition),
            summary=summary[:2000],
            decision_needed=decision_needed,
            evidence={
                **evidence,
                "source_item": run.source_item.model_dump(),
                "trace_id": run.trace_id,
                "plan_hash": run.plan.hash if run.plan else None,
                "workflow_id": run.workflow_id,
                "counters": run.counters.model_dump(),
            },
            attempted=attempted or [],
            escalated_at=utcnow(),
            delivered_to=["forgeflow-ui", "alerts"],
        )
        run.escalation = escalation
        await self.runs.save(run)
        metrics.L4_ESCALATIONS.labels(condition).inc()
        await self.runs.event(run.trace_id, "escalation", escalation.model_dump(mode="json"))
        await self.alerts.raise_alert(
            "escalation",
            f"[{condition}] {summary}",
            run.trace_id,
            details={"decision_needed": decision_needed},
        )
        return escalation

    async def _escalate(
        self,
        run: Run,
        condition: str,
        summary: str,
        decision_needed: str,
        evidence: dict[str, Any],
        attempted: list[str] | None = None,
    ) -> None:
        if run.workflow_id:
            # Work that never started must not linger as an "active" workflow.
            wf = await self.c.store.get_workflow(run.workflow_id)
            if wf.execution is None and wf.status in (
                WorkflowStatus.PLANNING,
                WorkflowStatus.PLANNED,
                WorkflowStatus.AWAITING_CLARIFICATION,
            ):
                await self.c.service.cancel_workflow(run.workflow_id)
        escalation = await self._record_escalation(
            run, condition, summary, decision_needed, evidence, attempted
        )
        if await self.runs.transition(
            run,
            RunStatus.ESCALATED,
            decision="ESCALATE",
            decision_reason=f"{condition}: {summary[:300]}",
        ):
            metrics.L4_DECISIONS.labels("ESCALATE").inc()
            client = await self.github(self.contract())
            if client is not None and run.mode == "autonomous":
                if await self._comment(
                    run,
                    client,
                    "ForgeFlow needs a human decision on this issue.\n\n"
                    f"- condition: `{condition}` "
                    f"({escalation.rule})\n- what happened: {summary[:800]}\n- decision needed: "
                    f"{decision_needed}\n- trace: `{run.trace_id}`",
                    "escalation",
                ):
                    escalation.delivered_to.append("github-issue")
                    run.escalation = escalation
                    await self.runs.save(run)
            await self._finish(run)

    async def _guardrail(self, run: Run, breaker: str, detail: str) -> None:
        if not await self.runs.transition(
            run, RunStatus.PAUSED_BY_GUARDRAIL, guardrail=f"{breaker}: {detail}"
        ):
            return
        metrics.L4_BREAKER.labels(breaker).inc()
        if run.workflow_id:
            await self.c.execution.halt_for_stop(run.workflow_id, f"circuit breaker {breaker}")
        await self.alerts.raise_alert(
            "circuit_breaker", f"{breaker}: {detail}", run.trace_id, "critical"
        )
        if breaker == "budget":
            await self.alerts.raise_alert("budget_exhausted", detail, run.trace_id, "critical")
        await self._record_escalation(
            run,
            "circuit_breaker",
            f"Circuit breaker '{breaker}' tripped: {detail}",
            "Investigate; resume only if budgets allow, otherwise start a new run.",
            {"breaker": breaker, "detail": detail},
        )

    @staticmethod
    def _verification_evidence(tasks: list[Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for t in tasks:
            if t.kind in ("review", "security", "qa", "ci") and t.result:
                out[f"{t.kind}_round_{t.round}"] = {
                    "verdict": t.result.verdict,
                    "blocking": t.result.blocking,
                    "reasons": t.result.blocking_reasons[:10],
                }
        return out

    async def _finish(self, run: Run) -> None:
        """Terminal bookkeeping: metrics and the learning record (section 7)."""
        tasks = await self.c.store.list_tasks(run.workflow_id) if run.workflow_id else []
        audit = (
            await self.c.extensibility.store.find(
                "capability_audit", {"workflow_id": run.workflow_id}, limit=10000
            )
            if run.workflow_id
            else []
        )
        approvals = (
            await self.c.extensibility.store.find("approvals", {"workflow_id": run.workflow_id})
            if run.workflow_id
            else []
        )
        final = await self.runs.get(run.run_id) or run
        learning = build_learning(final, tasks, audit, approvals)
        await self.c.extensibility.store.put("learning_records", learning)
        await self.runs.event(
            run.trace_id,
            "learning.recorded",
            {"learning_id": learning.learning_id, "candidates": len(learning.candidates)},
        )
        human = "yes" if learning.human_interventions else "no"
        metrics.L4_RUNS.labels(final.decision or "none", human).inc()
        for i in learning.human_interventions:
            metrics.L4_INTERVENTIONS.labels(i["kind"]).inc()
        metrics.L4_TOKENS.inc(final.counters.tokens)
        metrics.L4_COST.inc(final.counters.cost_usd)
        metrics.L4_WORKERS.observe(final.counters.max_parallel_observed)
        metrics.L4_DURATION.observe((utcnow() - final.created_at).total_seconds())
