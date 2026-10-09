"""Platform API (FastAPI). Run: uvicorn forgeflow.apps.api.main:app"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from forgeflow.apps.api.routes import (
    autonomy,
    extensibility,
    operations,
    questions,
    system,
    tasks,
    workflows,
)
from forgeflow.apps.container import Container, build_container, load_environment
from forgeflow.core.config import get_settings
from forgeflow.core.errors import (
    ConcurrencyConflict,
    ForgeFlowError,
    InvalidStateTransition,
    NotFoundError,
    PolicyViolation,
    ValidationFailed,
)
from forgeflow.core.logging import configure_logging, log_event
from forgeflow.extensibility.gateway import CapabilityDenied
from forgeflow.extensibility.secrets import SecretStoreError
from forgeflow.observability.metrics import API_LATENCY

logger = logging.getLogger("forgeflow.api")

_STATUS: list[tuple[type[ForgeFlowError], int]] = [
    (NotFoundError, 404),
    (ValidationFailed, 422),
    (PolicyViolation, 400),
    (ConcurrencyConflict, 409),
    (InvalidStateTransition, 409),
    (SecretStoreError, 400),
    (CapabilityDenied, 403),
]


def create_app(container_factory: Callable[[], Awaitable[Container]] | None = None) -> FastAPI:
    load_environment()
    settings = get_settings()
    configure_logging("platform-api", settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        factory = container_factory or (lambda: build_container(settings, "platform-api"))
        app.state.container = container = await factory()
        await _startup(container)
        try:
            yield
        finally:
            await app.state.container.close()

    app = FastAPI(title="ForgeFlow Platform API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(ForgeFlowError)
    async def _forgeflow_error(_: Request, exc: ForgeFlowError) -> JSONResponse:
        status = next((code for cls, code in _STATUS if isinstance(exc, cls)), 500)
        return JSONResponse({"detail": str(exc), "error": type(exc).__name__}, status_code=status)

    app.include_router(system.router)
    app.include_router(workflows.router)
    app.include_router(questions.router)
    app.include_router(tasks.router)
    app.include_router(extensibility.router)
    app.include_router(operations.router)
    app.include_router(autonomy.router)

    @app.middleware("http")
    async def _latency(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        path = getattr(route, "path", None) or "unmatched"
        if not path.endswith("/stream"):  # SSE connections stay open by design
            API_LATENCY.labels(request.method, path, str(response.status_code)).observe(
                time.perf_counter() - started
            )
        return response

    @app.get("/metrics", include_in_schema=False)
    async def _metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


async def _startup(container: Container) -> None:
    """Seed built-in skills and recover rollbacks interrupted by a restart."""
    try:
        if container.extensibility is not None:
            await container.extensibility.skills.ensure_builtin(
                container.settings.builtin_skills_path
            )
        if container.deployments is not None:
            await container.deployments.recover_interrupted()
    except Exception as exc:  # startup chores must not keep the API down
        log_event(logger, "startup task failed", logging.WARNING, error=str(exc)[:300])


app = create_app()
