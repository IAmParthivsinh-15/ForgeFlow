import asyncio
import json

import httpx
import pytest

from forgeflow.core.errors import NotFoundError, ValidationFailed
from forgeflow.extensibility.catalog import github_capabilities
from forgeflow.extensibility.gateway import CapabilityDenied, Invocation
from forgeflow.extensibility.policy import decide
from forgeflow.extensibility.secrets import SecretStore, SecretStoreError, generate_key
from forgeflow.extensibility.store import InMemoryDocumentStore
from forgeflow.integrations.github.client import (
    GitHubClient,
    push_branch,
    repository_from_remote,
)
from forgeflow.schemas.extensibility import Capability
from tests.conftest import git
from tests.fakes import FakeGitHub

TOKEN = "github_pat_" + "A" * 40


def cap(**kw):
    base = dict(capability_id="x.op", type="mcp_tool", name="x.op", status="active")
    base.update(kw)
    return Capability(**base)


# -------------------------------------------------------------------- secrets


async def test_secret_store_encrypts_and_never_stores_plaintext():
    store = InMemoryDocumentStore()
    secrets = SecretStore(store, generate_key())
    await secrets.put("secret://connector/c1", "super-secret-token")
    assert await secrets.get("secret://connector/c1") == "super-secret-token"
    assert "super-secret-token" not in json.dumps(store.data, default=str)
    with pytest.raises(SecretStoreError, match="cannot be decrypted"):
        await SecretStore(store, generate_key()).get("secret://connector/c1")
    with pytest.raises(SecretStoreError, match="FORGEFLOW_SECRET_KEY"):
        await SecretStore(store, "").put("secret://x/y", "v")
    with pytest.raises(SecretStoreError, match="not a valid"):
        SecretStore(store, "not-a-key")


# --------------------------------------------------------------------- policy


@pytest.mark.parametrize(
    "capability,kwargs,expected",
    [
        (cap(default_policy="auto"), {}, "auto"),
        (cap(default_policy="ask"), {}, "ask"),
        (cap(default_policy="auto"), {"source_policy": "ask"}, "ask"),
        (cap(default_policy="ask"), {"tool_override": "auto"}, "auto"),
        (cap(permissions=["DESTRUCTIVE"]), {}, "deny"),
        (cap(permissions=["DESTRUCTIVE"]), {"tool_override": "ask"}, "ask"),
        (cap(status="disabled"), {}, "deny"),
        (cap(allowed_agents=["qa"]), {}, "deny"),  # agent below is developer
    ],
)
def test_policy_decisions(capability, kwargs, expected):
    assert decide(capability, "developer", **kwargs).policy == expected


