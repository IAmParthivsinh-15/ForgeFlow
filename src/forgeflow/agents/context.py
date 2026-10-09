"""Typed runtime context passed to every agent run (spec section 26).

Holds identifiers, paths chosen by ForgeFlow, and policy - never secrets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forgeflow.platform.a2a.channel import A2AChannel
from forgeflow.tools.filesystem.repository import RepositorySandbox
from forgeflow.tools.filesystem.workspace import WorkspaceFiles
from forgeflow.tools.shell.commands import CheckRunner


@dataclass
class AgentRuntimeContext:
    workflow_id: str
    task_id: str | None = None
    # Read root: the repository (analysis) or the task's worktree (implementation).
    repository_root: Path | None = None
    # Present only for code-writing tasks; writes are confined to file_scope.
    file_scope: list[str] = field(default_factory=list)
    checks: CheckRunner | None = None
    # Verification agents: the change under review and the A2A channel to the Developer.
    diff: str | None = None
    a2a: A2AChannel | None = None
    # Extensibility: resolved skills (prompt + reference files) and MCP proxy tools.
    skills_prompt: str = ""
    skill_files: dict[str, str] = field(default_factory=dict)
    extra_tools: list[Any] = field(default_factory=list)
    policies: dict[str, Any] = field(default_factory=dict)
    # Retrieval (KnowledgeService) scoped to this project's repository id.
    knowledge: Any = None
    repository_id: str | None = None
    # Browser QA: where ForgeFlow serves the commit under test (None = no app served).
    app_url: str | None = None
    # MCP calls made through capability proxies in this run (tool, status, artifacts).
    mcp_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    _workspace: WorkspaceFiles | None = field(default=None, repr=False)

    def sandbox(self) -> RepositorySandbox:
        if self.repository_root is None:
            raise LookupError("this workflow has no repository attached")
        return RepositorySandbox(self.repository_root)

    def workspace(self) -> WorkspaceFiles:
        if self.repository_root is None or not self.file_scope:
            raise LookupError("this agent has no writable workspace")
        if self._workspace is None:
            self._workspace = WorkspaceFiles(self.repository_root, self.file_scope)
        return self._workspace
