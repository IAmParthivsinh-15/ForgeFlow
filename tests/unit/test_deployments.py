"""Argo CD connector, deployment verification and approval-gated rollback (spec 43, 157)."""

import asyncio
import json

import httpx
import pytest

from forgeflow.core.errors import ValidationFailed
from forgeflow.integrations.argocd.client import ArgoCDClient, validate_application

TOKEN = "argocd-token-" + "x" * 30


class FakeArgo:
    def __init__(self, health="Healthy", sync="Synced"):
        self.health, self.sync = health, sync
        self.rollbacks: list[dict] = []
        self.auth: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.auth.append(request.headers.get("authorization", ""))
        path = request.url.path
        if path == "/api/v1/session/userinfo":
            return httpx.Response(200, json={"loggedIn": True, "username": "forgeflow"})
        if path == "/api/v1/applications/shop" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "status": {
                        "sync": {"status": self.sync, "revision": "c0ffee2"},
                        "health": {"status": self.health, "message": "1/2 pods ready"},
                        "history": [
                            {"id": 4, "revision": "aaaa111", "deployedAt": "2026-10-01T10:00:00Z"},
                            {"id": 5, "revision": "c0ffee2", "deployedAt": "2026-10-05T10:00:00Z"},
                        ],
                    }
                },
            )
        if path == "/api/v1/applications/shop/rollback":
            self.rollbacks.append(json.loads(request.content))
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"message": "not found"})


@pytest.fixture
def argo():
    return FakeArgo()


@pytest.fixture
def wired(container, argo):
    ext = container.extensibility
    ext.connectors.argocd_factory = lambda url, token, verify: ArgoCDClient(
        url, token, transport=httpx.MockTransport(argo)
    )
    return container


def health_server(status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status if request.url.path == "/healthz" else 200, text="ok")

    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def bound(container, git_repo, health_status=200):
    ext = container.extensibility
    connector = await ext.connectors.create_argocd(
        "local", "Argo CD", "https://argocd.example", TOKEN, ["shop"]
    )
    assert connector.status == "active" and connector.account == "forgeflow"
    assert connector.scopes == ["shop:read"]
    project = await ext.projects.ensure("local", "app")
    await ext.projects.update(
        "local",
        project.project_id,
        deployment={
            "connector_id": connector.connector_id,
            "application": "shop",
            "health_url": "https://shop.example/healthz",
            "smoke_paths": ["/", "/cart"],
        },
    )
    container.deployments.http_factory = health_server(health_status)
    return project, connector


def test_application_names_are_validated():
    assert validate_application("shop-prod") == "shop-prod"
    for bad in ("../x", "Shop", "a b", ""):
        with pytest.raises(ValidationFailed):
            validate_application(bad)


async def test_argocd_connector_is_scoped_and_never_returns_the_token(wired, argo):
    ext = wired.extensibility
    with pytest.raises(ValidationFailed):
        await ext.connectors.create_argocd("local", "x", "https://argocd.example", TOKEN, [])
    with pytest.raises(ValidationFailed):
        await ext.connectors.create_argocd(
            "local", "x", "https://u:p@argocd.example", TOKEN, ["shop"]
        )
    connector = await ext.connectors.create_argocd(
        "local", "Argo CD", "https://argocd.example", TOKEN, ["shop"]
    )
    assert argo.auth[-1] == f"Bearer {TOKEN}"
    doc = await ext.store.get("connectors", connector.connector_id)
    assert TOKEN not in json.dumps(doc, default=str)
    # An Argo CD connector cannot be used as a GitHub client.
    with pytest.raises(ValidationFailed):
        await ext.connectors.client(connector)


async def test_project_binding_requires_an_argocd_connector_and_allowed_app(wired, git_repo):
    ext = wired.extensibility
    gh = await ext.connectors.create_github(
        "local", "GitHub", "github_pat_" + "B" * 40, ["octo/app"]
    )
    argo = await ext.connectors.create_argocd(
        "local", "Argo", "https://argocd.example", TOKEN, ["shop"]
    )
    project = await ext.projects.ensure("local", "app")
    with pytest.raises(ValidationFailed, match="Argo CD connector"):
        await ext.projects.update(
            "local",
            project.project_id,
            deployment={"connector_id": gh.connector_id, "application": "shop"},
        )
    with pytest.raises(ValidationFailed, match="not allowed"):
        await ext.projects.update(
            "local",
            project.project_id,
            deployment={"connector_id": argo.connector_id, "application": "other"},
        )
    with pytest.raises(ValidationFailed, match="smoke paths"):
        await ext.projects.update(
            "local",
            project.project_id,
            deployment={
                "connector_id": argo.connector_id,
                "application": "shop",
                "smoke_paths": ["cart"],
            },
        )


async def test_healthy_deployment(wired, git_repo):
    project, _ = await bound(wired, git_repo)
    check = await wired.deployments.verify("local", project.project_id)
    assert check.status == "healthy" and check.rollback == "not_needed"
    assert [p.name for p in check.probes] == ["argocd", "health", "smoke /", "smoke /cart"]
    assert check.revision == "c0ffee2" and check.sync_status == "Synced"
    audit = await wired.extensibility.store.find(
        "capability_audit", {"capability_id": "deployment.status"}
    )
    assert audit and audit[0]["approval"] == "auto"


async def rollback_flow(wired, argo, git_repo, approve: bool):
    argo.health = "Degraded"
    project, _ = await bound(wired, git_repo, health_status=503)
    check = await wired.deployments.verify("local", project.project_id)
    assert check.status == "unhealthy" and check.rollback == "available" and check.rollback_to == 4
    assert "1/2 pods ready" in check.probes[0].detail

    check = await wired.deployments.request_rollback("local", check.check_id)
    assert check.rollback == "requested"
    with pytest.raises(ValidationFailed):  # only one rollback per check
        await wired.deployments.request_rollback("local", check.check_id)
    ext = wired.extensibility
    for _ in range(200):
        pending = await ext.approvals.list_approvals("local", status="pending")
        if pending:
            break
        await asyncio.sleep(0.02)
    [approval] = pending
    assert approval.capability_id == "deployment.rollback" and approval.workflow_id is None
    assert approval.risk == "high" and approval.details["history_id"] == 4
    assert argo.rollbacks == []  # nothing happens before a human decides
    await ext.approvals.decide(approval.approval_id, approve, "local")
    for _ in range(200):
        check = await wired.deployments.get("local", check.check_id)
        if check.rollback not in ("requested",):
            break
        await asyncio.sleep(0.02)
    return check


async def test_unhealthy_deployment_rolls_back_only_after_approval(wired, argo, git_repo):
    check = await rollback_flow(wired, argo, git_repo, approve=True)
    assert check.rollback == "done"
    assert argo.rollbacks == [{"id": 4, "prune": False}]


async def test_rejected_rollback_changes_nothing(wired, argo, git_repo):
    check = await rollback_flow(wired, argo, git_repo, approve=False)
    assert check.rollback == "rejected" and argo.rollbacks == []


async def test_interrupted_rollbacks_become_available_again(wired, argo, git_repo):
    argo.health = "Degraded"
    project, _ = await bound(wired, git_repo, health_status=503)
    check = await wired.deployments.verify("local", project.project_id)
    store = wired.extensibility.store
    await store.compare_and_set("deployment_checks", check.check_id, {}, {"rollback": "requested"})
    assert await wired.deployments.recover_interrupted() == 1
    check = await wired.deployments.get("local", check.check_id)
    assert check.rollback == "available" and "restart" in check.rollback_detail
