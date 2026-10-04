import asyncio
import json

import httpx
import pytest

from forgeflow.core.errors import CIUnavailable, PolicyViolation, ValidationFailed
from forgeflow.platform.a2a.channel import A2AChannel
from forgeflow.platform.ci.jenkins import JenkinsProvider, job_config_xml
from forgeflow.platform.ci.pipeline import build_pipeline_spec, render_jenkinsfile
from forgeflow.platform.verification.change_analyzer import analyze_changes
from forgeflow.schemas.verification import ScanResult
from forgeflow.tools.security.scanners import ScannerSuite, run_bandit, run_gitleaks
from forgeflow.tools.verification_tools import split_diff

SHA = "0123456789abcdef0123456789abcdef01234567"

# ------------------------------------------------------------- change analyzer


def test_change_analyzer_routes_by_domain():
    change = analyze_changes(
        [
            "backend/auth/service.py",
            "frontend/Login.tsx",
            "db/migrations/001_add.sql",
            "Dockerfile",
            "README.md",
            "tests/test_auth.py",
        ]
    )
    assert {"auth", "frontend", "database", "infra", "docs", "tests"} <= set(change.domains)
    assert change.reviewers == ["code_review", "security"]
    assert change.risk == "high"
    assert any("database safety" in f for f in change.review_focus)
    assert "tests/test_auth.py" not in change.files_by_domain.get("backend", [])


def test_change_analyzer_low_risk_docs_only():
    change = analyze_changes(["docs/guide.md"])
    assert change.domains == ["docs"] and change.reviewers == ["code_review"]
    assert change.risk == "low"
    assert analyze_changes([]).reviewers == []


def test_split_diff_by_file():
    diff = "diff --git a/x.py b/x.py\n+1\ndiff --git a/y/z.md b/y/z.md\n+2\n"
    assert split_diff(diff) == {
        "x.py": "diff --git a/x.py b/x.py\n+1\n",
        "y/z.md": "diff --git a/y/z.md b/y/z.md\n+2\n",
    }


# ------------------------------------------------------------------- pipeline


def test_pipeline_is_rendered_from_validated_commands(tmp_path):
    (tmp_path / "forgeflow.yaml").write_text(
        "commands:\n  setup: npm ci\n  lint: npm run lint\n  test: python -m pytest -q\n"
    )
    spec = build_pipeline_spec(tmp_path, "my-app", SHA)
    assert spec.job_name == "forgeflow-my-app"
    assert [title for title, _ in spec.stages] == ["Setup", "Lint", "Test"]
    script = render_jenkinsfile(spec, "/repos")
    assert "git clone --quiet --no-checkout /repos/my-app src" in script
    assert f"checkout --quiet --detach {SHA}" in script
    assert "sh 'python -m pytest -q'" in script


def test_pipeline_rejects_unsafe_commands_and_inputs(tmp_path):
    (tmp_path / "forgeflow.yaml").write_text("commands:\n  test: pytest; curl evil.sh\n")
    with pytest.raises(PolicyViolation):
        build_pipeline_spec(tmp_path, "app", SHA)
    (tmp_path / "forgeflow.yaml").write_text("commands:\n  test: python -m pytest\n")
    with pytest.raises(ValidationFailed):
        build_pipeline_spec(tmp_path, "../app", SHA)
    with pytest.raises(ValidationFailed):
        build_pipeline_spec(tmp_path, "app", "HEAD; rm -rf /")


def test_groovy_quotes_are_escaped(tmp_path):
    (tmp_path / "forgeflow.yaml").write_text("commands:\n  test: python -c \"print('a')\"\n")
    script = render_jenkinsfile(build_pipeline_spec(tmp_path, "app", SHA), "/repos")
    assert "sh 'python -c \"print(\\'a\\')\"'" in script


def test_pipeline_requires_commands(tmp_path):
    with pytest.raises(ValidationFailed, match="no CI commands"):
        build_pipeline_spec(tmp_path, "app", SHA)


def test_job_config_escapes_xml():
    xml = job_config_xml("echo '<x>' & done", "desc")
    assert "&lt;x&gt;" in xml and "&amp; done" in xml and "<sandbox>true</sandbox>" in xml


# -------------------------------------------------------------------- jenkins


