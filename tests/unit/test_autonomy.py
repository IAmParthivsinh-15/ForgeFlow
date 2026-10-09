"""L4 autonomy (additional.md): triggers, plan-before-spawn, guardrails, independent review,
closure against GitHub, escalations, emergency stop and resume.

These run the real commander, guards, executor and GitHub client against the GitHub
fake, a local bare git remote and the CI fake - in one process, without Kafka.
"""

import asyncio
import hashlib
import hmac
import json
from pathlib import Path

import httpx
import pytest
import yaml

from forgeflow.autonomy.intake import handle_webhook, intake, source_item
from forgeflow.autonomy.learning import decide_candidate
from forgeflow.autonomy.planner import build_plan, role_models, validate_plan
from forgeflow.core.errors import ValidationFailed
from forgeflow.extensibility.catalog import github_capabilities
from forgeflow.extensibility.gateway import CapabilityDenied, Invocation
from forgeflow.integrations.github.client import issue_from_api, marker
from forgeflow.schemas.autonomy import RunStatus
from forgeflow.schemas.task import TaskStatus
from tests.conftest import drive, git

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "github_pat_" + "S" * 40
SECRET = "webhook-secret-for-tests"


def write_contract(tmp_path: Path, connector_id: str, **changes) -> Path:
    data = yaml.safe_load(
        (ROOT / "config" / "autonomy" / "contract.yaml").read_text(encoding="utf-8")
    )
    data["mode"] = changes.pop("mode", "autonomous")
    data["source"].update(
        repository="octo/app", project_repository_path="app", connector_id=connector_id
    )
    for dotted, value in changes.items():
        target = data
        *path, last = dotted.split("__")
        for key in path:
            target = target[key]
        target[last] = value
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.fixture
async def l4(container, settings, fake_github, git_repo, tmp_path):
    ext = container.extensibility
    connector = await ext.connectors.create_github("local", "ForgeFlow bot", TOKEN, ["octo/app"])
    remote = tmp_path / "remotes" / "octo" / "app.git"
    remote.parent.mkdir(parents=True)
    git(tmp_path, "init", "-q", "--bare", str(remote))
    fake_github.sha_for = lambda branch: git(remote, "rev-parse", f"refs/heads/{branch}")
    settings.autonomy_contract_path = write_contract(tmp_path, connector.connector_id)
    settings.github_webhook_secret = SECRET
    container.connector_id = connector.connector_id
    return container


async def run_to_end(container, run, limit: int = 60):
    auto = container.autonomy
    for _ in range(limit):
        run = await auto.commander.step(run)
        if run.status in (
            RunStatus.RESOLVED,
            RunStatus.CLOSED_NO_ACTION,
            RunStatus.ESCALATED,
            RunStatus.STOPPED,
            RunStatus.PAUSED_BY_GUARDRAIL,
        ):
            return run
        if run.workflow_id:
            await drive(container, run.workflow_id)
    raise AssertionError(f"run did not finish: {run.status}")


async def new_run(
    container,
    fake_github,
    number=12,
    title="Greeting has a bug",
    body="Fix the bug: the greeting is missing its exclamation mark.",
    labels=("forgeflow", "bug"),
):
    fake_github.add_issue(number, title, body, labels)
    contract = container.autonomy.commander.contract()
    issue = issue_from_api("octo/app", fake_github.issues[number])
    run, _ = await intake(container.autonomy.runs, contract, source_item(issue), "manual")
    return run


