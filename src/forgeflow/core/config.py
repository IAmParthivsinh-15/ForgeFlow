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


@lru_cache
def get_settings() -> Settings:
    return Settings()
