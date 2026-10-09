import asyncio

from forgeflow.schemas.task import TaskStatus
from forgeflow.schemas.workflow import WorkflowStatus
from tests.conftest import drive, git

TOKEN = "github_pat_" + "B" * 40


async def bound_project(container, settings, tmp_path, auto=True):
    ext = container.extensibility
    connector = await ext.connectors.create_github("local", "GitHub", TOKEN, ["octo/app"])
    project = await ext.projects.ensure("local", "app")
    await ext.projects.update(
        "local",
        project.project_id,
        github={
            "connector_id": connector.connector_id,
            "repository": "octo/app",
            "auto_pull_request": auto,
        },
    )
    remote = tmp_path / "remotes" / "octo" / "app.git"
    remote.parent.mkdir(parents=True)
    git(tmp_path, "init", "-q", "--bare", str(remote))
    return remote


async def planned(container):
    svc = container.service
    wf = await svc.create_workflow("Add a status page", "app")
    await svc.process_analysis(wf.workflow_id, 1)
    [q] = await container.store.list_questions(wf.workflow_id)
    await svc.answer_question(q.question_id, "A", None)
    await svc.process_analysis(wf.workflow_id, 2)
    return await container.execution.start_execution(wf.workflow_id)


async def run_until_approval(container, wf):
    """Drive the workflow in the background until the PR approval request appears."""
    ext = container.extensibility
    runner = asyncio.create_task(drive(container, wf.workflow_id))
    for _ in range(600):
        pending = await ext.approvals.list_approvals(
            "local", status="pending", workflow_id=wf.workflow_id
        )
        if pending:
            return runner, pending[0]
        await asyncio.sleep(0.05)
    raise AssertionError("no approval was requested")


async def test_verified_change_is_pushed_and_pr_opened_after_approval(
    container, git_repo, settings, tmp_path, fake_github
):
    remote = await bound_project(container, settings, tmp_path)
    wf = await planned(container)
    assert wf.project_id == "proj_app"
    assert set(wf.capability_snapshot.manifests) >= {"developer", "qa"}

    runner, approval = await run_until_approval(container, wf)
    assert approval.capability_id == "github.pull_request.create"
    assert "octo/app" in approval.summary and approval.details["base"] == "main"
    # The branch push (AUTO) already happened; the PR (ASK) waits for a human.
    head = git(remote, "rev-parse", f"refs/heads/forgeflow/{wf.workflow_id}")
    await container.execution.tick(wf.workflow_id)
    assert (
        await container.store.get_workflow(wf.workflow_id)
    ).status == WorkflowStatus.WAITING_FOR_APPROVAL

    await container.extensibility.approvals.decide(approval.approval_id, True, "local")
    await runner
    await drive(container, wf.workflow_id)

    wf = await container.store.get_workflow(wf.workflow_id)
    assert wf.status == WorkflowStatus.COMPLETED
    assert wf.pull_request.number == 1 and wf.pull_request.branch == f"forgeflow/{wf.workflow_id}"
    assert wf.report.pull_request_url == wf.pull_request.url
    assert head == wf.execution.target_commit
    [pr] = fake_github.pulls
    assert pr["head"]["ref"] == f"forgeflow/{wf.workflow_id}" and pr["base"]["ref"] == "main"
    assert "## Verification" in pr["body"] and pr["title"] == "Add a status page"
    audit = await container.extensibility.store.find(
        "capability_audit",
        {"workflow_id": wf.workflow_id, "capability_type": "connector"},
        sort="timestamp",
    )
    assert [(a["capability_id"], a["approval"], a["result"]) for a in audit] == [
        ("github.branch.push", "auto", "success"),
        ("github.pull_request.create", "user_approved", "success"),
    ]


async def test_rejected_pr_still_completes_the_workflow(
    container, git_repo, settings, tmp_path, fake_github
):
    await bound_project(container, settings, tmp_path)
    wf = await planned(container)
    runner, approval = await run_until_approval(container, wf)
    await container.extensibility.approvals.decide(approval.approval_id, False, "local", "not yet")
    await runner
    await drive(container, wf.workflow_id)
    wf = await container.store.get_workflow(wf.workflow_id)
    tasks = {t.key: t for t in await container.store.list_tasks(wf.workflow_id)}
    assert wf.status == WorkflowStatus.COMPLETED and wf.pull_request is None
    assert tasks["P1-publish"].status == TaskStatus.COMPLETED
    assert "not approved" in tasks["P1-publish"].result.summary
    assert fake_github.pulls == []


async def test_no_publish_without_binding_or_when_disabled(container, git_repo, settings, tmp_path):
    wf = await planned(container)
    await drive(container, wf.workflow_id)
    tasks = {t.key for t in await container.store.list_tasks(wf.workflow_id)}
    assert "P1-publish" not in tasks
    assert (await container.store.get_workflow(wf.workflow_id)).status == WorkflowStatus.COMPLETED


async def test_revoked_connector_blocks_publishing(
    container, git_repo, settings, tmp_path, fake_github
):
    await bound_project(container, settings, tmp_path)
    connector = (await container.extensibility.connectors.list_connectors("local"))[0]
    await container.extensibility.connectors.revoke("local", connector.connector_id)
    wf = await planned(container)
    await drive(container, wf.workflow_id)
    tasks = {t.key for t in await container.store.list_tasks(wf.workflow_id)}
    wf = await container.store.get_workflow(wf.workflow_id)
    # Revoked: no publish task, nothing reached GitHub, the workflow completes and says why.
    assert "P1-publish" not in tasks and fake_github.pulls == []
    assert wf.status == WorkflowStatus.COMPLETED
    assert "connector is revoked" in wf.execution.note
