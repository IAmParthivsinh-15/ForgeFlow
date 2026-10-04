from __future__ import annotations

import asyncio

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from forgeflow.apps.api.deps import ContainerDep
from forgeflow.platform.events.kafka import kafka_ping

router = APIRouter(tags=["system"])

CHECK_TIMEOUT_SECONDS = 3.0


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health")
async def health(c: ContainerDep) -> JSONResponse:
    async def check(coro) -> str:
        try:
            await asyncio.wait_for(coro, CHECK_TIMEOUT_SECONDS)
            return "ok"
        except Exception as exc:  # report, never raise, from a health check
            return f"error: {type(exc).__name__}"

    checks = {"mongodb": await check(c.store.ping())}
    if c.redis is not None:
        checks["redis"] = await check(c.redis.ping())
    checks["kafka"] = await check(kafka_ping(c.settings.kafka_bootstrap_servers))
    healthy = all(v == "ok" for v in checks.values())
    body = {
        "status": "ok" if healthy else "degraded",
        "checks": checks,
        "llm": "fake" if c.settings.fake_llm else "configured",
        "model_chains": c.registry.describe() if c.registry else {},
    }
    return JSONResponse(body, status_code=200 if healthy else 503)


@router.get("/api/v1/repositories")
async def list_repositories(c: ContainerDep) -> list[str]:
    return c.repositories.list()
