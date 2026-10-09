"""Deployment verification and rollback (spec sections 43, 157, 158).

    Argo CD application status -> health endpoint -> smoke paths -> healthy / unhealthy
    unhealthy + an earlier deployed revision -> rollback offered (requires approval)

ForgeFlow never applies Kubernetes resources. It reads Argo CD (AUTO) and can ask it
to roll back (ASK, through the capability gateway, so a human approves first and the
action is audited).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from forgeflow.core.config import Settings
from forgeflow.core.errors import NotFoundError, ValidationFailed
from forgeflow.core.ids import new_id, utcnow
from forgeflow.core.logging import log_event
from forgeflow.extensibility.catalog import argocd_capabilities
from forgeflow.extensibility.gateway import CapabilityDenied, Invocation
from forgeflow.integrations.argocd.client import ApplicationState, ArgoCDError
from forgeflow.observability.metrics import DEPLOYMENT_FAILURES, DEPLOYMENTS
from forgeflow.schemas.operations import DeploymentCheck, DeploymentProbe

logger = logging.getLogger(__name__)
HttpFactory = Callable[[], httpx.AsyncClient]


def validate_health_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValidationFailed("the health URL must be http(s)")
    if parts.username or parts.password:
        raise ValidationFailed("the health URL must not contain credentials")
    return url


class DeploymentService:
    def __init__(
        self, ext: Any, settings: Settings, http_factory: HttpFactory | None = None
    ) -> None:
        self.ext = ext
        self.settings = settings
        self.http_factory = http_factory or (
            lambda: httpx.AsyncClient(
                timeout=settings.deployment_check_timeout_seconds, follow_redirects=False
            )
        )
        self._background: set[asyncio.Task] = set()

    async def _context(self, owner_id: str, project_id: str):
        project = await self.ext.projects.get(owner_id, project_id)
        binding = project.deployment
        if binding is None:
            raise ValidationFailed("the project has no deployment binding (Argo CD application)")
        connector = await self.ext.connectors.get(owner_id, binding.connector_id)
        if connector.type != "argocd":
            raise ValidationFailed("the deployment connector is not an Argo CD connector")
        if binding.application not in connector.repositories:
            raise ValidationFailed(
                f"connector '{connector.name}' is not allowed to access {binding.application}"
            )
        caps = {c.capability_id: c for c in argocd_capabilities(connector.connector_id, owner_id)}
        inv = Invocation(
            owner_id=owner_id, workflow_id=None, task_id=None, agent="deployer", project=project
        )

        async def status() -> str:
            return await self.ext.connectors.status(connector.connector_id)

        return project, binding, connector, caps, inv, status

    # ------------------------------------------------------------------ verify

    async def verify(self, owner_id: str, project_id: str) -> DeploymentCheck:
        project, binding, connector, caps, inv, status = await self._context(owner_id, project_id)
        probes: list[DeploymentProbe] = []
        state: ApplicationState | None = None
        client = await self.ext.connectors.argocd_client(connector)
        started = time.perf_counter()
        try:
            state = await self.ext.gateway.invoke(
                inv,
                caps["deployment.status"],
                lambda: client.application(binding.application),
                summary=f"Read Argo CD application {binding.application}",
                source_status=status,
            )
            probes.append(
                DeploymentProbe(
                    name="argocd",
                    target=binding.application,
                    passed=state.healthy,
                    detail=f"sync={state.sync_status} health={state.health_status}"
                    + (f" ({state.message})" if state.message else ""),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            )
        except (ArgoCDError, CapabilityDenied) as exc:
            probes.append(
                DeploymentProbe(
                    name="argocd", target=binding.application, passed=False, detail=str(exc)
                )
            )
        finally:
            await client.close()

        if binding.health_url:
            targets = [("health", binding.health_url)] + [
                (f"smoke {path}", urljoin(binding.health_url, path)) for path in binding.smoke_paths
            ]
            async with self.http_factory() as http:
                for name, url in targets:
                    probes.append(await self._probe(http, name, url, binding.expected_status))

        healthy = bool(probes) and all(p.passed for p in probes)
        unknown = state is None and not binding.health_url
        previous = None
        if state and len(state.history) >= 2:
            previous = state.history[-2].id
        now = utcnow()
        check = DeploymentCheck(
            check_id=new_id("dep"),
            owner_id=owner_id,
            project_id=project.project_id,
            application=binding.application,
            status="unknown" if unknown else ("healthy" if healthy else "unhealthy"),
            sync_status=state.sync_status if state else None,
            health_status=state.health_status if state else None,
            revision=state.revision if state else None,
            rollback_to=previous if not healthy else None,
            probes=probes,
            summary=", ".join(f"{p.name}: {'ok' if p.passed else 'FAILED'}" for p in probes),
            rollback="available" if (not healthy and previous is not None) else "not_needed",
            created_at=now,
            updated_at=now,
        )
        await self.ext.store.put("deployment_checks", check)
        DEPLOYMENTS.labels(binding.application, check.status).inc()
        if check.status == "unhealthy":
            DEPLOYMENT_FAILURES.labels(binding.application).inc()
        log_event(
            logger, "deployment checked", application=binding.application, status=check.status
        )
        return check

    async def _probe(
        self, http: httpx.AsyncClient, name: str, url: str, expected: int
    ) -> DeploymentProbe:
        started = time.perf_counter()
        try:
            response = await http.get(validate_health_url(url))
        except (httpx.HTTPError, ValidationFailed) as exc:
            return DeploymentProbe(name=name, target=url, passed=False, detail=type(exc).__name__)
        return DeploymentProbe(
            name=name,
            target=url,
            passed=response.status_code == expected,
            detail=f"HTTP {response.status_code}",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    # ---------------------------------------------------------------- rollback

    async def request_rollback(self, owner_id: str, check_id: str) -> DeploymentCheck:
        """Ask for approval and roll back in the background (the approval may take a while)."""
        check = await self.get(owner_id, check_id)
        if check.rollback != "available" or check.rollback_to is None:
            raise ValidationFailed(f"no rollback is available for this check ({check.rollback})")
        claimed = await self.ext.store.compare_and_set(
            "deployment_checks",
            check_id,
            {"rollback": "available"},
            {"rollback": "requested", "updated_at": utcnow()},
        )
        if not claimed:
            raise ValidationFailed("a rollback was already requested for this check")
        task = asyncio.create_task(self._rollback(owner_id, check))
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return await self.get(owner_id, check_id)

    async def _rollback(self, owner_id: str, check: DeploymentCheck) -> None:
        outcome, detail = "failed", None
        try:
            _, binding, connector, caps, inv, status = await self._context(
                owner_id, check.project_id
            )
            client = await self.ext.connectors.argocd_client(connector)
            try:
                await self.ext.gateway.invoke(
                    inv,
                    caps["deployment.rollback"],
                    lambda: client.rollback(binding.application, int(check.rollback_to or 0)),
                    summary=(
                        f"Roll back Argo CD application {binding.application} to history "
                        f"entry {check.rollback_to} (currently {check.revision or 'unknown'})"
                    ),
                    details={
                        "application": binding.application,
                        "history_id": check.rollback_to,
                        "current_revision": check.revision,
                        "probes": [p.model_dump() for p in check.probes],
                    },
                    source_status=status,
                )
                outcome, detail = "done", f"rolled back to history entry {check.rollback_to}"
            finally:
                await client.close()
        except CapabilityDenied as exc:
            outcome, detail = "rejected", str(exc)
        except Exception as exc:  # recorded on the check; the user can request again
            detail = f"{type(exc).__name__}: {exc}"[:300]
        await self.ext.store.compare_and_set(
            "deployment_checks",
            check.check_id,
            {"rollback": "requested"},
            {"rollback": outcome, "rollback_detail": detail, "updated_at": utcnow()},
        )
        log_event(logger, "deployment rollback finished", outcome=outcome, check_id=check.check_id)

    async def recover_interrupted(self) -> int:
        """Rollbacks still 'requested' when the API restarted lost their waiter."""
        stale = await self.ext.store.find("deployment_checks", {"rollback": "requested"})
        for doc in stale:
            await self.ext.store.compare_and_set(
                "deployment_checks",
                doc["check_id"],
                {"rollback": "requested"},
                {
                    "rollback": "available",
                    "rollback_detail": "interrupted by a restart; request it again",
                    "updated_at": utcnow(),
                },
            )
        return len(stale)

    # ------------------------------------------------------------------- reads

    async def get(self, owner_id: str, check_id: str) -> DeploymentCheck:
        doc = await self.ext.store.get("deployment_checks", check_id)
        if doc is None or doc["owner_id"] != owner_id:
            raise NotFoundError(f"deployment check {check_id} not found")
        return DeploymentCheck.model_validate(doc)

    async def list_checks(
        self, owner_id: str, project_id: str | None = None
    ) -> list[DeploymentCheck]:
        query: dict[str, Any] = {"owner_id": owner_id}
        if project_id:
            query["project_id"] = project_id
        docs = await self.ext.store.find("deployment_checks", query, sort="-created_at", limit=50)
        return [DeploymentCheck.model_validate(d) for d in docs]