def test_project_override_can_only_tighten_unless_tool_override(ext):
    from forgeflow.core.ids import utcnow
    from forgeflow.schemas.extensibility import Project

    project = Project(
        project_id="p",
        owner_id="local",
        name="p",
        repository_path="p",
        capability_policies={"x.op": "deny"},
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    assert decide(cap(), "developer", project).policy == "deny"
    assert decide(cap(), "developer", project, tool_override="auto").policy == "deny"


# ------------------------------------------------------------------ approvals


async def test_approval_wait_returns_after_decision(ext):
    approval = await ext.approvals.request(
        owner_id="local",
        workflow_id=None,
        task_id=None,
        agent="developer",
        capability_id="x",
        action="x",
        summary="do x",
        risk="medium",
    )
    waiter = asyncio.create_task(ext.approvals.wait(approval.approval_id))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    await ext.approvals.decide(approval.approval_id, True, "local", "ok")
    decided = await waiter
    assert decided.status == "approved" and decided.note == "ok"
    with pytest.raises(ValidationFailed, match="already approved"):
        await ext.approvals.decide(approval.approval_id, False, "local")
    with pytest.raises(NotFoundError):
        await ext.approvals.decide(approval.approval_id, False, "someone-else")


async def test_approval_expires(ext):
    approval = await ext.approvals.request(
        owner_id="local",
        workflow_id=None,
        task_id=None,
        agent="qa",
        capability_id="x",
        action="x",
        summary="x",
        risk="low",
        timeout_seconds=0.05,
    )
    assert (await ext.approvals.wait(approval.approval_id)).status == "expired"


# -------------------------------------------------------------------- gateway


async def test_gateway_audits_every_outcome(ext):
    inv = Invocation(owner_id="local", workflow_id="wf", task_id="t", agent="developer")

    async def action():
        return "done"

    assert await ext.gateway.invoke(inv, cap(), action, summary="auto op") == "done"
    with pytest.raises(CapabilityDenied):
        await ext.gateway.invoke(inv, cap(permissions=["DESTRUCTIVE"]), action, summary="rm")

    async def revoked():
        return "revoked"

    with pytest.raises(CapabilityDenied, match="revoked"):
        await ext.gateway.invoke(inv, cap(), action, summary="x", source_status=revoked)

    async def approve_soon():
        while True:
            pending = await ext.approvals.list_approvals("local", status="pending")
            if pending:
                await ext.approvals.decide(pending[0].approval_id, True, "local")
                return
            await asyncio.sleep(0.01)

    approver = asyncio.create_task(approve_soon())
    assert (
        await ext.gateway.invoke(inv, cap(default_policy="ask"), action, summary="ask op") == "done"
    )
    await approver
    audit = await ext.store.find("capability_audit", {"workflow_id": "wf"}, sort="timestamp")
    assert [(a["approval"], a["result"]) for a in audit] == [
        ("auto", "success"),
        ("denied_by_policy", "denied"),
        ("not_required", "denied"),
        ("user_approved", "success"),
    ]


# ------------------------------------------------------------------ connectors


async def test_github_connector_lifecycle(ext, fake_github):
    connector = await ext.connectors.create_github("local", "Mine", TOKEN, ["octo/app"])
    assert connector.status == "active" and connector.account == "octo"
    assert connector.scopes == ["octo/app:write"]
    assert fake_github.tokens[0] == f"Bearer {TOKEN}"
    dump = json.dumps(ext.store.data, default=str)
    assert TOKEN not in dump  # only ciphertext and a reference are stored

    disabled = await ext.connectors.set_enabled("local", connector.connector_id, False)
    assert disabled.status == "disabled"
    revoked = await ext.connectors.revoke("local", connector.connector_id)
    assert revoked.status == "revoked" and revoked.credential_ref is None
    assert await ext.store.get("secrets", f"secret://connector/{connector.connector_id}") is None
    with pytest.raises(ValidationFailed):
        await ext.connectors.set_enabled("local", connector.connector_id, True)
    with pytest.raises(NotFoundError):
        await ext.connectors.get("someone-else", connector.connector_id)


async def test_connector_requires_scope_and_push_permission(ext, fake_github):
    with pytest.raises(ValidationFailed, match="least privilege"):
        await ext.connectors.create_github("local", "x", TOKEN, [])
    fake_github.can_push = False
    connector = await ext.connectors.create_github("local", "ro", TOKEN, ["octo/app"])
    assert connector.status == "error" and "cannot push" in connector.last_error


async def test_github_pull_request_is_idempotent():
    fake = FakeGitHub(existing_pr=True)
    client = GitHubClient(TOKEN, transport=httpx.MockTransport(fake))
    pr = await client.create_pull_request("o/r", "forgeflow/wf_1", "main", "t", "b")
    assert pr.number == 7 and pr.head == "forgeflow/wf_1"
    assert TOKEN not in repr(client)


def test_repository_from_remote():
    assert repository_from_remote("git@github.com:octo/app.git") == "octo/app"
    assert repository_from_remote("https://github.com/octo/app") == "octo/app"
    assert repository_from_remote("https://gitlab.com/octo/app") is None


async def test_push_branch_keeps_token_out_of_config(git_repo, tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    head = git(git_repo, "rev-parse", "HEAD")
    await push_branch(git_repo, head, "forgeflow/wf_x", TOKEN, remote_url=str(remote))
    assert git(remote, "rev-parse", "refs/heads/forgeflow/wf_x") == head
    assert TOKEN not in (git_repo / ".git" / "config").read_text()
    with pytest.raises(ValidationFailed, match="forgeflow/"):
        await push_branch(git_repo, head, "main", TOKEN, remote_url=str(remote))


# -------------------------------------------------------------------- projects


async def test_project_binding_respects_connector_scope(ext, git_repo):
    connector = await ext.connectors.create_github("local", "Mine", TOKEN, ["octo/app"])
    project = await ext.projects.ensure("local", "app")
    assert project.project_id == "proj_app"
    with pytest.raises(ValidationFailed, match="not allowed"):
        await ext.projects.update(
            "local",
            project.project_id,
            github={"connector_id": connector.connector_id, "repository": "octo/other"},
        )
    updated = await ext.projects.update(
        "local",
        project.project_id,
        github={"connector_id": connector.connector_id, "repository": "octo/app"},
    )
    assert updated.github.repository == "octo/app"
    git(git_repo, "remote", "add", "origin", "https://github.com/octo/app.git")
    assert await ext.projects.detected_github_repository(updated) == "octo/app"


def test_github_catalog_asks_before_opening_a_pr():
    caps = {c.capability_id: c for c in github_capabilities("con", "local")}
    assert caps["github.pull_request.create"].default_policy == "ask"
    assert caps["github.branch.push"].default_policy == "auto"
    assert "github.pull_request.merge" not in caps