async def test_issue_is_resolved_autonomously_and_closure_verified_on_github(l4, fake_github):
    run = await new_run(l4, fake_github)
    run = await run_to_end(l4, run)
    assert run.status == RunStatus.RESOLVED and run.decision == "RESOLVE", run.escalation
    auto = l4.autonomy
    events = await auto.runs.events(run.trace_id)
    types = [e.type for e in events]
    # Plan durably saved before any worker started.
    saved = next(e for e in events if e.type == "plan.saved")
    tasks = await l4.store.list_tasks(run.workflow_id)
    first_start = min(t.started_at for t in tasks if t.started_at)
    assert saved.timestamp <= first_start
    assert run.plan.status == "VALIDATED_AND_SAVED"
    assert run.plan.implementer_model == "fake/implementer-1"
    assert run.plan.reviewer_model == "fake/reviewer-1"
    # Two genuinely parallel workers (backend + frontend worktrees), within the cap.
    assert 2 <= run.counters.max_parallel_observed <= run.limits.max_workers
    # Closure: every check passed, each with evidence.
    assert {c.name for c in run.closure} == set(run.plan.closure_checks)
    assert all(c.passed for c in run.closure), [c for c in run.closure if not c.passed]
    review = next(c for c in run.closure if c.name == "independent_review_passes")
    assert review.evidence["reviewer_models"] == ["fake/reviewer-1"]
    assert "fake/reviewer-1" not in review.evidence["implementer_models"]
    # Bounded actions under the service identity: a draft PR, one resolution comment.
    [pr] = fake_github.pulls
    assert pr["draft"] is True and pr["head"]["ref"].startswith("forgeflow/")
    [comment] = fake_github.comments[12]
    assert marker(run.trace_id, "resolution") in comment and pr["html_url"] in comment
    # No human was needed: no approvals, no interventions.
    assert await l4.extensibility.store.find("approvals", {"workflow_id": run.workflow_id}) == []
    [learning] = await l4.extensibility.store.find("learning_records", {"trace_id": run.trace_id})
    assert learning["human_interventions"] == []
    for expected in (
        "run.created",
        "source.reverified",
        "prepare.completed",
        "plan.saved",
        "execution.started",
        "closure.verified",
        "decision.resolve",
        "learning.recorded",
    ):
        assert expected in types


async def test_webhook_sweep_and_manual_triggers_share_one_idempotent_run(l4, fake_github):
    fake_github.add_issue(20, "Bug in greeting", "Fix the bug in the greeting")
    auto = l4.autonomy
    contract = auto.commander.contract()
    payload = {
        "action": "opened",
        "repository": {"full_name": "octo/app"},
        "issue": fake_github.issues[20],
    }
    first = await handle_webhook(auto.runs, contract, "issues", payload)
    again = await handle_webhook(auto.runs, contract, "issues", payload)  # GitHub redelivery
    client = await auto.commander.github(contract)
    from forgeflow.autonomy.intake import sweep

    swept = await sweep(auto.runs, contract, client)
    assert first["status"] == "created" and again == {
        "status": "duplicate",
        "trace_id": first["trace_id"],
    }
    assert swept["duplicates"] == 1 and swept["created"] == 0
    runs = await auto.runs.list_runs()
    assert [r.trace_id for r in runs] == [first["trace_id"]]
    duplicate_events = [
        e for e in await auto.runs.events(first["trace_id"]) if e.type == "trigger.duplicate"
    ]
    assert len(duplicate_events) == 2


async def test_ineligible_issue_is_closed_with_evidence(l4, fake_github):
    run = await new_run(l4, fake_github, labels=("forgeflow", "bug", "wontfix"))
    run = await run_to_end(l4, run)
    assert run.status == RunStatus.CLOSED_NO_ACTION and "wontfix" in run.decision_reason
    events = [e.type for e in await l4.autonomy.runs.events(run.trace_id)]
    assert events.index("source.reverified") < events.index("decision.close_no_action")
    assert run.workflow_id is None  # nothing was started


async def test_high_risk_issue_escalates_before_any_worker(l4, fake_github):
    run = await new_run(
        l4, fake_github, title="Login bug", body="Fix the bug: the password reset token leaks"
    )
    run = await run_to_end(l4, run)
    assert run.status == RunStatus.ESCALATED and run.escalation.condition == "out_of_scope"
    assert run.escalation.decision_needed and run.escalation.evidence["trace_id"] == run.trace_id
    assert await l4.store.list_tasks(run.workflow_id) == []
    assert (await l4.store.get_workflow(run.workflow_id)).status.value == "CANCELLED"
    # Escalations are delivered: alert + a comment on the issue.
    assert any(a["kind"] == "escalation" for a in await l4.autonomy.alerts.list_alerts())
    assert marker(run.trace_id, "escalation") in fake_github.comments[12][0]


