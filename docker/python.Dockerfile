# Image for platform-api, orchestrator-worker and agent-worker.
FROM python:3.12-slim

# git: worktrees, commits, merges. nodejs/npm: allowlisted checks for JavaScript repos.
# Repositories are bind-mounted and owned by another uid, so git must trust them explicitly.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git nodejs npm ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory '*'

# Security scanners (spec section 14), isolated from ForgeFlow's own environment.
# Semgrep runs offline against the rules in config/semgrep (--metrics=off).
ARG GITLEAKS_VERSION=8.21.2
RUN curl -sSfL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" \
        | tar -xz -C /usr/local/bin gitleaks \
    && python -m venv /opt/scanners \
    && /opt/scanners/bin/pip install --no-cache-dir bandit semgrep pip-audit

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONUNBUFFERED=1 \
    SEMGREP_SEND_METRICS=off \
    PATH=/opt/venv/bin:/opt/scanners/bin:$PATH

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
