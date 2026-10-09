"""Environment-driven configuration.

Secrets (API keys) are read from the environment only. They are never stored on
workflow documents, written to logs, or placed into prompts.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    forgeflow_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    service_name: str = "forgeflow"

    mongodb_uri: str = "mongodb://localhost:27017/?replicaSet=rs0&directConnection=true"
    mongodb_database: str = "forgeflow"
    redis_url: str = "redis://localhost:6379/0"
    kafka_bootstrap_servers: str = "localhost:9094"

    # Root directory under which repositories may be analysed. A workflow's
    # repository_path must resolve inside this directory.
    repos_root: Path = Path("./repos")

    # Model routing configuration (provider endpoints + per-profile fallback chains).
    models_config_path: Path = Path("config/models.yaml")

    # When true, agents are replaced by a deterministic fake runner so the full
    # workflow can be exercised without any LLM provider or API key.
    fake_llm: bool = False

    # Requirement clarification limits.
    max_clarification_rounds: int = Field(default=3, ge=0)
    max_questions_per_round: int = Field(default=3, ge=1)

    agent_max_turns: int = Field(default=20, ge=1)
    # Agents SDK tracing uploads traces to OpenAI; requires OPENAI_API_KEY.
    sdk_tracing: bool = False
    outbox_poll_interval_seconds: float = 0.5
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:8080"]

    # --- Milestone 2: execution -------------------------------------------------------
    # Git worktrees for code-writing tasks are created under this directory.
    workspaces_root: Path = Path("./workspaces")
    # Upper bound on tasks of one workflow running at the same time (spec section 113).
    max_parallel_tasks: int = Field(default=3, ge=1)
    # Tasks one agent-worker process executes concurrently.
    agent_worker_concurrency: int = Field(default=3, ge=1)
    max_subtasks: int = Field(default=6, ge=1, le=20)
    task_max_attempts: int = Field(default=2, ge=1)
    task_retry_backoff_seconds: float = Field(default=10, ge=0)
    task_timeout_seconds: float = Field(default=1800, gt=0)
    # A DISPATCHED task not claimed within this time is re-dispatched.
    dispatch_timeout_seconds: float = Field(default=120, gt=0)
    heartbeat_interval_seconds: float = Field(default=15, gt=0)
    # A RUNNING task without a heartbeat for this long is treated as a lost worker.
    heartbeat_stale_seconds: float = Field(default=120, gt=0)
    reaper_interval_seconds: float = Field(default=15, gt=0)
    check_timeout_seconds: float = Field(default=600, gt=0)
    git_author_name: str = "ForgeFlow"
    git_author_email: str = "forgeflow@localhost"

    # --- Milestone 3: verification ------------------------------------------------------
    # Automatic fix rounds before the workflow waits for a human (spec section 57).
    max_repair_attempts: int = Field(default=2, ge=0)
    owasp_edition: str = "2021"
    semgrep_rules_path: Path = Path("config/semgrep")
    scanner_timeout_seconds: float = Field(default=300, gt=0)
    # A2A: bounded agent-to-agent questions (spec section 186).
    a2a_timeout_seconds: float = Field(default=180, gt=0)
    a2a_max_per_run: int = Field(default=3, ge=0)
    # Jenkins CI (spec section 56). Local instance from `docker compose --profile ci`.
    jenkins_url: str = "http://localhost:8081"
    # Address shown to people (links in the UI and report); defaults to jenkins_url.
    jenkins_public_url: str | None = None
    jenkins_user: str = "forgeflow"
    # Default only matches the local compose Jenkins; override via JENKINS_PASSWORD.
    jenkins_password: str = "forgeflow-local"  # noqa: S105
    # Path where Jenkins sees the repositories (its own mount of ./repos).
    jenkins_repos_root: str = "/repos"
    ci_poll_interval_seconds: float = Field(default=3, gt=0)
    ci_timeout_seconds: float = Field(default=1800, gt=0)

    # --- Extensibility (spec sections 200-255) ------------------------------------------
    # Single local user until authentication exists; every record still carries owner_id.
    local_user_id: str = "local"
    # Fernet key that encrypts connector/MCP credentials.
    # Generate one with: python -m forgeflow.scripts.generate_secret_key
    forgeflow_secret_key: str = ""
    approval_timeout_seconds: float = Field(default=1800, gt=0)
    approval_poll_seconds: float = Field(default=1.0, gt=0)
    github_api_url: str = "https://api.github.com"
    # Where verified branches are pushed; {repository} is owner/repo.
    github_push_url_template: str = "https://github.com/{repository}.git"
    # Only these stdio MCP server definitions may be started (spec section 207).
    mcp_stdio_allowlist_path: Path = Path("config/mcp_stdio_allowlist.yaml")
    mcp_timeout_seconds: float = Field(default=30, gt=0)
    skill_prompt_budget_chars: int = Field(default=12_000, ge=1000)
    # MongoDB Atlas free tier has 512 MB: published events and audit records expire.
    event_retention_days: int = Field(default=30, ge=1)
    audit_retention_days: int = Field(default=90, ge=1)

    # --- Knowledge: Elasticsearch / RAG (spec sections 36-37, 101) ----------------------
    # Empty = disabled. ForgeFlow keeps working when Elasticsearch is down (search is
    # reported as unavailable).
    elasticsearch_url: str = ""
    elasticsearch_index_prefix: str = "forgeflow"
    knowledge_timeout_seconds: float = Field(default=10, gt=0)
    # Optional embeddings (OpenAI-compatible /embeddings). Without them search is BM25.
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    max_index_file_bytes: int = Field(default=200_000, ge=1000)

    # --- Observability (spec sections 67-69, 102) --------------------------------------
    # Prometheus metrics port of the orchestrator worker (the API serves /metrics itself).
    metrics_port: int = Field(default=9101, ge=0)
    # Langfuse Cloud (or self-hosted) via OpenTelemetry. Tracing is off without both keys.
    langfuse_host: str = "https://cloud.langfuse.com"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    # Prompt/response text is not exported unless explicitly enabled.
    trace_include_content: bool = False

    # --- Browser QA: Playwright MCP (spec sections 28, 156, 266A) ----------------------
    # Ports the app under test is served on inside the agent worker, one per concurrent run.
    preview_ports: str = "4173-4175"
    # Host the browser uses to reach the app under test. Empty = the address this worker
    # uses to reach preview_peer_host (the Playwright MCP container).
    preview_host: str = ""
    preview_peer_host: str = "playwright-mcp"
    preview_start_timeout_seconds: float = Field(default=60, gt=0)
    mcp_presets_path: Path = Path("config/mcp_presets.yaml")
    builtin_skills_path: Path = Path("config/skills")
    # Screenshots and other evidence (spec section 155).
    artifacts_root: Path = Path("./artifacts")
    max_artifact_bytes: int = Field(default=5_000_000, ge=1000)

    # --- Deployments: Argo CD (spec sections 43, 108, 157) -----------------------------
    deployment_check_timeout_seconds: float = Field(default=15, gt=0)

    # --- L4 autonomy (additional.md) ----------------------------------------------------
    autonomy_contract_path: Path = Path("config/autonomy/contract.yaml")
    # Run the commander supervisor and the scheduled sweep in the orchestrator worker.
    autonomy_enabled: bool = True
    autonomy_tick_seconds: float = Field(default=5, gt=0)
    # GitHub webhook secret (X-Hub-Signature-256). Without it the webhook refuses events.
    github_webhook_secret: str = ""
    # Secret backend: "local" (Fernet-encrypted in MongoDB; development) or "vault"
    # (HashiCorp Vault KV v2; the token comes from the environment, never the database).
    secret_backend: Literal["local", "vault"] = "local"  # noqa: S105 - a backend name
    vault_addr: str = ""
    vault_token: str = ""
    vault_mount: str = "secret"
    vault_prefix: str = "forgeflow"


@lru_cache
def get_settings() -> Settings:
    return Settings()
