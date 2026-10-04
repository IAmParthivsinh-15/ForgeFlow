"""Loads config/models.yaml and resolves each profile into a concrete fallback chain."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

StructuredOutputMode = Literal["json_schema", "prompt"]


class ProviderSpec(BaseModel):
    base_url: str
    base_url_env: str | None = None
    api_key_env: str | None = None
    enabled_env: str | None = None
    structured_output: StructuredOutputMode = "prompt"
    timeout_seconds: float = 120.0


class ProfileEntry(BaseModel):
    provider: str
    model: str | None = None
    model_env: str | None = None


class ModelsFile(BaseModel):
    providers: dict[str, ProviderSpec]
    profiles: dict[str, list[ProfileEntry]]


@dataclass(frozen=True)
class ResolvedModel:
    """One link in a fallback chain. Holds the key *value* only in memory."""

    provider: str
    model: str
    base_url: str
    api_key: str
    structured_output: StructuredOutputMode
    timeout_seconds: float

    def __repr__(self) -> str:  # never print the key
        return f"ResolvedModel(provider={self.provider!r}, model={self.model!r})"


class ModelRegistry:
    def __init__(self, models_file: ModelsFile, env: Mapping[str, str] | None = None) -> None:
        self._file = models_file
        self._env = env if env is not None else os.environ

    @classmethod
    def from_path(cls, path: Path, env: Mapping[str, str] | None = None) -> ModelRegistry:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls(ModelsFile.model_validate(data), env)

    def chain(self, profile: str) -> list[ResolvedModel]:
        """Ordered fallback chain for a profile, excluding unconfigured providers."""
        if profile not in self._file.profiles:
            raise KeyError(f"unknown model profile: {profile}")
        resolved: list[ResolvedModel] = []
        for entry in self._file.profiles[profile]:
            spec = self._file.providers.get(entry.provider)
            if spec is None:
                raise KeyError(f"profile {profile} references unknown provider {entry.provider}")
            if spec.enabled_env and self._env.get(spec.enabled_env, "").lower() not in {
                "1",
                "true",
                "yes",
            }:
                continue
            model = entry.model or (self._env.get(entry.model_env, "") if entry.model_env else "")
            if not model.strip():
                continue
            if spec.api_key_env:
                api_key = self._env.get(spec.api_key_env, "")
                if not api_key.strip():
                    continue
            else:
                api_key = "not-required"
            base_url = (self._env.get(spec.base_url_env, "") if spec.base_url_env else "") or (
                spec.base_url
            )
            resolved.append(
                ResolvedModel(
                    provider=entry.provider,
                    model=model.strip(),
                    base_url=base_url,
                    api_key=api_key.strip(),
                    structured_output=spec.structured_output,
                    timeout_seconds=spec.timeout_seconds,
                )
            )
        return resolved

    def describe(self) -> dict[str, list[str]]:
        """Non-secret summary of active chains, for health/diagnostics."""
        return {
            name: [f"{m.provider}:{m.model}" for m in self.chain(name)]
            for name in self._file.profiles
        }
