import io
import json
import zipfile

import httpx
import pytest

from forgeflow.apps.api.main import create_app

TOKEN = "github_pat_" + "C" * 40


@pytest.fixture
async def client(container):
    async def factory():
        return container

    app = create_app(factory)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_connector_api_never_returns_credentials(client):
    r = await client.post(
        "/api/v1/connectors", json={"name": "GitHub", "token": TOKEN, "repositories": ["octo/app"]}
    )
    assert r.status_code == 201
    body = r.json()
    assert body["connector"]["status"] == "active" and body["has_credential"] is True
    assert body["connector"]["credential_ref"] is None
    listing = (await client.get("/api/v1/connectors")).text
    assert TOKEN not in r.text and TOKEN not in listing and "secret://" not in listing
    cid = body["connector"]["connector_id"]
    assert (await client.post(f"/api/v1/connectors/{cid}/revoke")).json()["has_credential"] is False
    assert (
        await client.post("/api/v1/connectors", json={"token": TOKEN, "repositories": []})
    ).status_code == 422


async def test_project_skill_mcp_and_manifest_api(client, git_repo):
    projects = (await client.get("/api/v1/projects")).json()
    assert "proj_app" in {p["project_id"] for p in projects}

    meta = {
        "name": "Python Style",
        "slug": "python-style",
        "version": "1.0.0",
        "description": "House Python rules",
        "tags": ["python"],
    }
    r = await client.post(
        "/api/v1/skills", json={"metadata": meta, "instructions": "Prefer dataclasses."}
    )
    assert r.status_code == 201
    skill_id = r.json()["skill"]["skill_id"]
    r = await client.post(f"/api/v1/skills/{skill_id}/enable", json={"project_id": "proj_app"})
    assert r.json()["version"] == "1.0.0"

    allow = (await client.get("/api/v1/mcps/allowlist")).json()
    assert "test" in allow
    r = await client.post(
        "/api/v1/mcps", json={"name": "tickets", "transport": "stdio", "stdio_server": "test"}
    )
    assert r.status_code == 201 and len(r.json()["tools"]) == 3
    mcp_id = r.json()["server"]["mcp_id"]
    r = await client.patch(f"/api/v1/mcps/{mcp_id}/tools/create_ticket", json={"policy": "deny"})
    assert r.json()["policy_override"] == "deny"
    r = await client.patch("/api/v1/projects/proj_app", json={"enabled_mcp_ids": [mcp_id]})
    assert r.json()["enabled_mcp_ids"] == [mcp_id]

    manifest = (
        await client.get(
            "/api/v1/capabilities/available",
            params={"project_id": "proj_app", "agent": "qa", "context": "python"},
        )
    ).json()
    assert [s["slug"] for s in manifest["skills"]] == ["python-style"]
    assert [t["name"] for t in manifest["mcp_tools"]] == ["tickets.get_weather"]
    caps = (await client.get("/api/v1/capabilities")).json()
    assert {"native_tool", "mcp_tool", "skill"} <= {c["type"] for c in caps}


async def test_skill_upload_and_rejection(client):
    def pkg(text, slug="uploaded"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("skill.md", text)
            z.writestr(
                "metadata.json",
                json.dumps({"name": "Up", "slug": slug, "version": "1.0.0", "description": "d"}),
            )
        return buf.getvalue()

    r = await client.post(
        "/api/v1/skills/upload", files={"file": ("s.zip", pkg("Be careful."), "application/zip")}
    )
    assert r.status_code == 201 and r.json()["skill"]["slug"] == "uploaded"
    r = await client.post(
        "/api/v1/skills/upload",
        files={"file": ("s.zip", pkg("key ghp_" + "x" * 36, "leaky"), "application/zip")},
    )
    assert r.status_code == 422 and "credential" in r.json()["detail"]


async def test_approvals_api(client, container):
    approval = await container.extensibility.approvals.request(
        owner_id="local",
        workflow_id=None,
        task_id=None,
        agent="developer",
        capability_id="github.pull_request.create",
        action="x",
        summary="Open PR",
        risk="medium",
    )
    pending = (await client.get("/api/v1/approvals", params={"status": "pending"})).json()
    assert [a["approval_id"] for a in pending] == [approval.approval_id]
    r = await client.post(
        f"/api/v1/approvals/{approval.approval_id}/reject", json={"note": "later"}
    )
    assert r.json()["status"] == "rejected"
    again = await client.post(f"/api/v1/approvals/{approval.approval_id}/approve", json={})
    assert again.status_code == 422
