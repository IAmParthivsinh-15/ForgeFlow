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
from forgeflow.platform.orchestration.fake_gateway import FakeAgentGateway
from forgeflow.platform.orchestration.gateway import AgentGateway, SdkAgentGateway
from forgeflow.platform.orchestration.repositories import RepositoryResolver
from forgeflow.platform.orchestration.service import WorkflowService
from forgeflow.platform.state.mongo import MongoWorkflowStore
from forgeflow.platform.state.store import WorkflowStore


@dataclass
class Container:
    settings: Settings
    store: WorkflowStore
    service: WorkflowService
    repositories: RepositoryResolver
    registry: ModelRegistry | None = None
    mongo: AsyncMongoClient | None = None
    redis: Redis | None = None

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


async def build_container(settings: Settings) -> Container:
    mongo: AsyncMongoClient = AsyncMongoClient(settings.mongodb_uri, tz_aware=True)
    store = MongoWorkflowStore(mongo, settings.mongodb_database)
    await store.ensure_indexes()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    gateway, registry = build_gateway(settings)
    repositories = RepositoryResolver(settings.repos_root)
    service = WorkflowService(store, gateway, repositories, settings)
    return Container(settings, store, service, repositories, registry, mongo, redis)