async def test_no_saved_plan_means_no_worker(l4, fake_github):
    run = await new_run(l4, fake_github)
    run = await l4.autonomy.commander.step(run)  # RECEIVED -> PLANNING (analysis ran)
    wf = await l4.store.get_workflow(run.workflow_id)
    assert wf.autonomous and wf.status.value == "PLANNED"
    assert await l4.execution.start_execution(wf.workflow_id) is None  # blocked by the guard
    assert await l4.autonomy.guard.spawn_guard(wf, 0) == (0, "no validated, saved plan")


async def test_plan_validation_is_enforced_in_code(l4, fake_github):
    run = await new_run(l4, fake_github)
    contract = l4.autonomy.commander.contract()
    models = role_models(True, None)
    plan = build_plan(run, "wf_x", None, contract, models, 3)
    assert validate_plan(plan, run, contract) == ([], [])
    same = {**models, "reviewer": models["implementer"]}
    errors, _ = validate_plan(build_plan(run, "wf_x", None, contract, same, 3), run, contract)
    assert "the reviewer model must differ from the implementer model" in errors
    rogue = models | {
        "implementer": models["implementer"].model_copy(update={"model": "gpt-unlisted"})
    }
    errors, _ = validate_plan(build_plan(run, "wf_x", None, contract, rogue, 3), run, contract)
    assert any("allow-list" in e for e in errors)
    big = build_plan(run, "wf_x", None, contract, models, 3)
    big.worker_count_max = 10
    errors, _ = validate_plan(big, run, contract)
    assert any("exceeds the cap" in e for e in errors)
    # Authority: a plan cannot grant itself an ASK action.
    asking = build_plan(run, "wf_x", None, contract, models, 3)
    asking.planned_actions.append("github.pull_request.merge")
    _, authority = validate_plan(asking, run, contract)
    assert authority == ["github.pull_request.merge is ASK in repo_low_risk_changes_v1"]


async def test_profile_without_draft_pr_authority_escalates(l4, fake_github, settings, tmp_path):
    settings.autonomy_contract_path = write_contract(
        tmp_path,
        l4.connector_id,
        action_profile__auto=[
            "github.repository.read",
            "github.issue.read",
            "workspace.write",
            "checks.run",
            "github.branch.push",
            "github.issue.comment",
        ],
    )
    run = await run_to_end(l4, await new_run(l4, fake_github))
    assert (
        run.status == RunStatus.ESCALATED
        and run.escalation.condition == "action_requires_authority"
    )
    assert "github.pull_request.create_draft is ASK" in run.escalation.summary
    assert await l4.store.list_tasks(run.workflow_id) == []


async def test_action_profile_tiers_are_enforced_by_the_gateway(l4):
    ext = l4.extensibility
    profile = l4.autonomy.commander.contract().action_profile
    inv = Invocation(
        owner_id="local", workflow_id=None, task_id=None, agent="commander", action_profile=profile
    )
    caps = {c.capability_id: c for c in github_capabilities(l4.connector_id, "local")}
    deny_cap = caps["github.repository.read"].model_copy(
        update={"capability_id": "github.repository.delete"}
    )
    with pytest.raises(CapabilityDenied, match="denied by action profile"):
        await ext.gateway.invoke(inv, deny_cap, lambda: asyncio.sleep(0), summary="delete repo")
    # AUTO in the profile runs without an approval even though the catalog default asks.
    assert (
        await ext.gateway.invoke(
            inv, caps["github.issue.comment"], lambda: asyncio.sleep(0, "ok"), summary="comment"
        )
        == "ok"
    )


