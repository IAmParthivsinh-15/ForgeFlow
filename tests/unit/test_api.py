import httpx
import pytest

from forgeflow.apps.api.main import create_app


@pytest.fixture
async def client(container):
    async def factory():
        return container

    app = create_app(factory)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_create_answer_and_plan_via_api(client, service):
    r = await client.post(
        "/api/v1/workflows",
        json={"request": "Add forgot-password flow", "repository_path": "demo-app"},
    )
    assert r.status_code == 201
    workflow_id = r.json()["workflow_id"]

    await service.process_analysis(workflow_id, 1)  # normally triggered by the worker via Kafka

    detail = (await client.get(f"/api/v1/workflows/{workflow_id}")).json()
    assert detail["workflow"]["status"] == "AWAITING_CLARIFICATION"
    [question] = (await client.get(f"/api/v1/workflows/{workflow_id}/questions")).json()

    r = await client.post(
        f"/api/v1/questions/{question['question_id']}/answer", json={"selected_option": "A"}
    )
    assert r.status_code == 200
    assert r.json()["workflow"]["status"] == "PLANNING"

    await service.process_analysis(workflow_id, 2)
    detail = (await client.get(f"/api/v1/workflows/{workflow_id}")).json()
    assert detail["workflow"]["status"] == "PLANNED"
    assert detail["specification"]["version"] == 2
    assert detail["workflow"]["route_plan"]["stages"]

    versions = (await client.get(f"/api/v1/workflows/{workflow_id}/requirements")).json()
    assert [v["version"] for v in versions] == [1, 2]

    events = (await client.get(f"/api/v1/workflows/{workflow_id}/events?after=3")).json()
    assert events[0]["seq"] == 4


async def test_error_mapping(client):
    assert (await client.get("/api/v1/workflows/wf_missing")).status_code == 404
    r = await client.post("/api/v1/workflows", json={"request": "x", "repository_path": "../x"})
    assert r.status_code == 400
    r = await client.post("/api/v1/workflows", json={"request": ""})
    assert r.status_code == 422
    r = await client.post("/api/v1/questions/nope/answer", json={"selected_option": "A"})
    assert r.status_code == 404


async def test_repositories_listing(client):
    assert (await client.get("/api/v1/repositories")).json() == ["demo-app"]
