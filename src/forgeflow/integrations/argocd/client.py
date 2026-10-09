"""Argo CD plugin: read application state, sync and roll back (spec sections 43, 108, 158).

Argo CD is the deployment reconciler; ForgeFlow never applies Kubernetes resources
itself. Sync and rollback go through the capability gateway and need approval.
Authentication uses an Argo CD API token (account with `apiKey` capability, scoped
by Argo CD RBAC to the applications ForgeFlow may touch).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from forgeflow.core.errors import ForgeFlowError, ValidationFailed

_APP_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")


class ArgoCDError(ForgeFlowError):
    retryable = True


class ArgoCDAuthError(ArgoCDError):
    retryable = False


def validate_application(name: str) -> str:
    if not _APP_RE.match(name):
        raise ValidationFailed(f"invalid Argo CD application name: {name!r}")
    return name


@dataclass
class HistoryEntry:
    id: int
    revision: str
    deployed_at: str | None = None


@dataclass
class ApplicationState:
    name: str
    sync_status: str
    health_status: str
    revision: str | None
    operation_phase: str | None = None
    history: list[HistoryEntry] = field(default_factory=list)
    message: str | None = None

    @property
    def healthy(self) -> bool:
        return self.health_status == "Healthy" and self.sync_status == "Synced"


class ArgoCDClient:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        verify_tls: bool = True,
        timeout: float = 15,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.client = httpx.AsyncClient(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            verify=verify_tls,
            transport=transport,
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ArgoCDError(f"Argo CD unreachable: {type(exc).__name__}") from exc
        if response.status_code in (401, 403):
            raise ArgoCDAuthError(
                f"Argo CD rejected the token ({response.status_code}); check the account's RBAC"
            )
        if response.status_code == 404:
            raise ArgoCDError(f"not found: {path}")
        if response.status_code >= 400:
            raise ArgoCDError(f"Argo CD {response.status_code}: {response.text[:300]}")
        return response.json() if response.content else {}

    async def user(self) -> str:
        info = await self._request("GET", "/api/v1/session/userinfo")
        if not info.get("loggedIn"):
            raise ArgoCDAuthError("the token is not logged in")
        return str(info.get("username") or "unknown")

    async def version(self) -> str:
        return str((await self._request("GET", "/api/version")).get("Version", "unknown"))

    async def application(self, name: str) -> ApplicationState:
        data = await self._request("GET", f"/api/v1/applications/{validate_application(name)}")
        status = data.get("status") or {}
        history = [
            HistoryEntry(
                id=int(h.get("id", 0)),
                revision=str(h.get("revision", "")),
                deployed_at=h.get("deployedAt"),
            )
            for h in status.get("history") or []
        ]
        conditions = status.get("conditions") or []
        return ApplicationState(
            name=name,
            sync_status=(status.get("sync") or {}).get("status", "Unknown"),
            health_status=(status.get("health") or {}).get("status", "Unknown"),
            revision=(status.get("sync") or {}).get("revision"),
            operation_phase=(status.get("operationState") or {}).get("phase"),
            history=history,
            message=(conditions[0].get("message") if conditions else None)
            or (status.get("health") or {}).get("message"),
        )

    async def sync(self, name: str, revision: str | None = None) -> None:
        body: dict[str, Any] = {"prune": False}  # never delete resources from ForgeFlow
        if revision:
            body["revision"] = revision
        await self._request(
            "POST", f"/api/v1/applications/{validate_application(name)}/sync", json=body
        )

    async def rollback(self, name: str, history_id: int) -> None:
        await self._request(
            "POST",
            f"/api/v1/applications/{validate_application(name)}/rollback",
            json={"id": history_id, "prune": False},
        )

    async def close(self) -> None:
        await self.client.aclose()