async def test_observe_mode_records_the_decision_without_external_actions(
    l4, fake_github, settings, tmp_path
):
    settings.autonomy_contract_path = write_contract(tmp_path, l4.connector_id, mode="observe")
    run = await run_to_end(l4, await new_run(l4, fake_github))
    assert (
        run.status == RunStatus.ESCALATED
        and run.escalation.condition == "action_requires_authority"
    )
    assert run.escalation.evidence["would_resolve"] is True
    assert fake_github.pulls == [] and fake_github.comments.get(12) is None
    audit = await l4.extensibility.store.find("capability_audit", {"workflow_id": run.workflow_id})
    assert any(a["result"] == "denied" and "observe mode" in (a["error"] or "") for a in audit)


async def test_emergency_stop_during_active_work_then_resume(l4, fake_github):
    """additional.md section 5 'required stop test', on a real in-process run."""
    gateway = l4.gateway
    original = gateway.implement_subtask
    release = asyncio.Event()
    started = asyncio.Event()

    async def slow(ctx, request):
        if request.task.kind == "implement" and not release.is_set():
            started.set()
            await release.wait()
        return await original(ctx, request)

    gateway.implement_subtask = slow
    auto = l4.autonomy
    run = await new_run(l4, fake_github)
    while run.status != RunStatus.EXECUTING:
        run = await auto.commander.step(run)
    runner = asyncio.create_task(drive(l4, run.workflow_id))
    await asyncio.wait_for(started.wait(), 10)
    running = [
        t for t in await l4.store.list_tasks(run.workflow_id) if t.status == TaskStatus.RUNNING
    ]
    assert running, "work must be active when the stop is requested"

    stopped = await auto.control.stop(run.trace_id, "user:operator", "operator emergency stop")
    again = await auto.control.stop(run.trace_id, "user:operator", "pressed twice")  # idempotent
    assert stopped.status == RunStatus.STOPPED and again.stop.reason == "operator emergency stop"
    assert set(stopped.stop.cancelled_tasks) >= {t.task_id for t in running}
    assert stopped.stop.checkpoint["tasks"] and stopped.stop.acknowledged_at
    release.set()
    await runner
    # New spawns are blocked: ticking dispatches nothing while stopped.
    assert await l4.execution.tick(run.workflow_id) == []
    assert all(
        t.status != TaskStatus.DISPATCHED for t in await l4.store.list_tasks(run.workflow_id)
    )
    # External actions are blocked too.
    caps = {c.capability_id: c for c in github_capabilities(l4.connector_id, "local")}
    inv = Invocation(owner_id="local", workflow_id=run.workflow_id, task_id=None, agent="commander")
    with pytest.raises(CapabilityDenied, match="STOPPED"):
        await l4.extensibility.gateway.invoke(
            inv, caps["github.issue.comment"], lambda: asyncio.sleep(0), summary="should not post"
        )
    types = [e.type for e in await auto.runs.events(run.trace_id)]
    assert (
        types.index("run.stop_requested")
        < types.index("run.stop_acknowledged")
        < types.index("run.stopped")
    )
    assert await auto.commander.step(stopped) == await auto.runs.by_trace(
        run.trace_id
    )  # no silent restart
    assert (await auto.runs.by_trace(run.trace_id)).status == RunStatus.STOPPED

    resumed = await auto.control.resume(run.trace_id, "user:operator")
    assert resumed.status == RunStatus.EXECUTING and resumed.resumes == 1
    events = await auto.runs.events(run.trace_id)
    reverify = [e for e in events if e.type == "source.reverified" and e.actor == "user:operator"]
    assert reverify and reverify[-1].data["eligible"] is True
    final = await run_to_end(l4, resumed)
    assert final.status == RunStatus.RESOLVED, final.escalation
    assert (
        len(fake_github.pulls) == 1 and len(fake_github.comments[12]) == 1
    )  # no duplicate side effects
    [learning] = await l4.extensibility.store.find("learning_records", {"trace_id": run.trace_id})
    kinds = {i["kind"] for i in learning["human_interventions"]}
    assert kinds == {"emergency_stop", "resume"}


