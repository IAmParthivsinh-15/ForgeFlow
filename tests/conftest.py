from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from forgeflow.apps.container import Container, assemble
from forgeflow.core.config import Settings
from forgeflow.platform.orchestration.fake_gateway import FakeAgentGateway
from forgeflow.platform.state.memory import InMemoryWorkflowStore
from forgeflow.schemas.task import TaskStatus
from forgeflow.tools.security.scanners import ScannerSuite
from tests.fakes import FakeCI, FakeGitHub

ROOT = Path(__file__).resolve().parents[1]
PRESETS = ROOT / "config" / "mcp_presets.yaml"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "core.autocrlf=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repos_root(tmp_path: Path) -> Path:
    repo = tmp_path / "repos" / "demo-app"
    (repo / "backend").mkdir(parents=True)
    (repo / "backend" / "auth.py").write_text("def login(user, password):\n    return True\n")
    (repo / "README.md").write_text("# Demo app\n")
    (repo / ".env").write_text("SECRET=do-not-read\n")
    return tmp_path / "repos"


@pytest.fixture
def git_repo(repos_root: Path) -> Path:
    """A real git repository with one commit at repos/app."""
    repo = repos_root / "app"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("def handler():\n    return 'ok'\n")
    (repo / "README.md").write_text("# App\n")
    # Allowlisted repository commands used by QA and CI (spec section 75).
    (repo / "forgeflow.yaml").write_text("commands:\n  test: python -c \"print('tests ok')\"\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "dev@example.com")
    git(repo, "config", "user.name", "Dev")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    return repo


@pytest.fixture
def mcp_allowlist(tmp_path: Path) -> Path:
    import sys

    import yaml

    path = tmp_path / "mcp_allowlist.yaml"
    server = Path(__file__).with_name("mcp_test_server.py").resolve()
    browser = Path(__file__).with_name("mcp_browser_server.py").resolve()
    path.write_text(
        yaml.safe_dump(
            {
                "servers": {
                    "test": {"command": sys.executable, "args": [str(server)], "env": []},
                    "browser-test": {
                        "command": sys.executable,
                        "args": [str(browser)],
                        "env": [],
                    },
                }
            }
        )
    )
    return path


@pytest.fixture
def mcp_presets(tmp_path: Path) -> Path:
    """The real Playwright preset, pointed at the stdio stand-in instead of the container."""
    import yaml

    real = yaml.safe_load(PRESETS.read_text(encoding="utf-8"))["presets"]["playwright"]
    preset = {**real, "transport": "stdio", "stdio_server": "browser-test", "url": None}
    path = tmp_path / "mcp_presets.yaml"
    path.write_text(yaml.safe_dump({"presets": {"playwright": preset}}))
    return path


@pytest.fixture
def settings(repos_root: Path, tmp_path: Path, mcp_allowlist: Path, mcp_presets: Path) -> Settings:
    from forgeflow.extensibility.secrets import generate_key

    return Settings(
        _env_file=None,
        forgeflow_env="test",
        fake_llm=True,
        repos_root=repos_root,
        workspaces_root=tmp_path / "workspaces",
        max_clarification_rounds=2,
        task_retry_backoff_seconds=0,
        heartbeat_interval_seconds=0.05,
        forgeflow_secret_key=generate_key(),
        approval_poll_seconds=0.02,
        approval_timeout_seconds=30,
        mcp_stdio_allowlist_path=mcp_allowlist,
        mcp_presets_path=mcp_presets,
        builtin_skills_path=ROOT / "config" / "skills",
        artifacts_root=tmp_path / "artifacts",
        preview_host="127.0.0.1",
        preview_ports="47173-47176",
        preview_start_timeout_seconds=15,
        # Pushes go to local bare repositories instead of github.com.
        github_push_url_template=str(tmp_path / "remotes" / "{repository}.git"),
    )


@pytest.fixture
def store() -> InMemoryWorkflowStore:
    return InMemoryWorkflowStore()


@pytest.fixture
def fake_ci() -> FakeCI:
    return FakeCI()


@pytest.fixture
def fake_github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def container(store, settings, fake_ci, fake_github) -> Container:
    import httpx

    from forgeflow.integrations.github.client import GitHubClient
    from forgeflow.knowledge.index import InMemoryKnowledgeIndex

    c = assemble(settings, store, FakeAgentGateway(), knowledge_index=InMemoryKnowledgeIndex())
    c.ci = fake_ci
    c.scanners = ScannerSuite([])  # real scanners are exercised in test_scanners.py
    assert c.extensibility is not None
    c.extensibility.connectors.github_factory = lambda token, url: GitHubClient(
        token, url, transport=httpx.MockTransport(fake_github)
    )
    return c


@pytest.fixture
def ext(container):
    return container.extensibility


@pytest.fixture
def service(container):
    return container.service


async def drive(container: Container, workflow_id: str, max_rounds: int = 30) -> None:
    """Stand-in for Kafka + workers: tick, run every dispatched task concurrently, repeat."""
    executor = container.executor("test-worker")
    for _ in range(max_rounds):
        await container.execution.tick(workflow_id)
        dispatched = [
            t
            for t in await container.store.list_tasks(workflow_id)
            if t.status == TaskStatus.DISPATCHED
        ]
        if not dispatched:
            return
        await asyncio.gather(*(executor.execute(t.task_id, t.attempt) for t in dispatched))
    raise AssertionError("workflow did not settle")
