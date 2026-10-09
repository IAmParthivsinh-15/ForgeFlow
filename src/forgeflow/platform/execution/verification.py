"""Verification stage handlers: Code Review, Security, QA, CI (spec sections 11-14, 53-57).

Each stage runs on a disposable detached checkout of the commit under
verification. Agents reason and report; ForgeFlow decides the verdict and
whether it blocks, using rules that agents cannot override:

    review    blocking if decision is 'blocked', or 'changes_requested' with a high+ finding
    security  blocking if a high/critical finding (agent or scanner) touches the change
    qa        blocking if any acceptance criterion FAILs or the test command fails;
              PASS without executed passing tests is downgraded to UNCERTAIN;
              a browser_test PASS needs successful Playwright MCP actions in this run
    ci        blocking unless the Jenkins build result is SUCCESS
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from typing import Any

from forgeflow.agents.context import AgentRuntimeContext
from forgeflow.core.config import Settings
from forgeflow.core.errors import CIUnavailable, PolicyViolation, ValidationFailed
from forgeflow.core.ids import utcnow
from forgeflow.extensibility.projects import project_id_for
from forgeflow.platform.a2a.channel import A2AChannel
from forgeflow.platform.ci.jenkins import CIProvider, JenkinsProvider
from forgeflow.platform.ci.pipeline import build_pipeline_spec, render_jenkinsfile
from forgeflow.platform.execution.common import (
    HandlerOutput,
    agent_event,
    check_event,
    run_from_outcome,
    run_with_capabilities,
)
from forgeflow.platform.orchestration.execution import task_event
from forgeflow.platform.orchestration.gateway import (
    A2ARequest,
    AgentGateway,
    CIRequest,
    QARequest,
    ReviewRequest,
    SecurityRequest,
)
from forgeflow.platform.state.store import WorkflowStore
from forgeflow.platform.verification.change_analyzer import analyze_changes
from forgeflow.platform.worktrees.manager import PreparedWorkspace, WorktreeManager
from forgeflow.schemas.events import EventType
from forgeflow.schemas.requirement import RequirementSpecification
from forgeflow.schemas.task import Task, TaskResult, TaskStatus
from forgeflow.schemas.verification import (
    BLOCKING_SEVERITIES,
    OWASP_TOP_10_2021,
    CIReport,
    CriterionResult,
    QAReport,
    ReviewFinding,
    SecurityFinding,
    SecurityReport,
)
from forgeflow.schemas.workflow import AgentRunRecord, Workflow
from forgeflow.tools.browser.preview import PortPool, PreviewServer, parse_ports, preview_host
from forgeflow.tools.git.client import GitClient
from forgeflow.tools.security.scanners import ScannerSuite
from forgeflow.tools.shell.commands import CheckRunner

TEST_VERIFICATIONS = frozenset(
    {"unit_test", "integration_test", "api_test", "e2e_test", "smoke_test", "ci_pipeline"}
)


class VerificationStages:
    def __init__(
        self,
        store: WorkflowStore,
        gateway: AgentGateway,
        worktrees: WorktreeManager,
        git: GitClient,
        settings: Settings,
        *,
        ci: CIProvider | None,
        scanners: ScannerSuite,
        extensibility: Any = None,
        knowledge: Any = None,
        artifacts: Any = None,
    ) -> None:
        self.store = store
        self.knowledge = knowledge
        self.artifacts = artifacts
        self.preview_ports = PortPool(parse_ports(settings.preview_ports))
        self.gateway = gateway
        self.worktrees = worktrees
        self.git = git
        self.settings = settings
        self.scanners = scanners
        self.extensibility = extensibility
        self.ci = ci or JenkinsProvider(
            settings.jenkins_url,
            settings.jenkins_user,
            settings.jenkins_password,
            poll_interval=settings.ci_poll_interval_seconds,
            timeout=settings.ci_timeout_seconds,
            public_url=settings.jenkins_public_url,
        )

    async def run(self, wf: Workflow, task: Task, spec: RequirementSpecification) -> HandlerOutput:
        assert wf.execution is not None and wf.repository_path is not None
        target = wf.execution.target_commit or wf.execution.base_commit
        checkout = await self.worktrees.create(
            workflow_id=wf.workflow_id,
            task_id=task.task_id,
            key=task.key,
            repository_path=wf.repository_path,
            base_commit=target,
            detach=True,
        )
        try:
            handler = {
                "review": self._review,
                "security": self._security,
                "qa": self._qa,
                "ci": self._ci,
            }[task.kind]
            return await handler(wf, task, spec, checkout)
        finally:
            await self.worktrees.remove(checkout.workspace)

    # ------------------------------------------------------------------ shared

    async def _capable(self, wf, task, agent, ctx, spec, call, allowed_origins=None):
        """Run an agent call with its resolved skills and MCP tools."""
        context = " ".join(
            [
                spec.summary,
                spec.goal,
                *spec.scope.in_scope,
                *(c.description for c in spec.checklist),
                *(
                    f"{ac.description} ({ac.verification.replace('_', ' ')})"
                    for ac in spec.acceptance_criteria
                ),
                task.title,
                task.instructions,
                *task.file_scope,
            ]
        )
        return await run_with_capabilities(
            self.extensibility,
            self.settings.local_user_id,
            wf,
            task,
            agent,
            ctx,
            context,
            call,
            allowed_origins=allowed_origins,
        )

    def _repository_id(self, wf: Workflow) -> str:
        return wf.project_id or project_id_for(wf.repository_path or "")

    async def _change(self, wf: Workflow) -> tuple[str, list[str]]:
        """(diff, changed files) between the base and the commit under verification."""
        assert wf.execution is not None and wf.repository_path is not None
        target = wf.execution.target_commit or wf.execution.base_commit
        if target == wf.execution.base_commit:
            return "", []
        repo = self.worktrees.repository(wf.repository_path)
        return (
            await self.git.diff(repo, wf.execution.base_commit, target),
            await self.git.files_between(repo, wf.execution.base_commit, target),
        )

    def _channel(
        self,
        wf: Workflow,
        task: Task,
        spec: RequirementSpecification,
        root: Path,
        runs: list[AgentRunRecord],
    ) -> A2AChannel:
        async def respond(question: str, context: str) -> str:
            ctx = AgentRuntimeContext(
                workflow_id=wf.workflow_id, task_id=task.task_id, repository_root=root
            )
            started = utcnow()
            outcome = await self.gateway.answer_a2a(
                ctx, A2ARequest(spec, asker=task.agent_type, question=question, context=context)
            )
            runs.append(run_from_outcome(task, "developer (a2a)", outcome, started))
            return outcome.output.answer

        return A2AChannel(
            workflow_id=wf.workflow_id,
            task_id=task.task_id,
            sender=task.agent_type,
            receiver="developer",
            responder=respond,
            timeout_seconds=self.settings.a2a_timeout_seconds,
            max_messages=self.settings.a2a_max_per_run,
        )

    async def _previous(self, task: Task) -> Task | None:
        """The same stage's result from the previous verification round."""
        tasks = await self.store.list_tasks(task.workflow_id)
        earlier = [
            t
            for t in tasks
            if t.kind == task.kind and t.round < task.round and t.status == TaskStatus.COMPLETED
        ]
        return max(earlier, key=lambda t: t.round) if earlier else None

    def _output(
        self,
        task: Task,
        result: TaskResult,
        runs: list[AgentRunRecord],
        channel: A2AChannel | None,
        checks: list | None = None,
    ) -> HandlerOutput:
        messages = channel.messages if channel else []
        result.a2a_messages = len(messages)
        events = [check_event(task, c) for c in checks or []]
        events += [agent_event(task, r) for r in runs]
        return HandlerOutput(result=result, agent_runs=runs, a2a_messages=messages, events=events)

    # ------------------------------------------------------------------ review

    async def _review(
        self, wf: Workflow, task: Task, spec: RequirementSpecification, checkout: PreparedWorkspace
    ) -> HandlerOutput:
        diff, files = await self._change(wf)
        change = analyze_changes(files)
        previous = await self._previous(task)
        prior: list[ReviewFinding] = (
            [f for f in previous.result.review.findings if f.severity in BLOCKING_SEVERITIES]
            if previous and previous.result and previous.result.review
            else []
        )
        runs: list[AgentRunRecord] = []
        channel = self._channel(wf, task, spec, checkout.path, runs)
        ctx = AgentRuntimeContext(
            workflow_id=wf.workflow_id,
            task_id=task.task_id,
            repository_root=checkout.path,
            diff=diff,
            a2a=channel,
        )
        started = utcnow()
        outcome, manifest = await self._capable(
            wf,
            task,
            "code_review",
            ctx,
            spec,
            lambda: self.gateway.review_changes(
                ctx, ReviewRequest(spec, change, diff, round=task.round, previous_findings=prior)
            ),
        )
        runs.insert(0, run_from_outcome(task, "code_review", outcome, started))
        report = outcome.output
        severe = [f for f in report.findings if f.severity in BLOCKING_SEVERITIES]
        blocking = report.decision == "blocked" or (
            report.decision == "changes_requested" and bool(severe)
        )
        reasons = [
            f"review [{f.severity}] {f.file or '-'}:{f.line or '-'} {f.message}" for f in severe
        ]
        if blocking and not reasons:
            reasons = [f"review blocked: {report.summary}"]
        result = TaskResult(
            capability_manifest=manifest,
            summary=report.summary,
            verdict="fail" if blocking else "pass",
            blocking=blocking,
            blocking_reasons=reasons,
            change_analysis=change,
            review=report,
            files_changed=files,
        )
        return self._output(task, result, runs, channel)

    # ---------------------------------------------------------------- security

    async def _security(
        self, wf: Workflow, task: Task, spec: RequirementSpecification, checkout: PreparedWorkspace
    ) -> HandlerOutput:
        diff, files = await self._change(wf)
        change = analyze_changes(files)
        scans = await self.scanners.run(checkout.path)
        previous = await self._previous(task)
        prior: list[SecurityFinding] = (
            [f for f in previous.result.security.findings if f.severity in BLOCKING_SEVERITIES]
            if previous and previous.result and previous.result.security
            else []
        )
        runs: list[AgentRunRecord] = []
        channel = self._channel(wf, task, spec, checkout.path, runs)
        ctx = AgentRuntimeContext(
            workflow_id=wf.workflow_id,
            task_id=task.task_id,
            repository_root=checkout.path,
            diff=diff,
            a2a=channel,
        )
        started = utcnow()
        outcome, manifest = await self._capable(
            wf,
            task,
            "security",
            ctx,
            spec,
            lambda: self.gateway.assess_security(
                ctx,
                SecurityRequest(
                    spec,
                    change,
                    diff,
                    scans,
                    self.settings.owasp_edition,
                    round=task.round,
                    previous_findings=prior,
                ),
            ),
        )
        runs.insert(0, run_from_outcome(task, "security", outcome, started))
        assessment = outcome.output
        dismissed = {fp.split(":", 1)[0].strip() for fp in assessment.false_positives}
        findings = list(assessment.findings)
        for scan in scans:
            for f in scan.findings:
                if f.rule_id in dismissed:
                    continue
                findings.append(
                    SecurityFinding(
                        category=f.category,
                        severity=f.severity,
                        file=f.file,
                        line=f.line,
                        evidence=f.evidence or f.message,
                        impact=f.message,
                        remediation=f"Address {f.tool} rule {f.rule_id}.",
                        source=f.tool,
                    )
                )
        # Categories the agent did not report are explicitly uncertain, never assumed safe.
        reported = {c.id for c in assessment.categories}
        categories = list(assessment.categories) + [
            {"id": cid, "status": "uncertain", "notes": "not evaluated by the agent"}
            for cid in OWASP_TOP_10_2021
            if cid not in reported
        ]
        report = SecurityReport(
            owasp_edition=self.settings.owasp_edition,
            summary=assessment.summary,
            categories=categories,  # type: ignore[arg-type]
            findings=findings,
            scanners=scans,
            false_positives=assessment.false_positives,
        )
        # With a diff, only findings in changed files block; a pure audit blocks on all.
        in_scope = set(files)
        severe = [
            f
            for f in findings
            if f.severity in BLOCKING_SEVERITIES
            and (not in_scope or f.file is None or f.file in in_scope)
        ]
        incomplete = any(s.status in ("unavailable", "error") for s in scans)
        blocking = bool(severe)
        result = TaskResult(
            capability_manifest=manifest,
            summary=assessment.summary,
            verdict="fail" if blocking else ("uncertain" if incomplete else "pass"),
            blocking=blocking,
            blocking_reasons=[
                f"security [{f.severity}] {f.category} {f.file or '-'}:{f.line or '-'} "
                f"{f.impact[:160]}"
                for f in severe
            ],
            change_analysis=change,
            security=report,
            files_changed=files,
            risks=[
                f"{s.tool} {s.status}: {s.detail}"
                for s in scans
                if s.status in ("unavailable", "error")
            ],
        )
        return self._output(task, result, runs, channel)

    # ---------------------------------------------------------------------- qa

    async def _qa(
        self, wf: Workflow, task: Task, spec: RequirementSpecification, checkout: PreparedWorkspace
    ) -> HandlerOutput:
        diff, files = await self._change(wf)
        checks = CheckRunner(
            checkout.path, self.settings.check_timeout_seconds, exclude_path=sys.prefix
        )
        available = [k for k in checks.available() if k != "setup"]
        # Evidence first: ForgeFlow runs the repository's tests itself (spec section 196).
        executed = await checks.run("test") if "test" in available else []
        runs: list[AgentRunRecord] = []
        channel = self._channel(wf, task, spec, checkout.path, runs)
        ctx = AgentRuntimeContext(
            workflow_id=wf.workflow_id,
            task_id=task.task_id,
            repository_root=checkout.path,
            checks=checks,
            diff=diff,
            a2a=channel,
        )
        browser_ids = {
            ac.id for ac in spec.acceptance_criteria if ac.verification == "browser_test"
        }
        events = []
        note: str | None = None
        async with contextlib.AsyncExitStack() as stack:
            # A browser is started only when a criterion needs one (spec section 156).
            if browser_ids:
                try:
                    preview = await stack.enter_async_context(
                        PreviewServer(
                            checkout.path,
                            self.preview_ports,
                            preview_host(
                                self.settings.preview_host, self.settings.preview_peer_host
                            ),
                            timeout=self.settings.preview_start_timeout_seconds,
                            check_timeout=self.settings.check_timeout_seconds,
                            exclude_path=sys.prefix,
                        )
                    )
                    ctx.app_url = preview.url
                    events.append(
                        task_event(
                            task, EventType.PREVIEW_STARTED, url=preview.url, mode=preview.mode
                        )
                    )
                except (ValidationFailed, PolicyViolation, OSError) as exc:
                    note = str(exc)
            started = utcnow()
            outcome, manifest = await self._capable(
                wf,
                task,
                "qa",
                ctx,
                spec,
                lambda: self.gateway.verify_acceptance(
                    ctx,
                    QARequest(
                        spec,
                        executed,
                        available,
                        round=task.round,
                        app_url=ctx.app_url,
                        browser_note=note,
                    ),
                ),
                allowed_origins=[ctx.app_url] if ctx.app_url else [],
            )
        runs.insert(0, run_from_outcome(task, "qa", outcome, started))
        assessment = outcome.output
        browser_calls = [c for c in ctx.mcp_calls if str(c.get("tool", "")).startswith("browser_")]
        browser_ok = any(c.get("status") == "ok" for c in browser_calls)
        screenshots = [a for c in browser_calls for a in c.get("artifacts", [])]
        if browser_ids and not browser_calls and note is None:
            note = (
                "no Playwright MCP tools were available to the QA agent "
                "(enable the Playwright preset for this project under Extensibility)"
            )

        test_runs = [c for c in checks.runs if c.kind == "test"]
        tests_passed = bool(test_runs) and all(c.passed for c in test_runs)
        tests_failed = any(not c.passed for c in test_runs)
        by_id = {c.id: c for c in assessment.criteria}
        criteria: list[CriterionResult] = []
        downgraded: list[str] = []
        for ac in spec.acceptance_criteria:
            result_ac = by_id.get(ac.id) or CriterionResult(
                id=ac.id, status="UNCERTAIN", evidence="not evaluated by the QA agent"
            )
            if (
                result_ac.status == "PASS"
                and ac.verification in TEST_VERIFICATIONS
                and not tests_passed
            ):
                result_ac = result_ac.model_copy(
                    update={
                        "status": "UNCERTAIN",
                        "evidence": f"{result_ac.evidence} [downgraded: no executed passing test]",
                    }
                )
                downgraded.append(ac.id)
            if ac.id in browser_ids:
                if result_ac.status == "PASS" and not browser_ok:
                    result_ac = result_ac.model_copy(
                        update={
                            "status": "UNCERTAIN",
                            "evidence": f"{result_ac.evidence} [downgraded: no browser evidence]",
                        }
                    )
                    downgraded.append(ac.id)
                result_ac = result_ac.model_copy(update={"artifacts": screenshots})
            criteria.append(result_ac)

        failed = [c for c in criteria if c.status == "FAIL"]
        blocking = bool(failed) or tests_failed
        reasons = [f"qa {c.id} FAIL: {c.evidence[:200]}" for c in failed]
        if tests_failed:
            reasons += [
                f"qa test command failed: {c.command} (exit {c.exit_code})"
                for c in test_runs
                if not c.passed
            ]
        uncertain = any(c.status == "UNCERTAIN" for c in criteria)
        report = QAReport(
            summary=assessment.summary,
            criteria=criteria,
            gaps=assessment.gaps,
            downgraded=downgraded,
            browser_url=ctx.app_url,
            browser_note=note,
            browser_actions=len(browser_calls),
            artifacts=screenshots,
        )
        result = TaskResult(
            capability_manifest=manifest,
            summary=assessment.summary,
            verdict="fail" if blocking else ("uncertain" if uncertain else "pass"),
            blocking=blocking,
            blocking_reasons=reasons,
            qa=report,
            checks=checks.runs,
            files_changed=files,
            risks=[
                f"{c.id} uncertain: {c.evidence[:160]}" for c in criteria if c.status == "UNCERTAIN"
            ]
            + ([f"browser verification unavailable: {note}"] if browser_ids and note else []),
        )
        output = self._output(task, result, runs, channel, checks.runs)
        output.events = events + output.events
        return output

    # ---------------------------------------------------------------------- ci

    async def _ci(
        self, wf: Workflow, task: Task, spec: RequirementSpecification, checkout: PreparedWorkspace
    ) -> HandlerOutput:
        assert wf.repository_path is not None
        target = (wf.execution.target_commit if wf.execution else None) or ""
        try:
            pipeline_spec = build_pipeline_spec(checkout.path, wf.repository_path, target)
        except ValidationFailed as exc:
            result = TaskResult(
                summary=f"CI not run: {exc}",
                verdict="uncertain",
                risks=[str(exc)],
            )
            return self._output(task, result, [], None)
        pipeline = render_jenkinsfile(pipeline_spec, self.settings.jenkins_repos_root)
        build = await self.ci.run(pipeline_spec.job_name, pipeline, target)
        if build.status == "TIMEOUT":
            raise CIUnavailable(f"CI build {build.build_number} did not finish in time")
        runs: list[AgentRunRecord] = []
        analysis = None
        if build.status != "SUCCESS":
            _, files = await self._change(wf)
            ctx = AgentRuntimeContext(
                workflow_id=wf.workflow_id,
                task_id=task.task_id,
                repository_root=checkout.path,
                knowledge=self.knowledge,
                repository_id=self._repository_id(wf),
            )
            history = await self._build_history(wf, build.log_tail)
            started = utcnow()
            outcome = await self.gateway.analyze_ci_failure(
                ctx,
                CIRequest(
                    build,
                    pipeline,
                    change_summary=f"{spec.summary}; files: {', '.join(files[:30])}",
                    history=history,
                ),
            )
            runs.append(run_from_outcome(task, "ci", outcome, started))
            analysis = outcome.output
        blocking = build.status != "SUCCESS"
        reasons = []
        if blocking:
            reasons.append(
                f"ci build #{build.build_number} {build.status}"
                + (f" in {analysis.failing_stage}: {analysis.summary}" if analysis else "")
            )
        build_event = task_event(
            task,
            EventType.CI_BUILD_COMPLETED,
            provider=build.provider,
            job=build.job,
            build_number=build.build_number,
            result=build.status,
            build_duration_ms=build.duration_ms,
        )
        result = TaskResult(
            summary=f"{build.provider} build #{build.build_number}: {build.status}",
            verdict="fail" if blocking else "pass",
            blocking=blocking,
            blocking_reasons=reasons,
            ci=CIReport(build=build, pipeline=pipeline, analysis=analysis),
        )
        output = self._output(task, result, runs, None)
        output.events.insert(0, build_event)
        return output

    async def _build_history(self, wf: Workflow, log_tail: str) -> str:
        """Earlier builds/failures of this project resembling the first error lines."""
        if self.knowledge is None or not self.knowledge.available():
            return ""
        from forgeflow.knowledge.service import format_hits

        lines = [
            line
            for line in log_tail.splitlines()
            if any(w in line.lower() for w in ("error", "fail", "exception", "traceback"))
        ]
        query = "\n".join(lines[:5]) or log_tail[-500:]
        hits = await self.knowledge.similar_failures(
            query, self._repository_id(wf), exclude_workflow=wf.workflow_id
        )
        return format_hits(hits, max_chars=400) if hits else ""