async def test_resume_refuses_when_source_no_longer_eligible(l4, fake_github):
    auto = l4.autonomy
    run = await new_run(l4, fake_github)
    while run.status != RunStatus.EXECUTING:
        run = await auto.commander.step(run)
    await auto.control.stop(run.trace_id, "user:operator", "pause please")
    fake_github.issues[12]["state"] = "closed"
    with pytest.raises(ValidationFailed, match="no longer eligible"):
        await auto.control.resume(run.trace_id, "user:operator")
    new = await auto.control.rerun(run.trace_id, "user:operator")
    assert new.trace_id != run.trace_id and new.idempotency_key.endswith(":rerun:1")


async def test_budget_circuit_breaker_pauses_and_blocks_resume(l4, fake_github, settings, tmp_path):
    settings.autonomy_contract_path = write_contract(
        tmp_path, l4.connector_id, limits__budgets__max_runtime_seconds=1
    )
    auto = l4.autonomy
    run = await new_run(l4, fake_github)
    while run.status not in (RunStatus.EXECUTING, RunStatus.PAUSED_BY_GUARDRAIL):
        run = await auto.commander.step(run)
    await asyncio.sleep(1.1)
    run = await auto.commander.step(run)
    assert run.status == RunStatus.PAUSED_BY_GUARDRAIL and run.guardrail.startswith("budget")
    assert run.escalation.condition == "circuit_breaker"
    assert await auto.guard.spawn_guard(await l4.store.get_workflow(run.workflow_id), 0) == (
        0,
        "run is PAUSED_BY_GUARDRAIL",
    )
    kinds = {a["kind"] for a in await auto.alerts.list_alerts()}
    assert {"circuit_breaker", "budget_exhausted", "escalation"} <= kinds
    with pytest.raises(ValidationFailed, match="budgets cannot be raised"):
        await auto.control.resume(run.trace_id, "user:operator")


async def test_learning_candidates_are_governed(l4, fake_github):
    run = await run_to_end(
        l4, await new_run(l4, fake_github, title="Bug", body="Fix the demo-repair bug")
    )
    [learning] = await l4.extensibility.store.find("learning_records", {"trace_id": run.trace_id})
    assert learning["review_corrections"]  # the reviewer's round-1 finding was recorded
    assert learning["candidates"]
    decided = await decide_candidate(
        l4.extensibility.store, learning["learning_id"], 0, "accepted", "user:local", "add it"
    )
    assert (
        decided["candidates"][0]["status"] == "accepted"
        and decided["candidates"][0]["applied"] is False
    )


# --------------------------------------------------------------------------- API


@pytest.fixture
async def api(l4):
    from forgeflow.apps.api.main import create_app

    async def factory():
        return l4

    app = create_app(factory)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


async def test_api_webhook_requires_a_valid_signature_and_exposes_the_run(api, fake_github, l4):
    fake_github.add_issue(31, "Bug: greeting", "Fix the bug")
    body = json.dumps(
        {
            "action": "opened",
            "repository": {"full_name": "octo/app"},
            "issue": fake_github.issues[31],
        }
    ).encode()
    bad = await api.post(
        "/api/v1/autonomy/webhooks/github",
        content=body,
        headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": "sha256=00"},
    )
    assert bad.status_code == 401
    ok = await api.post(
        "/api/v1/autonomy/webhooks/github",
        content=body,
        headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": sign(body)},
    )
    assert ok.status_code == 202 and ok.json()["status"] == "created"
    trace = ok.json()["trace_id"]
    run = await l4.autonomy.runs.by_trace(trace)
    await run_to_end(l4, run)
    plan = (await api.get(f"/api/v1/autonomy/runs/{trace}/plan")).json()
    assert plan["status"] == "VALIDATED_AND_SAVED" and plan["schema_version"] == "1.0"
    portfolio = (await api.get(f"/api/v1/autonomy/runs/{trace}/portfolio")).json()
    assert (
        portfolio["run"]["decision"] == "RESOLVE" and portfolio["audit"] and portfolio["learning"]
    )
    review = (await api.get("/api/v1/autonomy/review?last=5")).json()
    assert review[0]["trace_id"] == trace and review[0]["autonomous_without_intervention"] is True
    status = (await api.get("/api/v1/autonomy/status")).json()
    assert status["source_ready"] and status["webhook_secret_configured"]
    assert TOKEN not in json.dumps(portfolio)