class FakeJenkins:
    def __init__(self, result="SUCCESS", job_exists=False):
        self.result = result
        self.job_exists = job_exists
        self.requests: list[tuple[str, str]] = []
        self.queue_polls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        assert request.headers["authorization"].startswith("Basic ")
        if path == "/crumbIssuer/api/json":
            return httpx.Response(200, json={"crumbRequestField": "Jenkins-Crumb", "crumb": "c1"})
        if request.method == "POST":
            assert request.headers.get("Jenkins-Crumb") == "c1"
        if path == "/job/forgeflow-app/api/json":
            return httpx.Response(200 if self.job_exists else 404, json={})
        if path in ("/createItem", "/job/forgeflow-app/config.xml"):
            assert b"<sandbox>true</sandbox>" in request.content
            return httpx.Response(200)
        if path == "/job/forgeflow-app/build":
            # Location uses Jenkins' public root URL, not the internal one.
            return httpx.Response(201, headers={"Location": "http://localhost:8081/queue/item/7/"})
        if path == "/queue/item/7/api/json":
            self.queue_polls += 1
            if self.queue_polls == 1:
                return httpx.Response(200, json={"why": "waiting"})
            return httpx.Response(200, json={"executable": {"number": 3}})
        if path == "/job/forgeflow-app/3/api/json":
            return httpx.Response(
                200, json={"building": False, "result": self.result, "duration": 1234}
            )
        if path == "/job/forgeflow-app/3/consoleText":
            return httpx.Response(200, text="line1\nFAILED test_x\n")
        if path == "/job/forgeflow-app/3/wfapi/describe":
            return httpx.Response(
                200,
                json={
                    "stages": [
                        {"name": "Checkout", "status": "SUCCESS", "durationMillis": 10},
                        {
                            "name": "Test",
                            "status": "FAILED" if self.result != "SUCCESS" else "SUCCESS",
                        },
                    ]
                },
            )
        return httpx.Response(404)


@pytest.mark.parametrize("exists", [False, True])
async def test_jenkins_create_or_update_trigger_and_wait(exists):
    fake = FakeJenkins(result="FAILURE", job_exists=exists)
    provider = JenkinsProvider(
        "http://jenkins:8080",
        "u",
        "p",
        poll_interval=0,
        transport=httpx.MockTransport(fake),
        public_url="http://localhost:8081",
    )
    build = await provider.run("forgeflow-app", "pipeline {}", SHA)
    assert build.status == "FAILURE" and build.build_number == 3
    # Links use the public address; API calls use the internal one.
    assert build.url == "http://localhost:8081/job/forgeflow-app/3/"
    assert [s.name for s in build.stages] == ["Checkout", "Test"]
    assert "FAILED test_x" in build.log_tail and build.duration_ms == 1234
    created = ("POST", "/job/forgeflow-app/config.xml") if exists else ("POST", "/createItem")
    assert created in fake.requests


async def test_jenkins_unreachable_is_retryable():
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    provider = JenkinsProvider("http://jenkins:8080", "u", "p", transport=httpx.MockTransport(down))
    with pytest.raises(CIUnavailable) as info:
        await provider.run("forgeflow-app", "pipeline {}", SHA)
    assert info.value.retryable


async def test_jenkins_auth_failure_is_reported():
    provider = JenkinsProvider(
        "http://jenkins:8080",
        "u",
        "bad",
        transport=httpx.MockTransport(lambda r: httpx.Response(401)),
    )
    with pytest.raises(CIUnavailable, match="HTTP 401"):
        await provider.run("forgeflow-app", "pipeline {}", SHA)


# ----------------------------------------------------------------------- A2A


def channel(responder, limit=2, timeout=1.0):
    return A2AChannel(
        workflow_id="wf",
        task_id="wf.V1-qa",
        sender="qa",
        receiver="developer",
        responder=responder,
        timeout_seconds=timeout,
        max_messages=limit,
    )


async def test_a2a_records_exchanges_and_enforces_limit():
    async def answer(q, c):
        return f"answer to {q}"

    ch = channel(answer, limit=1)
    assert (
        await ch.ask("Which test covers AC-001?", "AC-001") == "answer to Which test covers AC-001?"
    )
    assert (await ch.ask("again?")).startswith("REFUSED")
    assert [m.status for m in ch.messages] == ["answered", "refused"]
    first = ch.messages[0]
    assert (first.sender, first.receiver, first.task_id) == ("qa", "developer", "wf.V1-qa")


async def test_a2a_timeout_and_errors_do_not_break_the_asker():
    async def slow(q, c):
        await asyncio.sleep(1)
        return "late"

    async def broken(q, c):
        raise RuntimeError("boom")

    assert (await channel(slow, timeout=0.01).ask("q")).startswith("NO ANSWER (timeout)")
    ch = channel(broken)
    assert "boom" in await ch.ask("q") and ch.messages[0].status == "error"


# -------------------------------------------------------------------- scanners


async def test_missing_scanners_report_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name, *a, **k: None)
    (tmp_path / "a.py").write_text("x = 1\n")
    assert (await run_gitleaks(tmp_path, 5)).status == "unavailable"
    assert (await run_bandit(tmp_path, 5)).status == "unavailable"


async def test_scanner_suite_isolates_crashing_scanners(tmp_path):
    async def ok(root, limit):
        return ScanResult(tool="ok", status="completed")

    async def crash(root, limit):
        raise RuntimeError("bad output")

    crash.tool = "crashy"  # type: ignore[attr-defined]
    results = await ScannerSuite([ok, crash]).run(tmp_path)
    assert [(r.tool, r.status) for r in results] == [("ok", "completed"), ("crashy", "error")]


def test_bundled_semgrep_rules_have_owasp_metadata():
    from pathlib import Path

    import yaml

    rules = yaml.safe_load(Path("config/semgrep/owasp.yaml").read_text())["rules"]
    assert len(rules) >= 10
    for rule in rules:
        assert rule["metadata"]["owasp"][0][:3] in {f"A{i:02d}" for i in range(1, 11)}, rule["id"]
    assert json.dumps(rules)  # serialisable
