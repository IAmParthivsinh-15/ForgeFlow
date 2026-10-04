"""Typed runtime context passed to every agent run (spec section 26).

Holds identifiers and policy only - never secrets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forgeflow.tools.filesystem.repository import RepositorySandbox


@dataclass
class AgentRuntimeContext:
    workflow_id: str
    task_id: str | None = None
    repository_root: Path | None = None
    policies: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    def sandbox(self) -> RepositorySandbox:
        if self.repository_root is None:
            raise LookupError("this workflow has no repository attached")
        return RepositorySandbox(self.repository_root)
