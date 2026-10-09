"""Connector registry (spec sections 204, 205, 254).

    CONFIGURE (token stored encrypted) -> VALIDATE (GitHub API) -> ACTIVE
    ACTIVE <-> DISABLED (configuration kept) ; REVOKED (credential destroyed) ; deleted

A connector is scoped to explicit resources (GitHub repositories, Argo CD
applications); using it for anything else is refused.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlsplit

from forgeflow.core.errors import NotFoundError, ValidationFailed
from forgeflow.core.ids import new_id, utcnow
from forgeflow.extensibility.secrets import SecretStore
from forgeflow.extensibility.store import DocumentStore
from forgeflow.integrations.argocd.client import (
    ArgoCDClient,
    ArgoCDError,
    validate_application,
)
from forgeflow.integrations.github.client import (
    GitHubAuthError,
    GitHubClient,
    GitHubError,
    validate_repository,
)
from forgeflow.schemas.extensibility import Connector

GitHubFactory = Callable[[str, str], GitHubClient]
# (url, token, verify_tls) -> client
ArgoCDFactory = Callable[[str, str, bool], ArgoCDClient]


class ConnectorService:
    def __init__(
        self,
        store: DocumentStore,
        secrets: SecretStore,
        github_api_url: str,
        github_factory: GitHubFactory | None = None,
        argocd_factory: ArgoCDFactory | None = None,
    ) -> None:
        self.store = store
        self.secrets = secrets
        self.github_api_url = github_api_url
        self.github_factory = github_factory or (lambda token, url: GitHubClient(token, url))
        self.argocd_factory = argocd_factory or (
            lambda url, token, verify: ArgoCDClient(url, token, verify_tls=verify)
        )

    async def create_argocd(
        self,
        owner_id: str,
        name: str,
        url: str,
        token: str,
        applications: list[str],
        verify_tls: bool = True,
    ) -> Connector:
        token = token.strip()
        if len(token) < 20:
            raise ValidationFailed("the token looks too short")
        parts = urlsplit(url.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username:
            raise ValidationFailed(
                "the Argo CD URL must be http(s)://host[:port] without credentials"
            )
        apps = sorted({validate_application(a.strip()) for a in applications if a.strip()})
        if not apps:
            raise ValidationFailed(
                "list the Argo CD applications this connector may use (least privilege)"
            )
        now = utcnow()
        connector_id = new_id("con")
        connector = Connector(
            connector_id=connector_id,
            owner_id=owner_id,
            type="argocd",
            name=name.strip()[:80] or "Argo CD",
            status="configured",
            config={"url": url.strip().rstrip("/"), "verify_tls": verify_tls},
            repositories=apps,  # the resources this connector is scoped to
            credential_ref=f"secret://connector/{connector_id}",
            created_at=now,
            updated_at=now,
        )
        await self.secrets.put(connector.credential_ref or "", token)
        await self.store.put("connectors", connector)
        return await self.test(owner_id, connector_id)

    async def argocd_client(self, connector: Connector) -> ArgoCDClient:
        if connector.type != "argocd" or not connector.credential_ref:
            raise ValidationFailed(f"connector {connector.name} is not a usable Argo CD connector")
        token = await self.secrets.get(connector.credential_ref)
        return self.argocd_factory(
            connector.config["url"], token, bool(connector.config.get("verify_tls", True))
        )

    async def _test_argocd(self, connector: Connector) -> None:
        client = await self.argocd_client(connector)
        try:
            connector.account = await client.user()
            scopes = []
            for app in connector.repositories:
                await client.application(app)
                scopes.append(f"{app}:read")
            connector.scopes = scopes
            if connector.status in ("configured", "error"):
                connector.status = "active"
            connector.last_error = None
        except ArgoCDError as exc:
            connector.status = "error"
            connector.last_error = str(exc)[:500]
        finally:
            await client.close()

    async def create_github(
        self,
        owner_id: str,
        name: str,
        token: str,
        repositories: list[str],
        api_url: str | None = None,
    ) -> Connector:
        token = token.strip()
        if len(token) < 20:
            raise ValidationFailed("the token looks too short")
        repos = sorted({validate_repository(r.strip()) for r in repositories if r.strip()})
        if not repos:
            raise ValidationFailed(
                "list the repositories this connector may use (owner/repo); "
                "least privilege is required (spec section 254)"
            )
        now = utcnow()
        connector_id = new_id("con")
        connector = Connector(
            connector_id=connector_id,
            owner_id=owner_id,
            type="github",
            name=name.strip()[:80] or "GitHub",
            status="configured",
            config={"api_url": (api_url or self.github_api_url).rstrip("/")},
            repositories=repos,
            credential_ref=f"secret://connector/{connector_id}",
            created_at=now,
            updated_at=now,
        )
        await self.secrets.put(connector.credential_ref or "", token)
        await self.store.put("connectors", connector)
        return await self.test(owner_id, connector_id)

    async def test(self, owner_id: str, connector_id: str) -> Connector:
        connector = await self.get(owner_id, connector_id)
        if connector.status == "revoked" or not connector.credential_ref:
            raise ValidationFailed("a revoked connector cannot be tested; reconnect it")
        if connector.type == "argocd":
            await self._test_argocd(connector)
            connector.last_tested_at = connector.updated_at = utcnow()
            await self.store.put("connectors", connector)
            return connector
        client = await self.client(connector)
        scopes: list[str] = []
        try:
            connector.account = await client.get_user()
            for repo in connector.repositories:
                info = await client.get_repository(repo)
                perms = info.get("permissions") or {}
                scopes.append(f"{repo}:{'write' if perms.get('push') else 'read'}")
                if not perms.get("push"):
                    raise GitHubAuthError(
                        f"the token cannot push to {repo}; grant Contents: Read and write "
                        "and Pull requests: Read and write"
                    )
            if connector.status in ("configured", "error"):
                connector.status = "active"
            connector.last_error = None
        except GitHubError as exc:
            connector.status = "error"
            connector.last_error = str(exc)[:500]
        connector.scopes = scopes
        connector.last_tested_at = connector.updated_at = utcnow()
        await self.store.put("connectors", connector)
        return connector

    async def set_enabled(self, owner_id: str, connector_id: str, enabled: bool) -> Connector:
        connector = await self.get(owner_id, connector_id)
        if connector.status == "revoked":
            raise ValidationFailed("a revoked connector cannot be enabled; reconnect it")
        connector.status = "active" if enabled else "disabled"
        connector.updated_at = utcnow()
        await self.store.put("connectors", connector)
        return connector

    async def revoke(self, owner_id: str, connector_id: str) -> Connector:
        """Destroy the credential; future calls are blocked immediately (spec section 245)."""
        connector = await self.get(owner_id, connector_id)
        if connector.credential_ref:
            await self.secrets.delete(connector.credential_ref)
        connector.status = "revoked"
        connector.credential_ref = None
        connector.updated_at = utcnow()
        await self.store.put("connectors", connector)
        return connector

    async def delete(self, owner_id: str, connector_id: str) -> None:
        connector = await self.get(owner_id, connector_id)
        if connector.credential_ref:
            await self.secrets.delete(connector.credential_ref)
        await self.store.delete("connectors", connector_id)

    async def get(self, owner_id: str, connector_id: str) -> Connector:
        doc = await self.store.get("connectors", connector_id)
        if doc is None or doc["owner_id"] != owner_id:  # never reveal others' records
            raise NotFoundError(f"connector {connector_id} not found")
        return Connector.model_validate(doc)

    async def list_connectors(self, owner_id: str) -> list[Connector]:
        docs = await self.store.find("connectors", {"owner_id": owner_id}, sort="created_at")
        return [Connector.model_validate(d) for d in docs]

    async def status(self, connector_id: str) -> str:
        doc = await self.store.get("connectors", connector_id)
        return doc["status"] if doc else "deleted"

    async def client(self, connector: Connector) -> GitHubClient:
        if connector.type != "github":
            raise ValidationFailed(f"connector {connector.name} is not a GitHub connector")
        if not connector.credential_ref:
            raise ValidationFailed(f"connector {connector.name} has no credential")
        token = await self.secrets.get(connector.credential_ref)
        return self.github_factory(token, connector.config.get("api_url", self.github_api_url))

    async def token(self, connector: Connector) -> str:
        if not connector.credential_ref:
            raise ValidationFailed(f"connector {connector.name} has no credential")
        return await self.secrets.get(connector.credential_ref)
