from __future__ import annotations

from pathlib import Path

import pytest

from forgeflow.apps.container import Container
from forgeflow.core.config import Settings
from forgeflow.platform.orchestration.fake_gateway import FakeAgentGateway
from forgeflow.platform.orchestration.repositories import RepositoryResolver
from forgeflow.platform.orchestration.service import WorkflowService
from forgeflow.platform.state.memory import InMemoryWorkflowStore


@pytest.fixture
def repos_root(tmp_path: Path) -> Path:
    repo = tmp_path / "repos" / "demo-app"
    (repo / "backend").mkdir(parents=True)
    (repo / "backend" / "auth.py").write_text("def login(user, password):\n    return True\n")
    (repo / "README.md").write_text("# Demo app\n")
    (repo / ".env").write_text("SECRET=do-not-read\n")
    return tmp_path / "repos"


@pytest.fixture
def settings(repos_root: Path) -> Settings:
    return Settings(
        _env_file=None,
        forgeflow_env="test",
        fake_llm=True,
        repos_root=repos_root,
        max_clarification_rounds=2,
    )


@pytest.fixture
def store() -> InMemoryWorkflowStore:
    return InMemoryWorkflowStore()


@pytest.fixture
def service(store, settings) -> WorkflowService:
    return WorkflowService(
        store, FakeAgentGateway(), RepositoryResolver(settings.repos_root), settings
    )


@pytest.fixture
def container(store, service, settings) -> Container:
    return Container(settings, store, service, service.repositories)
