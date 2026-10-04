# Image for platform-api and orchestrator-worker.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app

# Dependencies first for layer caching.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY config ./config
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 10001 forgeflow
USER forgeflow

EXPOSE 8000
CMD ["uvicorn", "forgeflow.apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
