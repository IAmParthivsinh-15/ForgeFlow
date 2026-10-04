import pytest

from forgeflow.core.errors import ValidationFailed
from forgeflow.platform.orchestration.fake_gateway import FakeAgentGateway
from forgeflow.platform.orchestration.gateway import AgentOutcome
from forgeflow.schemas.events import EventType
from forgeflow.schemas.task import DevelopmentPlan, SubtaskSpec, TaskStatus
from forgeflow.schemas.verification import CriterionResult, QAAssessment, ScannerFinding, ScanResult
from forgeflow.schemas.workflow import WorkflowStatus
from forgeflow.tools.security.scanners import ScannerSuite
from tests.conftest import drive, git


async def run_workflow(container, request="Add a status page", repo="app"):
    svc = container.service
    wf = await svc.create_workflow(request, repo)
    wf = await svc.process_analysis(wf.workflow_id, 1)
    if wf.status == WorkflowStatus.AWAITING_CLARIFICATION:
        [q] = await container.store.list_questions(wf.workflow_id)
        await svc.answer_question(q.question_id, "A", None)
        wf = await svc.process_analysis(wf.workflow_id, 2)
    await container.execution.start_execution(wf.workflow_id)
    await drive(container, wf.workflow_id)
    wf = await container.store.get_workflow(wf.workflow_id)
    tasks = {t.key: t for t in await container.store.list_tasks(wf.workflow_id)}
    return wf, tasks


async def events_of(container, wf):
    return [se.event.event_type for se in await container.store.list_events(wf.workflow_id)]


# --------------------------------------------------------------- happy path


async def test_clean_run_completes_with_report_and_pr_text(container, git_repo):
    wf, tasks = await run_workflow(container)
    assert wf.status == WorkflowStatus.COMPLETED
    assert {k for k in tasks if k.startswith("V")} == {"V1-code_review", "V1-qa", "V1-ci"}
    assert tasks["V1-qa"].result.verdict == "pass"
    assert [c.status for c in tasks["V1-qa"].result.qa.criteria] == ["PASS"]
    assert tasks["V1-qa"].result.checks[0].passed  # ForgeFlow ran the repo's tests itself
    report = wf.report
    assert report.outcome == "passed"
    assert [s.stage for s in report.stages] == ["Development", "Code review", "QA", "CI"]
    assert report.pr_title == "Add a status page"
    assert "| AC-001 | PASS |" in report.pr_body and report.ci_build_url in report.pr_body
    # Status followed the stages: EXECUTING -> INTEGRATING -> REVIEWING -> TESTING -> CI.
    statuses = [
        se.event.payload.get("current")
        for se in await container.store.list_events(wf.workflow_id)
        if se.event.event_type == EventType.WORKFLOW_STATUS_CHANGED
    ]
    order = [
        s
        for s in statuses
        if s in ("EXECUTING", "INTEGRATING", "REVIEWING", "TESTING", "CI", "COMPLETED")
    ]
    assert order == ["EXECUTING", "INTEGRATING", "REVIEWING", "TESTING", "CI", "COMPLETED"]


async def test_qa_asks_the_developer_over_a2a(container, git_repo):
    wf, tasks = await run_workflow(container)
    [message] = await container.store.list_a2a_messages(wf.workflow_id)
    assert (message.sender, message.receiver, message.status) == ("qa", "developer", "answered")
    assert message.task_id == tasks["V1-qa"].task_id
    assert tasks["V1-qa"].result.a2a_messages == 1
    assert EventType.A2A_EXCHANGE in await events_of(container, wf)


# ------------------------------------------------------------- repair loop


async def test_review_changes_trigger_a_repair_round(container, git_repo):
    wf, tasks = await run_workflow(container, "Add a status page demo-repair")
    assert wf.status == WorkflowStatus.COMPLETED
    assert tasks["V1-code_review"].result.blocking
    assert tasks["F1-repair"].kind == "repair" and tasks["F1-repair"].status == TaskStatus.COMPLETED
    assert "Demo finding" in tasks["F1-repair"].instructions
    # Round 2 re-runs review (the failed stage) and everything after it.
    assert {k for k in tasks if k.startswith("V2")} == {"V2-code_review", "V2-qa", "V2-ci"}
    assert not tasks["V2-code_review"].result.blocking
    # The repair commit was fast-forwarded onto the integration branch and re-verified.
    repair_commit = tasks["F1-repair"].result.commit
    assert wf.execution.integration_commit == repair_commit == wf.execution.target_commit
    branch_head = git(git_repo, "rev-parse", wf.execution.integration_branch)
    assert branch_head == repair_commit
    assert tasks["V2-ci"].result.ci.build.commit == repair_commit
    assert wf.report.repair_rounds == 1 and wf.report.outcome == "passed"
    assert {st.capability: st.status for st in wf.route_plan.stages}["development"] == "completed"
    assert EventType.REPAIR_REQUESTED in await events_of(container, wf)


