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
from tests.fakes import FakeCI


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
def settings(repos_root: Path, tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        forgeflow_env="test",
        fake_llm=True,
        repos_root=repos_root,
        workspaces_root=tmp_path / "workspaces",
        max_clarification_rounds=2,
        task_retry_backoff_seconds=0,
        heartbeat_interval_seconds=0.05,
    )


@pytest.fixture
def store() -> InMemoryWorkflowStore:
    return InMemoryWorkflowStore()


@pytest.fixture
def fake_ci() -> FakeCI:
    return FakeCI()


@pytest.fixture
def container(store, settings, fake_ci) -> Container:
    c = assemble(settings, store, FakeAgentGateway())
    c.ci = fake_ci
    c.scanners = ScannerSuite([])  # real scanners are exercised in test_scanners.py
    return c


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
