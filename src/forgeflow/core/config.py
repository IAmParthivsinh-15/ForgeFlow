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


@lru_cache
def get_settings() -> Settings:
    return Settings()