async def test_ci_failure_reruns_only_ci_and_review(container, git_repo, fake_ci):
    fake_ci.results = ["FAILURE", "SUCCESS"]
    wf, tasks = await run_workflow(container)
    assert tasks["V1-ci"].result.blocking
    assert tasks["V1-ci"].result.ci.analysis.failing_stage == "Test"
    assert "AssertionError" in tasks["F1-repair"].instructions  # CI log evidence reached the fix
    assert {k for k in tasks if k.startswith("V2")} == {"V2-code_review", "V2-ci"}
    assert wf.status == WorkflowStatus.COMPLETED and wf.report.outcome == "passed"


async def test_repair_limit_waits_for_a_human_then_accept(container, git_repo, fake_ci, settings):
    settings.max_repair_attempts = 1
    fake_ci.results = ["FAILURE"] * 5
    wf, tasks = await run_workflow(container)
    assert wf.status == WorkflowStatus.PAUSED and wf.execution.awaiting_decision
    assert wf.execution.repair_attempts == 1 and "F1-repair" in tasks and "F2-repair" not in tasks
    assert "Blocking findings remain" in wf.error
    with pytest.raises(ValidationFailed):
        await container.execution.retry_task(tasks["V2-ci"].task_id)

    wf = await container.execution.resolve_decision(wf.workflow_id, "accept")
    assert wf.status == WorkflowStatus.COMPLETED
    assert wf.report.outcome == "passed_with_accepted_risks"
    assert wf.report.accepted_risks and "FAILURE" in wf.report.accepted_risks[0]


async def test_repair_limit_then_one_more_repair(container, git_repo, fake_ci, settings):
    settings.max_repair_attempts = 1
    fake_ci.results = ["FAILURE", "FAILURE", "SUCCESS"]
    wf, _ = await run_workflow(container)
    assert wf.execution.awaiting_decision
    await container.execution.resolve_decision(wf.workflow_id, "repair")
    await drive(container, wf.workflow_id)
    wf = await container.store.get_workflow(wf.workflow_id)
    tasks = {t.key for t in await container.store.list_tasks(wf.workflow_id)}
    assert "F2-repair" in tasks and "V3-ci" in tasks
    assert wf.status == WorkflowStatus.COMPLETED and wf.report.repair_rounds == 2
    with pytest.raises(ValidationFailed):
        await container.execution.resolve_decision(wf.workflow_id, "accept")


# ---------------------------------------------------------------- security


async def test_security_blocks_on_scanner_finding_in_changed_code(container, git_repo):
    calls = {"n": 0}

    async def scanner(root, limit):
        calls["n"] += 1
        findings = []
        if calls["n"] == 1:  # first round only: the repair "fixes" it
            findings = [
                ScannerFinding(
                    tool="semgrep",
                    rule_id="python-eval-exec",
                    severity="high",
                    category="A03",
                    file="forgeflow-demo/backend/T2-backend.md",
                    line=1,
                    message="eval on dynamic input",
                )
            ]
        return ScanResult(tool="semgrep", status="completed", findings=findings)

    container.scanners = ScannerSuite([scanner])
    wf, tasks = await run_workflow(container, "Add a forgot-password flow")
    sec = tasks["V1-security"].result
    assert sec.blocking and sec.security.owasp_edition == "2021"
    assert any(f.source == "semgrep" and f.category == "A03" for f in sec.security.findings)
    assert len(sec.security.categories) == 10
    assert "F1-repair" in tasks
    # Security failed, so it and everything after it re-run (plus review).
    assert {k for k in tasks if k.startswith("V2")} == {
        "V2-code_review",
        "V2-security",
        "V2-qa",
        "V2-ci",
    }
    assert wf.status == WorkflowStatus.COMPLETED


async def test_findings_outside_the_change_do_not_block(container, git_repo):
    async def scanner(root, limit):
        return ScanResult(
            tool="gitleaks",
            status="completed",
            findings=[
                ScannerFinding(
                    tool="gitleaks",
                    rule_id="generic-api-key",
                    severity="high",
                    category="A07",
                    file="legacy/old_config.py",
                    line=3,
                    message="secret in untouched file",
                )
            ],
        )

    container.scanners = ScannerSuite([scanner])
    wf, tasks = await run_workflow(container, "Add a forgot-password flow")
    assert not tasks["V1-security"].result.blocking
    assert tasks["V1-security"].result.security.findings  # still reported
    assert wf.report.outcome == "passed"


