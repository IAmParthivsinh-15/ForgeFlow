"""Composition root shared by the API and worker processes."""

from __future__ import annotations

from dataclasses import dataclass

from agents import set_tracing_disabled
from dotenv import load_dotenv
from pymongo import AsyncMongoClient
from redis.asyncio import Redis

from forgeflow.core.config import Settings
from forgeflow.models.config import ModelRegistry
from forgeflow.models.executor import AgentExecutor
from forgeflow.platform.events.coordination import Coordinator
from forgeflow.platform.execution.executor import TaskExecutor
from forgeflow.platform.orchestration.execution import ExecutionService
from forgeflow.platform.orchestration.fake_gateway import FakeAgentGateway
from forgeflow.platform.orchestration.gateway import AgentGateway, SdkAgentGateway
from forgeflow.platform.orchestration.repositories import RepositoryResolver
from forgeflow.platform.orchestration.service import WorkflowService
from forgeflow.platform.state.mongo import MongoWorkflowStore
from forgeflow.platform.state.store import WorkflowStore
from forgeflow.platform.worktrees.manager import LockFactory, WorktreeManager
from forgeflow.tools.git.client import GitClient


@dataclass
class Container:
    settings: Settings
    store: WorkflowStore
    gateway: AgentGateway
    service: WorkflowService
    execution: ExecutionService
    repositories: RepositoryResolver
    git: GitClient
    worktrees: WorktreeManager
    registry: ModelRegistry | None = None
    mongo: AsyncMongoClient | None = None
    redis: Redis | None = None

    def executor(self, worker_id: str | None = None) -> TaskExecutor:
        return TaskExecutor(
            self.store, self.gateway, self.worktrees, self.git, self.settings, worker_id
        )

    async def close(self) -> None:
        if self.redis is not None:
            await self.redis.aclose()
        if self.mongo is not None:
            await self.mongo.close()


def load_environment() -> None:
    """Expose .env values (API keys, model ids) to os.environ for the model registry."""
    load_dotenv(override=False)


def build_gateway(settings: Settings) -> tuple[AgentGateway, ModelRegistry | None]:
    if settings.fake_llm:
        return FakeAgentGateway(), None
    # SDK tracing exports to OpenAI's trace backend; off unless explicitly enabled.
    set_tracing_disabled(not settings.sdk_tracing)
    registry = ModelRegistry.from_path(settings.models_config_path)
    return SdkAgentGateway(AgentExecutor(registry, max_turns=settings.agent_max_turns)), registry


def assemble(
    settings: Settings,
    store: WorkflowStore,
    gateway: AgentGateway,
    *,
    locks: LockFactory | None = None,
    registry: ModelRegistry | None = None,
    mongo: AsyncMongoClient | None = None,
    redis: Redis | None = None,
) -> Container:
    """Wire services around a store and gateway (also used by tests with in-memory parts)."""
    git = GitClient(settings.git_author_name, settings.git_author_email)
    worktrees = WorktreeManager(git, settings.repos_root, settings.workspaces_root, locks)
    repositories = RepositoryResolver(settings.repos_root)
    return Container(
        settings=settings,
        store=store,
        gateway=gateway,
        service=WorkflowService(store, gateway, repositories, settings),
        execution=ExecutionService(store, git, worktrees, settings),
        repositories=repositories,
        git=git,
        worktrees=worktrees,
        registry=registry,
        mongo=mongo,
        redis=redis,
    )


async def build_container(settings: Settings) -> Container:
    mongo: AsyncMongoClient = AsyncMongoClient(settings.mongodb_uri, tz_aware=True)
    store = MongoWorkflowStore(mongo, settings.mongodb_database)
    await store.ensure_indexes()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    gateway, registry = build_gateway(settings)
    return assemble(
        settings,
        store,
        gateway,
        locks=Coordinator(redis).lock,
        registry=registry,
        mongo=mongo,
        redis=redis,
    )
