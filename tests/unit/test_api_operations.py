"""API: knowledge search, MCP presets, artifacts, deployment checks, Argo CD connectors."""

import json

import httpx
import pytest

from forgeflow.apps.api.main import create_app
from forgeflow.integrations.argocd.client import ArgoCDClient
from tests.unit.test_deployments import TOKEN, FakeArgo


@pytest.fixture
async def client(container):
    async def factory():
        return container

    app = create_app(factory)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_knowledge_status_index_and_search(client, container, git_repo):
    status = (await client.get("/api/v1/knowledge/status")).json()
    assert status["enabled"] and status["available"] and status["embeddings"] is None
    projects = (await client.get("/api/v1/projects")).json()
    project_id = next(p["project_id"] for p in projects if p["repository_path"] == "app")
    r = await client.post(f"/api/v1/knowledge/projects/{project_id}/index", json={})
    assert r.status_code == 200 and r.json()["chunks"] >= 3
    hits = (
        await client.get("/api/v1/knowledge/search", params={"q": "handler", "kind": "code"})
    ).json()
    assert hits[0]["path"] == "src/app.py" and hits[0]["kind"] == "code"
    assert (
        await client.get("/api/v1/knowledge/search", params={"q": "nothing-matches-this"})
    ).json() == []
    status = (await client.get("/api/v1/knowledge/status")).json()
    assert (
        status["counts"]["code"] >= 3 and status["repositories"][0]["repository_id"] == project_id
    )


async def test_builtin_skills_are_seeded_and_presets_enable_browser_qa(client, container, git_repo):
    skills = (await client.get("/api/v1/skills", params={"scope": "public"})).json()
    playwright = next(s for s in skills if s["slug"] == "playwright-mcp")
    assert playwright["trust"] == "verified" and playwright["owner_id"] == "system"

    presets = (await client.get("/api/v1/mcps/presets")).json()
    [preset] = presets
    assert preset["key"] == "playwright" and "browser_run_code_unsafe" in preset["denied_tools"]

    await client.get("/api/v1/projects")  # projects appear when listed (as in the UI)
    r = await client.post("/api/v1/mcps/presets/playwright", json={"project_id": "proj_app"})
    assert r.status_code == 201, r.text
    server = r.json()
    assert server["server"]["preset"] == "playwright" and not server["has_credential"]
    project = (await client.get("/api/v1/projects/proj_app")).json()["project"]
    assert server["server"]["mcp_id"] in project["enabled_mcp_ids"]
    available = (
        await client.get(
            "/api/v1/capabilities/available", params={"project_id": "proj_app", "agent": "qa"}
        )
    ).json()
    assert [s["slug"] for s in available["skills"]] == ["playwright-mcp"]
    assert any(t["name"] == "playwright.browser_navigate" for t in available["mcp_tools"])
    # Other agents get neither.
    dev = (
        await client.get(
            "/api/v1/capabilities/available",
            params={"project_id": "proj_app", "agent": "developer"},
        )
    ).json()
    assert dev["mcp_tools"] == [] and dev["skills"] == []


async def test_artifacts_are_served_with_safe_headers(client, container):
    artifact = await container.artifacts.save(
        workflow_id="wf_1",
        task_id="wf_1.V1-qa",
        type="screenshot",
        name="shot",
        content=b"\x89PNG fake",
        content_type="image/png",
    )
    r = await client.get(f"/api/v1/artifacts/{artifact.artifact_id}")
    assert r.status_code == 200 and r.content == b"\x89PNG fake"
    assert (
        r.headers["content-type"] == "image/png"
        and r.headers["x-content-type-options"] == "nosniff"
    )
    listed = (await client.get("/api/v1/workflows/wf_1/artifacts")).json()
    assert [a["artifact_id"] for a in listed] == [artifact.artifact_id]
    assert (await client.get("/api/v1/artifacts/art_missing")).status_code == 404


async def test_argocd_connector_and_deployment_check_over_the_api(client, container, git_repo):
    argo = FakeArgo(health="Degraded")
    container.extensibility.connectors.argocd_factory = lambda url, token, verify: ArgoCDClient(
        url, token, transport=httpx.MockTransport(argo)
    )
    r = await client.post(
        "/api/v1/connectors",
        json={
            "type": "argocd",
            "name": "Argo CD",
            "url": "https://argocd.example",
            "token": TOKEN,
            "applications": ["shop"],
        },
    )
    assert r.status_code == 201, r.text
    assert TOKEN not in json.dumps(r.json())
    connector_id = r.json()["connector"]["connector_id"]
    caps = (await client.get("/api/v1/capabilities")).json()
    assert {"deployment.status", "deployment.rollback"} <= {c["capability_id"] for c in caps}

    await client.get("/api/v1/projects")
    r = await client.patch(
        "/api/v1/projects/proj_app",
        json={"deployment": {"connector_id": connector_id, "application": "shop"}},
    )
    assert r.status_code == 200, r.text
    check = (await client.post("/api/v1/projects/proj_app/deployments/verify")).json()
    assert check["status"] == "unhealthy" and check["rollback"] == "available"
    assert (await client.get("/api/v1/deployments", params={"project_id": "proj_app"})).json()[0][
        "check_id"
    ] == check["check_id"]
    # A rollback must be explicitly confirmed, and then only creates an approval request.
    assert (
        await client.post(f"/api/v1/deployments/{check['check_id']}/rollback", json={})
    ).status_code == 422
    r = await client.post(
        f"/api/v1/deployments/{check['check_id']}/rollback", json={"confirm": True}
    )
    assert r.status_code == 202 and r.json()["rollback"] == "requested"
    assert argo.rollbacks == []