async def test_change_analyzer_adds_security_when_auth_code_changes(container, git_repo):
    class AuthPlan(FakeAgentGateway):
        async def plan_development(self, ctx, request):
            plan = DevelopmentPlan(
                summary="auth",
                subtasks=[
                    SubtaskSpec(
                        key="auth",
                        title="Session handling",
                        instructions="x",
                        file_scope=["src/auth/**"],
                    )
                ],
            )
            return AgentOutcome(plan, "test", [])

    container.gateway = AuthPlan()
    container.service.gateway = container.gateway
    wf, tasks = await run_workflow(container, "Add a status page")  # no security terms
    assert "V1-security" in tasks
    stage = next(s for s in wf.route_plan.stages if s.capability == "security")
    assert "change analyzer" in stage.reason and "auth" in stage.reason


# ---------------------------------------------------------------------- QA


async def test_unsupported_pass_claims_are_downgraded(container, git_repo):
    (git_repo / "forgeflow.yaml").write_text("commands: {}\n")  # no test command at all
    git(git_repo, "commit", "-qam", "no tests")

    class OptimisticQA(FakeAgentGateway):
        async def verify_acceptance(self, ctx, request):
            ids = [ac.id for ac in request.specification.acceptance_criteria]
            return AgentOutcome(
                QAAssessment(
                    summary="all good",
                    criteria=[
                        CriterionResult(id=i, status="PASS", evidence="trust me") for i in ids
                    ],
                ),
                "test",
                [],
            )

    container.gateway = OptimisticQA()
    wf, tasks = await run_workflow(container)
    qa = tasks["V1-qa"].result
    assert qa.qa.downgraded == ["AC-001"]
    assert qa.qa.criteria[0].status == "UNCERTAIN" and qa.verdict == "uncertain"
    assert not qa.blocking
    assert tasks["V1-ci"].result.verdict == "uncertain"  # no CI commands either


async def test_failing_repo_tests_block_qa(container, git_repo, settings):
    settings.max_repair_attempts = 0
    (git_repo / "forgeflow.yaml").write_text(
        'commands:\n  test: python -c "import sys; sys.exit(1)"\n'
    )
    git(git_repo, "commit", "-qam", "failing tests")
    wf, tasks = await run_workflow(container)
    qa = tasks["V1-qa"].result
    assert qa.blocking and qa.qa.criteria[0].status == "FAIL"
    assert any("test command failed" in r for r in qa.blocking_reasons)
    assert wf.status == WorkflowStatus.PAUSED and wf.execution.awaiting_decision


# ------------------------------------------------------- verification-only


async def test_review_only_request_needs_no_developer(container, git_repo):
    wf, tasks = await run_workflow(container, "Review PR #142 for correctness and security")
    assert set(tasks) == {"V1-code_review", "V1-security"}
    assert wf.status == WorkflowStatus.COMPLETED
    assert wf.execution.integration_branch is None
    assert [s.stage for s in wf.report.stages] == ["Code review", "Security"]


async def test_audit_findings_are_reported_not_repaired(container, git_repo):
    async def scanner(root, limit):
        return ScanResult(
            tool="bandit",
            status="completed",
            findings=[
                ScannerFinding(
                    tool="bandit",
                    rule_id="B602",
                    severity="high",
                    category="A03",
                    file="src/app.py",
                    line=1,
                    message="shell=True",
                )
            ],
        )

    container.scanners = ScannerSuite([scanner])
    wf, tasks = await run_workflow(container, "Check this application against OWASP Top 10")
    assert set(tasks) == {"V1-security"} and tasks["V1-security"].result.blocking
    assert wf.status == WorkflowStatus.COMPLETED and wf.report.outcome == "failed"
    assert wf.report.open_findings


# -------------------------------------------------------------- infrastructure


async def test_jenkins_down_pauses_then_retry_recovers(container, git_repo, fake_ci, settings):
    fake_ci.unavailable = settings.task_max_attempts  # every automatic attempt fails
    wf, tasks = await run_workflow(container)
    assert wf.status == WorkflowStatus.PAUSED
    assert tasks["V1-ci"].status == TaskStatus.FAILED and "Jenkins" in tasks["V1-ci"].error
    await container.execution.retry_task(tasks["V1-ci"].task_id)
    await drive(container, wf.workflow_id)
    wf = await container.store.get_workflow(wf.workflow_id)
    assert wf.status == WorkflowStatus.COMPLETED


async def test_resolve_requires_a_pending_decision(container, git_repo):
    wf, _ = await run_workflow(container)
    with pytest.raises(ValidationFailed):
        await container.execution.resolve_decision(wf.workflow_id, "repair")
