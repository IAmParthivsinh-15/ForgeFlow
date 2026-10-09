"""Composition root shared by the API and worker processes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agents import set_tracing_disabled
from dotenv import load_dotenv
from pymongo import AsyncMongoClient
from redis.asyncio import Redis

from forgeflow.core.config import Settings
from forgeflow.extensibility.facade import Extensibility, build_extensibility
from forgeflow.extensibility.store import (
    DocumentStore,
    InMemoryDocumentStore,
    MongoDocumentStore,
)
from forgeflow.knowledge.embeddings import build_embedder
from forgeflow.knowledge.index import ElasticsearchIndex, KnowledgeIndex
from forgeflow.knowledge.service import KnowledgeService
from forgeflow.models.config import ModelRegistry
from forgeflow.models.executor import AgentExecutor
from forgeflow.observability.tracing import setup_tracing
from forgeflow.platform.artifacts import ArtifactStore
from forgeflow.platform.ci.jenkins import CIProvider
from forgeflow.platform.deployments import DeploymentService
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
from forgeflow.tools.security.scanners import ScannerSuite


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
    # None = defaults from settings (Jenkins, bundled scanners); tests inject fakes.
    ci: CIProvider | None = None
    scanners: ScannerSuite | None = None
    extensibility: Extensibility | None = None
    knowledge: KnowledgeService | None = None
    artifacts: ArtifactStore | None = None
    deployments: DeploymentService | None = None
    autonomy: Any = None

    def executor(self, worker_id: str | None = None) -> TaskExecutor:
        return TaskExecutor(
            self.store,
            self.gateway,
            self.worktrees,
            self.git,
            self.settings,
            worker_id,
            ci=self.ci,
            scanners=self.scanners,
            extensibility=self.extensibility,
            knowledge=self.knowledge,
            artifacts=self.artifacts,
        )

    async def close(self) -> None:
        if self.knowledge is not None and self.knowledge.index is not None:
            await self.knowledge.index.close()
        if self.redis is not None:
            await self.redis.aclose()
        if self.mongo is not None:
            await self.mongo.close()


def load_environment() -> None:
    """Expose .env values (API keys, model ids) to os.environ for the model registry."""
    load_dotenv(override=False)


def build_gateway(
    settings: Settings, service: str = "agents"
) -> tuple[AgentGateway, ModelRegistry | None]:
    if settings.fake_llm:
        return FakeAgentGateway(), None
    # Langfuse (when keys are set) replaces the SDK's default exporter to OpenAI; without
    # it, SDK tracing to OpenAI stays off unless explicitly enabled.
    if not setup_tracing(settings, service):
        set_tracing_disabled(not settings.sdk_tracing)
    registry = ModelRegistry.from_path(settings.models_config_path)
    executor = AgentExecutor(
        registry,
        max_turns=settings.agent_max_turns,
        include_content=settings.trace_include_content,
    )
    return SdkAgentGateway(executor), registry


def build_knowledge(
    settings: Settings,
    documents: DocumentStore,
    git: GitClient,
    index: KnowledgeIndex | None = None,
    embedder: Any = None,
) -> KnowledgeService:
    if index is None and settings.elasticsearch_url:
        index = ElasticsearchIndex(
            settings.elasticsearch_url,
            settings.elasticsearch_index_prefix,
            settings.knowledge_timeout_seconds,
        )
    return KnowledgeService(
        index,
        documents,
        git,
        settings,
        embedder if embedder is not None else build_embedder(settings),
    )


def assemble(
    settings: Settings,
    store: WorkflowStore,
    gateway: AgentGateway,
    *,
    locks: LockFactory | None = None,
    documents: DocumentStore | None = None,
    registry: ModelRegistry | None = None,
    mongo: AsyncMongoClient | None = None,
    redis: Redis | None = None,
    knowledge_index: KnowledgeIndex | None = None,
) -> Container:
    """Wire services around a store and gateway (also used by tests with in-memory parts)."""
    git = GitClient(settings.git_author_name, settings.git_author_email)
    worktrees = WorktreeManager(git, settings.repos_root, settings.workspaces_root, locks)
    repositories = RepositoryResolver(settings.repos_root)
    documents = documents or InMemoryDocumentStore()
    artifacts = ArtifactStore(documents, settings.artifacts_root, settings.max_artifact_bytes)
    ext = build_extensibility(settings, documents, store, git, artifacts=artifacts)
    knowledge = build_knowledge(settings, documents, git, knowledge_index)
    container = Container(
        settings=settings,
        store=store,
        gateway=gateway,
        service=WorkflowService(store, gateway, repositories, settings, extensibility=ext),
        execution=ExecutionService(store, git, worktrees, settings, extensibility=ext),
        repositories=repositories,
        git=git,
        worktrees=worktrees,
        registry=registry,
        mongo=mongo,
        redis=redis,
        extensibility=ext,
        knowledge=knowledge,
        artifacts=artifacts,
        deployments=DeploymentService(ext, settings),
    )
    from forgeflow.autonomy.facade import build_autonomy

    container.autonomy = build_autonomy(container)
    return container


async def build_container(settings: Settings, service: str = "agents") -> Container:
    mongo: AsyncMongoClient = AsyncMongoClient(settings.mongodb_uri, tz_aware=True)
    store = MongoWorkflowStore(mongo, settings.mongodb_database)
    await store.ensure_indexes(settings.event_retention_days)
    documents = MongoDocumentStore(mongo, settings.mongodb_database)
    await documents.ensure_indexes(settings.audit_retention_days)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    gateway, registry = build_gateway(settings, service)
    return assemble(
        settings,
        store,
        gateway,
        locks=Coordinator(redis).lock,
        documents=documents,
        registry=registry,
        mongo=mongo,
        redis=redis,
    )
