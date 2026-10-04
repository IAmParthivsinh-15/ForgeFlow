"""Platform API (FastAPI). Run: uvicorn forgeflow.apps.api.main:app"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from forgeflow.apps.api.routes import questions, system, workflows
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
from forgeflow.core.logging import configure_logging

_STATUS: list[tuple[type[ForgeFlowError], int]] = [
    (NotFoundError, 404),
    (ValidationFailed, 422),
    (PolicyViolation, 400),
    (ConcurrencyConflict, 409),
    (InvalidStateTransition, 409),
]


def create_app(container_factory: Callable[[], Awaitable[Container]] | None = None) -> FastAPI:
    load_environment()
    settings = get_settings()
    configure_logging("platform-api", settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        factory = container_factory or (lambda: build_container(settings))
        app.state.container = await factory()
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
    return app


app = create_app()
