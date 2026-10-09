"""Extensibility gateway facade (spec section 202): one object wiring the control plane."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from forgeflow.core.config import Settings
from forgeflow.extensibility.approvals import ApprovalService
from forgeflow.extensibility.connectors import ArgoCDFactory, ConnectorService, GitHubFactory
from forgeflow.extensibility.gateway import CapabilityGateway
from forgeflow.extensibility.mcp.service import MCPService
from forgeflow.extensibility.projects import ProjectService
from forgeflow.extensibility.resolver import CapabilityResolver
from forgeflow.extensibility.runtime import CapabilityRuntime
from forgeflow.extensibility.secrets import SecretStore
from forgeflow.extensibility.skills.service import SkillService
from forgeflow.extensibility.store import DocumentStore
from forgeflow.platform.state.store import WorkflowStore
from forgeflow.tools.git.client import GitClient


@dataclass
class Extensibility:
    store: DocumentStore
    secrets: SecretStore
    approvals: ApprovalService
    gateway: CapabilityGateway
    connectors: ConnectorService
    projects: ProjectService
    skills: SkillService
    mcp: MCPService
    resolver: CapabilityResolver
    runtime: CapabilityRuntime


def build_extensibility(
    settings: Settings,
    store: DocumentStore,
    workflows: WorkflowStore | None,
    git: GitClient,
    github_factory: GitHubFactory | None = None,
    artifacts: Any = None,
    argocd_factory: ArgoCDFactory | None = None,
) -> Extensibility:
    backend = None
    if settings.secret_backend == "vault":  # noqa: S105 - a backend name
        from forgeflow.extensibility.secrets import VaultBackend

        backend = VaultBackend(
            settings.vault_addr, settings.vault_token, settings.vault_mount, settings.vault_prefix
        )
    secrets = SecretStore(store, settings.forgeflow_secret_key, backend)
    approvals = ApprovalService(
        store, workflows, settings.approval_timeout_seconds, settings.approval_poll_seconds
    )
    gateway = CapabilityGateway(store, approvals)
    mcp = MCPService(
        store,
        secrets,
        settings.mcp_stdio_allowlist_path,
        settings.mcp_timeout_seconds,
        presets_path=settings.mcp_presets_path,
    )
    resolver = CapabilityResolver(store, mcp, settings.skill_prompt_budget_chars)
    return Extensibility(
        store=store,
        secrets=secrets,
        approvals=approvals,
        gateway=gateway,
        connectors=ConnectorService(
            store, secrets, settings.github_api_url, github_factory, argocd_factory
        ),
        projects=ProjectService(store, settings.repos_root, git),
        skills=SkillService(store),
        mcp=mcp,
        resolver=resolver,
        runtime=CapabilityRuntime(resolver, mcp, gateway, artifacts),
    )
