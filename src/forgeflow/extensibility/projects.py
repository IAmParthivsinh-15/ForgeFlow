"""Projects: a repository plus the capabilities bound to it (spec section 241)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from forgeflow.core.errors import NotFoundError, ValidationFailed
from forgeflow.core.ids import utcnow
from forgeflow.extensibility.store import DocumentStore
from forgeflow.integrations.github.client import repository_from_remote, validate_repository
from forgeflow.schemas.extensibility import DeploymentBinding, GitHubBinding, Policy, Project
from forgeflow.tools.git.client import GitClient


def project_id_for(repository_path: str) -> str:
    return "proj_" + re.sub(r"[^a-z0-9_-]+", "-", repository_path.lower()).strip("-")[:60]


class ProjectService:
    def __init__(self, store: DocumentStore, repos_root: Path, git: GitClient) -> None:
        self.store = store
        self.repos_root = repos_root
        self.git = git

    async def ensure(self, owner_id: str, repository_path: str) -> Project:
        project_id = project_id_for(repository_path)
        doc = await self.store.get("projects", project_id)
        if doc is not None:
            return Project.model_validate(doc)
        now = utcnow()
        project = Project(
            project_id=project_id,
            owner_id=owner_id,
            name=repository_path,
            repository_path=repository_path,
            created_at=now,
            updated_at=now,
        )
        await self.store.put("projects", project)
        return project

    async def get(self, owner_id: str, project_id: str) -> Project:
        doc = await self.store.get("projects", project_id)
        if doc is None or doc["owner_id"] != owner_id:
            raise NotFoundError(f"project {project_id} not found")
        return Project.model_validate(doc)

    async def find_for_repository(self, repository_path: str | None) -> Project | None:
        if not repository_path:
            return None
        doc = await self.store.get("projects", project_id_for(repository_path))
        return Project.model_validate(doc) if doc else None

    async def list_projects(self, owner_id: str, repositories: list[str]) -> list[Project]:
        """Every repository under REPOS_ROOT is a project; create records on first sight."""
        return [await self.ensure(owner_id, repo) for repo in repositories]

    async def detected_github_repository(self, project: Project) -> str | None:
        repo = self.repos_root / project.repository_path
        res = await self.git.run(repo, "remote", "get-url", "origin", check=False)
        return repository_from_remote(res.stdout) if res.code == 0 else None

    async def update(
        self,
        owner_id: str,
        project_id: str,
        *,
        github: dict[str, Any] | None = None,
        clear_github: bool = False,
        enabled_mcp_ids: list[str] | None = None,
        capability_policies: dict[str, Policy] | None = None,
        deployment: dict[str, Any] | None = None,
        clear_deployment: bool = False,
        browser_allowed_origins: list[str] | None = None,
    ) -> Project:
        project = await self.get(owner_id, project_id)
        if clear_deployment:
            project.deployment = None
        elif deployment is not None:
            target = DeploymentBinding.model_validate(deployment)
            connector = await self.store.get("connectors", target.connector_id)
            if connector is None or connector["owner_id"] != owner_id:
                raise ValidationFailed("unknown connector")
            if connector["type"] != "argocd":
                raise ValidationFailed("a deployment binding needs an Argo CD connector")
            if target.application not in (connector.get("repositories") or []):
                raise ValidationFailed(
                    f"connector '{connector['name']}' is not allowed to access {target.application}"
                )
            if target.health_url:
                parts = urlsplit(target.health_url)
                if parts.scheme not in ("http", "https") or parts.username or parts.password:
                    raise ValidationFailed("the health URL must be http(s) without credentials")
            for path in target.smoke_paths:
                if not path.startswith("/"):
                    raise ValidationFailed(f"smoke paths must start with '/': {path}")
            project.deployment = target
        if browser_allowed_origins is not None:
            origins = []
            for raw in browser_allowed_origins:
                parts = urlsplit(raw.strip())
                if (
                    parts.scheme not in ("http", "https")
                    or not parts.hostname
                    or parts.path not in ("", "/")
                ):
                    raise ValidationFailed(f"an origin looks like https://host[:port], not {raw!r}")
                origins.append(f"{parts.scheme}://{parts.netloc}")
            project.browser_allowed_origins = list(dict.fromkeys(origins))
        if clear_github:
            project.github = None
        elif github is not None:
            binding = GitHubBinding.model_validate(github)
            validate_repository(binding.repository)
            connector = await self.store.get("connectors", binding.connector_id)
            if connector is None or connector["owner_id"] != owner_id:
                raise ValidationFailed("unknown connector")
            allowed = connector.get("repositories") or []
            if allowed and binding.repository not in allowed:
                raise ValidationFailed(
                    f"connector '{connector['name']}' is not allowed to access "
                    f"{binding.repository} (allowed: {', '.join(allowed)})"
                )
            project.github = binding
        if enabled_mcp_ids is not None:
            for mcp_id in enabled_mcp_ids:
                doc = await self.store.get("mcp_servers", mcp_id)
                if doc is None or doc["owner_id"] != owner_id:
                    raise ValidationFailed(f"unknown MCP server {mcp_id}")
            project.enabled_mcp_ids = list(dict.fromkeys(enabled_mcp_ids))
        if capability_policies is not None:
            project.capability_policies = dict(capability_policies)
        project.updated_at = utcnow()
        await self.store.put("projects", project)
        return project
