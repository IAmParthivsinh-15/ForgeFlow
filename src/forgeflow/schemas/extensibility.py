"""Extensibility contracts: connectors, projects, MCP, skills, approvals, audit, manifests.

Spec sections 200-255. Records carry metadata and *references* to credentials, never
the credentials themselves.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Permission = Literal[
    "READ", "WRITE", "EXECUTE", "NETWORK", "SECRET_ACCESS", "DEPLOY", "DESTRUCTIVE"
]
Policy = Literal["auto", "ask", "deny"]
Risk = Literal["low", "medium", "high", "critical"]
Visibility = Literal["private", "public"]

# Agents that can receive extensibility capabilities.
AGENT_TYPES = (
    "orchestrator",
    "requirement_analyzer",
    "developer",
    "developer_subagent",
    "integrator",
    "code_review",
    "security",
    "qa",
    "ci",
)


# ------------------------------------------------------------------ capabilities


class Capability(BaseModel):
    """Unified view over native tools, connector operations, MCP tools and skills (sec. 223)."""

    capability_id: str
    type: Literal["native_tool", "connector", "mcp_tool", "skill"]
    name: str
    description: str = ""
    source_id: str | None = None
    owner_id: str | None = None
    visibility: Visibility = "private"
    version: str | None = None
    risk: Risk = "low"
    permissions: list[Permission] = Field(default_factory=list)
    default_policy: Policy = "auto"
    status: str = "active"
    allowed_agents: list[str] = Field(default_factory=list, description="Empty = all agents.")


class ManifestSkill(BaseModel):
    skill_id: str
    slug: str
    name: str
    version: str
    checksum: str
    level: Literal["metadata", "summary", "full"]


class ManifestTool(BaseModel):
    capability_id: str
    name: str
    source_id: str
    policy: Policy


class CapabilityManifest(BaseModel):
    """Deterministic, reproducible capability set for one agent in one workflow (sec. 243)."""

    workflow_id: str
    agent: str
    project_id: str | None
    skills: list[ManifestSkill] = Field(default_factory=list)
    mcp_tools: list[ManifestTool] = Field(default_factory=list)
    connector_capabilities: list[ManifestTool] = Field(default_factory=list)
    native_tools: list[str] = Field(default_factory=list)
    unavailable: list[str] = Field(default_factory=list, description="Excluded, with reason.")
    hash: str = ""


class CapabilitySnapshot(BaseModel):
    """Saved at execution start so the workflow can be reconstructed later (sec. 244)."""

    resolved_at: datetime
    manifests: dict[str, CapabilityManifest]
    mcp_config_hashes: dict[str, str] = Field(default_factory=dict)
    connector_scopes: dict[str, list[str]] = Field(default_factory=dict)


# --------------------------------------------------------------------- connectors


class Connector(BaseModel):
    connector_id: str
    owner_id: str
    type: Literal["github", "argocd"]
    name: str
    status: Literal["configured", "active", "disabled", "revoked", "error"]
    config: dict[str, Any] = Field(default_factory=dict)
    # Repositories this connector may touch (owner/repo). Empty = any the token can access.
    repositories: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)
    account: str | None = None
    credential_ref: str | None = None
    last_tested_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime


class GitHubBinding(BaseModel):
    connector_id: str
    repository: str = Field(description="owner/repo on GitHub")
    base_branch: str | None = Field(default=None, description="Defaults to the execution base ref.")
    auto_pull_request: bool = True
    draft: bool = False


class DeploymentBinding(BaseModel):
    """Where the project is deployed and how to tell it is healthy (spec sections 43, 157)."""

    connector_id: str = Field(description="An Argo CD connector")
    application: str = Field(description="Argo CD application name")
    health_url: str | None = Field(default=None, description="GET must return expected_status")
    smoke_paths: list[str] = Field(
        default_factory=list, description="Extra paths under health_url's origin to GET"
    )
    expected_status: int = 200


class Project(BaseModel):
    """A repository and the capabilities bound to it (spec section 241)."""

    project_id: str
    owner_id: str
    name: str
    repository_path: str
    github: GitHubBinding | None = None
    deployment: DeploymentBinding | None = None
    # Extra origins the browser may open besides the app under test (e.g. a login page).
    browser_allowed_origins: list[str] = Field(default_factory=list)
    enabled_mcp_ids: list[str] = Field(default_factory=list)
    capability_policies: dict[str, Policy] = Field(
        default_factory=dict, description="Per-project overrides by capability_id."
    )
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------- MCP


class MCPServerConfig(BaseModel):
    mcp_id: str
    owner_id: str
    name: str
    description: str = ""
    transport: Literal["stdio", "streamable_http", "sse"]
    url: str | None = None
    stdio_server: str | None = Field(default=None, description="Key in the stdio allowlist.")
    credential_ref: str | None = None
    auth_header: str = "Authorization"
    status: Literal["registered", "active", "disabled", "revoked", "error"]
    trusted: bool = False
    approval_policy: Literal["auto_read_ask_write", "ask_all", "auto_all"] = "auto_read_ask_write"
    allowed_agents: list[str] = Field(default_factory=list)
    visibility: Visibility = "private"
    # Registered from config/mcp_presets.yaml (e.g. "playwright").
    preset: str | None = None
    # tool name -> argument holding a URL; such calls may only open allowed origins.
    url_guard: dict[str, str] = Field(default_factory=dict)
    config_hash: str = ""
    tools_refreshed_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime


class MCPToolInfo(BaseModel):
    tool_id: str
    mcp_id: str
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    operation: Literal["read", "write", "destructive"]
    risk: Risk
    policy_override: Policy | None = None
    enabled: bool = True


# ------------------------------------------------------------------------- skills


class SkillDependencies(BaseModel):
    skills: list[str] = Field(default_factory=list)
    mcp: list[str] = Field(default_factory=list, description="MCP server names")
    connectors: list[str] = Field(default_factory=list, description="Connector types")


class SkillMetadata(BaseModel):
    schema_version: str = "1"
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str = Field(min_length=1, max_length=1000)
    when_to_use: str = ""
    license: str = "proprietary"
    allowed_agents: list[str] = Field(default_factory=list, description="Empty = all agents.")
    tags: list[str] = Field(default_factory=list)
    endpoints: list[str] = Field(
        default_factory=list, description="External URLs the skill may reference."
    )
    dependencies: SkillDependencies = Field(default_factory=SkillDependencies)


class ValidationReport(BaseModel):
    passed: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class Skill(BaseModel):
    skill_id: str
    owner_id: str
    slug: str
    name: str
    description: str
    tags: list[str] = Field(default_factory=list)
    visibility: Visibility = "private"
    status: Literal["draft", "active", "published", "deprecated", "archived"]
    trust: Literal["unverified", "community", "verified"] = "unverified"
    latest_version: str
    forked_from: str | None = None
    usage_count: int = 0
    created_at: datetime
    updated_at: datetime


class SkillVersion(BaseModel):
    """Immutable once created (spec section 219)."""

    skill_version_id: str
    skill_id: str
    version: str
    metadata: SkillMetadata
    instructions: str = Field(description="skill.md")
    files: dict[str, str] = Field(default_factory=dict, description="references/examples")
    checksum: str
    size_bytes: int
    validation: ValidationReport
    created_at: datetime


class SkillInstallation(BaseModel):
    """A user enabling a skill version, optionally for one project (spec section 222)."""

    installation_id: str
    skill_id: str
    version: str
    user_id: str
    project_id: str | None = None
    enabled: bool = True
    enabled_agents: list[str] = Field(default_factory=list, description="Empty = metadata's.")
    installed_at: datetime


# ---------------------------------------------------------------- approvals/audit


class Approval(BaseModel):
    """'May we perform this action?' - distinct from requirement clarification (sec. 237)."""

    approval_id: str
    owner_id: str
    workflow_id: str | None
    task_id: str | None
    agent: str
    capability_id: str
    action: str
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)
    risk: Risk
    status: Literal["pending", "approved", "rejected", "expired"]
    requested_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    note: str | None = None


class CapabilityAudit(BaseModel):
    """Spec section 238."""

    audit_id: str
    timestamp: datetime
    owner_id: str
    workflow_id: str | None
    task_id: str | None
    agent: str
    capability_id: str
    capability_type: str
    source_id: str | None = None
    skill_version: str | None = None
    action: str
    approval: Literal[
        "auto", "user_approved", "user_rejected", "denied_by_policy", "expired", "not_required"
    ]
    result: Literal["success", "error", "denied"]
    latency_ms: int = 0
    error: str | None = None
