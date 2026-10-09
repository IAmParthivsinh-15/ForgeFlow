"""Extensibility API (spec sections 61, 248): connectors, MCP, skills, projects, approvals, audit.

Credentials are write-only: they are accepted on create and never returned, not even
as references. Every query is scoped to the current user server-side (spec 240).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, File, UploadFile
from pydantic import BaseModel, Field

from forgeflow.apps.api.deps import ContainerDep
from forgeflow.core.errors import ValidationFailed
from forgeflow.extensibility.catalog import (
    argocd_capabilities,
    github_capabilities,
    native_capabilities,
)
from forgeflow.extensibility.facade import Extensibility
from forgeflow.extensibility.mcp.service import tool_capability
from forgeflow.extensibility.skills.validation import MAX_PACKAGE_BYTES
from forgeflow.schemas.extensibility import (
    Approval,
    Capability,
    CapabilityAudit,
    CapabilityManifest,
    Connector,
    MCPServerConfig,
    MCPToolInfo,
    Policy,
    Project,
    Skill,
    SkillInstallation,
    SkillMetadata,
    SkillVersion,
)

router = APIRouter(prefix="/api/v1", tags=["extensibility"])


def _ext(c: Any) -> Extensibility:
    if c.extensibility is None:
        raise ValidationFailed("the extensibility gateway is not configured")
    return c.extensibility


def _owner(c: Any) -> str:
    return c.settings.local_user_id


# ----------------------------------------------------------------- connectors


class ConnectorOut(BaseModel):
    connector: Connector
    has_credential: bool


def _connector_out(conn: Connector) -> ConnectorOut:
    return ConnectorOut(
        connector=conn.model_copy(update={"credential_ref": None}),
        has_credential=bool(conn.credential_ref),
    )


class CreateConnector(BaseModel):
    type: Literal["github", "argocd"] = "github"
    name: str = "GitHub"
    token: str = Field(min_length=20, description="GitHub fine-grained PAT or Argo CD API token")
    repositories: list[str] = Field(
        default_factory=list, description="GitHub: owner/repo the token may use"
    )
    api_url: str | None = None
    # Argo CD
    url: str | None = Field(default=None, description="Argo CD server URL")
    applications: list[str] = Field(default_factory=list, description="Argo CD applications")
    verify_tls: bool = True


@router.get("/connectors")
async def list_connectors(c: ContainerDep) -> list[ConnectorOut]:
    return [_connector_out(x) for x in await _ext(c).connectors.list_connectors(_owner(c))]


@router.post("/connectors", status_code=201)
async def create_connector(body: CreateConnector, c: ContainerDep) -> ConnectorOut:
    if body.type == "argocd":
        if not body.url:
            raise ValidationFailed("an Argo CD connector needs the server URL")
        conn = await _ext(c).connectors.create_argocd(
            _owner(c), body.name, body.url, body.token, body.applications, body.verify_tls
        )
    else:
        conn = await _ext(c).connectors.create_github(
            _owner(c), body.name, body.token, body.repositories, body.api_url
        )
    return _connector_out(conn)


@router.get("/connectors/{connector_id}")
async def get_connector(connector_id: str, c: ContainerDep) -> ConnectorOut:
    return _connector_out(await _ext(c).connectors.get(_owner(c), connector_id))


@router.post("/connectors/{connector_id}/test")
async def test_connector(connector_id: str, c: ContainerDep) -> ConnectorOut:
    return _connector_out(await _ext(c).connectors.test(_owner(c), connector_id))


@router.post("/connectors/{connector_id}/disable")
async def disable_connector(connector_id: str, c: ContainerDep) -> ConnectorOut:
    return _connector_out(await _ext(c).connectors.set_enabled(_owner(c), connector_id, False))


@router.post("/connectors/{connector_id}/enable")
async def enable_connector(connector_id: str, c: ContainerDep) -> ConnectorOut:
    return _connector_out(await _ext(c).connectors.set_enabled(_owner(c), connector_id, True))


@router.post("/connectors/{connector_id}/revoke")
async def revoke_connector(connector_id: str, c: ContainerDep) -> ConnectorOut:
    return _connector_out(await _ext(c).connectors.revoke(_owner(c), connector_id))


@router.delete("/connectors/{connector_id}", status_code=204)
async def delete_connector(connector_id: str, c: ContainerDep) -> None:
    await _ext(c).connectors.delete(_owner(c), connector_id)


# ------------------------------------------------------------------------ MCP


class MCPOut(BaseModel):
    server: MCPServerConfig
    has_credential: bool
    tools: list[MCPToolInfo] = Field(default_factory=list)


async def _mcp_out(ext: Extensibility, owner: str, server: MCPServerConfig) -> MCPOut:
    return MCPOut(
        server=server.model_copy(update={"credential_ref": None}),
        has_credential=bool(server.credential_ref),
        tools=await ext.mcp.tools(owner, server.mcp_id),
    )


class CreateMCP(BaseModel):
    name: str
    description: str = ""
    transport: Literal["stdio", "streamable_http", "sse"]
    url: str | None = None
    stdio_server: str | None = None
    token: str | None = None
    auth_header: str = "Authorization"
    approval_policy: Literal["auto_read_ask_write", "ask_all", "auto_all"] = "auto_read_ask_write"
    allowed_agents: list[str] = Field(default_factory=list)
    trusted: bool = False


class UpdateTool(BaseModel):
    enabled: bool | None = None
    policy: Policy | None = None
    clear_policy: bool = False


@router.get("/mcps")
async def list_mcps(c: ContainerDep) -> list[MCPOut]:
    ext = _ext(c)
    return [await _mcp_out(ext, _owner(c), s) for s in await ext.mcp.list_servers(_owner(c))]


@router.get("/mcps/allowlist")
async def mcp_allowlist(c: ContainerDep) -> dict[str, str]:
    """stdio servers that may be registered (spec section 207)."""
    return {k: str(v.get("description", "")) for k, v in _ext(c).mcp.allowlist().items()}


class PresetOut(BaseModel):
    key: str
    name: str
    description: str
    transport: str
    url: str | None = None
    allowed_agents: list[str] = Field(default_factory=list)
    skill: str | None = None
    denied_tools: list[str] = Field(default_factory=list)


class EnablePreset(BaseModel):
    project_id: str | None = Field(
        default=None, description="Also enable the server (and its skill) for this project"
    )


@router.get("/mcps/presets")
async def mcp_presets(c: ContainerDep) -> list[PresetOut]:
    """Admin-reviewed MCP registrations (config/mcp_presets.yaml)."""
    return [
        PresetOut(
            key=key,
            name=str(p.get("name") or key),
            description=str(p.get("description") or "").strip(),
            transport=str(p.get("transport")),
            url=p.get("url"),
            allowed_agents=list(p.get("allowed_agents") or []),
            skill=p.get("skill"),
            denied_tools=[t for t, v in (p.get("tool_policies") or {}).items() if v == "deny"],
        )
        for key, p in _ext(c).mcp.presets().items()
    ]


@router.post("/mcps/presets/{key}", status_code=201)
async def enable_mcp_preset(key: str, body: EnablePreset, c: ContainerDep) -> MCPOut:
    """Register the preset's server; optionally enable it and its skill for a project."""
    ext, owner = _ext(c), _owner(c)
    preset = ext.mcp.presets().get(key)
    if preset is None:
        raise ValidationFailed(f"unknown MCP preset '{key}'")
    server = await ext.mcp.register_preset(owner, key)
    if body.project_id:
        project = await ext.projects.get(owner, body.project_id)
        await ext.projects.update(
            owner,
            project.project_id,
            enabled_mcp_ids=[*project.enabled_mcp_ids, server.mcp_id],
        )
        if preset.get("skill"):
            skill = await ext.skills.find_builtin(str(preset["skill"]))
            if skill is not None:
                await ext.skills.enable(
                    owner,
                    skill.skill_id,
                    project_id=project.project_id,
                    agents=list(preset.get("allowed_agents") or []),
                )
    return await _mcp_out(ext, owner, server)


@router.post("/mcps", status_code=201)
async def create_mcp(body: CreateMCP, c: ContainerDep) -> MCPOut:
    ext = _ext(c)
    server = await ext.mcp.register(_owner(c), **body.model_dump())
    return await _mcp_out(ext, _owner(c), server)


@router.get("/mcps/{mcp_id}")
async def get_mcp(mcp_id: str, c: ContainerDep) -> MCPOut:
    ext = _ext(c)
    return await _mcp_out(ext, _owner(c), await ext.mcp.get(_owner(c), mcp_id))


@router.post("/mcps/{mcp_id}/test")
@router.post("/mcps/{mcp_id}/refresh-tools")
async def refresh_mcp(mcp_id: str, c: ContainerDep) -> MCPOut:
    ext = _ext(c)
    return await _mcp_out(ext, _owner(c), await ext.mcp.refresh_tools(_owner(c), mcp_id))


@router.post("/mcps/{mcp_id}/disable")
async def disable_mcp(mcp_id: str, c: ContainerDep) -> MCPOut:
    ext = _ext(c)
    return await _mcp_out(ext, _owner(c), await ext.mcp.set_enabled(_owner(c), mcp_id, False))


@router.post("/mcps/{mcp_id}/enable")
async def enable_mcp(mcp_id: str, c: ContainerDep) -> MCPOut:
    ext = _ext(c)
    return await _mcp_out(ext, _owner(c), await ext.mcp.set_enabled(_owner(c), mcp_id, True))


@router.post("/mcps/{mcp_id}/revoke")
async def revoke_mcp(mcp_id: str, c: ContainerDep) -> MCPOut:
    ext = _ext(c)
    return await _mcp_out(ext, _owner(c), await ext.mcp.revoke(_owner(c), mcp_id))


@router.delete("/mcps/{mcp_id}", status_code=204)
async def delete_mcp(mcp_id: str, c: ContainerDep) -> None:
    await _ext(c).mcp.delete(_owner(c), mcp_id)


@router.patch("/mcps/{mcp_id}/tools/{tool_name}")
async def update_mcp_tool(
    mcp_id: str, tool_name: str, body: UpdateTool, c: ContainerDep
) -> MCPToolInfo:
    return await _ext(c).mcp.update_tool(
        _owner(c),
        mcp_id,
        tool_name,
        enabled=body.enabled,
        policy_override=body.policy,
        clear_override=body.clear_policy,
    )


# --------------------------------------------------------------------- skills


class SkillDetail(BaseModel):
    skill: Skill
    versions: list[SkillVersion]
    installations: list[SkillInstallation]
    owned: bool


class CreateSkill(BaseModel):
    metadata: SkillMetadata
    instructions: str = Field(min_length=1)
    files: dict[str, str] = Field(default_factory=dict)


class EnableSkill(BaseModel):
    version: str | None = None
    project_id: str | None = None
    agents: list[str] = Field(default_factory=list)


class DisableSkill(BaseModel):
    project_id: str | None = None


class SkillStatus(BaseModel):
    status: Literal["active", "deprecated", "archived"]


class ForkSkill(BaseModel):
    version: str | None = None


async def _skill_detail(ext: Extensibility, owner: str, skill_id: str) -> SkillDetail:
    skill = await ext.skills.get(owner, skill_id)
    installs = [i for i in await ext.skills.installations(owner) if i.skill_id == skill_id]
    return SkillDetail(
        skill=skill,
        versions=await ext.skills.versions(owner, skill_id),
        installations=installs,
        owned=skill.owner_id == owner,
    )


@router.get("/skills")
async def list_skills(
    c: ContainerDep, scope: Literal["all", "mine", "public"] = "all", q: str = ""
) -> list[Skill]:
    return await _ext(c).skills.search(_owner(c), scope, q)


@router.post("/skills", status_code=201)
async def create_skill(body: CreateSkill, c: ContainerDep) -> SkillDetail:
    ext = _ext(c)
    skill, _ = await ext.skills.create(_owner(c), body.metadata, body.instructions, body.files)
    return await _skill_detail(ext, _owner(c), skill.skill_id)


@router.post("/skills/upload", status_code=201)
async def upload_skill(c: ContainerDep, file: Annotated[UploadFile, File()]) -> SkillDetail:
    data = await file.read(MAX_PACKAGE_BYTES + 1)
    ext = _ext(c)
    skill, _ = await ext.skills.upload(_owner(c), data)
    return await _skill_detail(ext, _owner(c), skill.skill_id)


@router.get("/skills/installations")
async def skill_installations(c: ContainerDep) -> list[SkillInstallation]:
    return await _ext(c).skills.installations(_owner(c))


@router.get("/skills/{skill_id}")
async def get_skill(skill_id: str, c: ContainerDep) -> SkillDetail:
    return await _skill_detail(_ext(c), _owner(c), skill_id)


@router.post("/skills/{skill_id}/versions", status_code=201)
async def add_skill_version(skill_id: str, body: CreateSkill, c: ContainerDep) -> SkillVersion:
    return await _ext(c).skills.add_version(
        _owner(c), skill_id, body.metadata, body.instructions, body.files
    )


@router.post("/skills/{skill_id}/publish")
async def publish_skill(skill_id: str, c: ContainerDep) -> Skill:
    return await _ext(c).skills.publish(_owner(c), skill_id)


@router.post("/skills/{skill_id}/status")
async def set_skill_status(skill_id: str, body: SkillStatus, c: ContainerDep) -> Skill:
    return await _ext(c).skills.set_status(_owner(c), skill_id, body.status)


@router.post("/skills/{skill_id}/fork", status_code=201)
async def fork_skill(skill_id: str, body: ForkSkill, c: ContainerDep) -> Skill:
    return await _ext(c).skills.fork(_owner(c), skill_id, body.version)


@router.post("/skills/{skill_id}/enable")
async def enable_skill(skill_id: str, body: EnableSkill, c: ContainerDep) -> SkillInstallation:
    return await _ext(c).skills.enable(
        _owner(c), skill_id, body.version, body.project_id, body.agents
    )


@router.post("/skills/{skill_id}/disable", status_code=204)
async def disable_skill(skill_id: str, body: DisableSkill, c: ContainerDep) -> None:
    await _ext(c).skills.disable(_owner(c), skill_id, body.project_id)


# ------------------------------------------------------------------- projects


class ProjectOut(BaseModel):
    project: Project
    detected_github_repository: str | None = None


class UpdateProject(BaseModel):
    github: dict[str, Any] | None = None
    clear_github: bool = False
    enabled_mcp_ids: list[str] | None = None
    capability_policies: dict[str, Policy] | None = None
    deployment: dict[str, Any] | None = None
    clear_deployment: bool = False
    browser_allowed_origins: list[str] | None = None


@router.get("/projects")
async def list_projects(c: ContainerDep) -> list[Project]:
    return await _ext(c).projects.list_projects(_owner(c), c.repositories.list())


@router.get("/projects/{project_id}")
async def get_project(project_id: str, c: ContainerDep) -> ProjectOut:
    ext = _ext(c)
    project = await ext.projects.get(_owner(c), project_id)
    return ProjectOut(
        project=project,
        detected_github_repository=await ext.projects.detected_github_repository(project),
    )


@router.patch("/projects/{project_id}")
async def update_project(project_id: str, body: UpdateProject, c: ContainerDep) -> Project:
    return await _ext(c).projects.update(
        _owner(c),
        project_id,
        github=body.github,
        clear_github=body.clear_github,
        enabled_mcp_ids=body.enabled_mcp_ids,
        capability_policies=body.capability_policies,
        deployment=body.deployment,
        clear_deployment=body.clear_deployment,
        browser_allowed_origins=body.browser_allowed_origins,
    )


# ------------------------------------------------------------------ approvals


class Decision(BaseModel):
    note: str | None = None


@router.get("/approvals")
async def list_approvals(
    c: ContainerDep, status: str | None = None, workflow_id: str | None = None
) -> list[Approval]:
    return await _ext(c).approvals.list_approvals(_owner(c), status, workflow_id)


@router.post("/approvals/{approval_id}/approve")
async def approve(approval_id: str, body: Decision, c: ContainerDep) -> Approval:
    return await _ext(c).approvals.decide(approval_id, True, _owner(c), body.note)


@router.post("/approvals/{approval_id}/reject")
async def reject(approval_id: str, body: Decision, c: ContainerDep) -> Approval:
    return await _ext(c).approvals.decide(approval_id, False, _owner(c), body.note)


# ------------------------------------------------------------ capabilities/audit


@router.get("/capabilities")
async def list_capabilities(c: ContainerDep) -> list[Capability]:
    """Unified registry view (spec section 223)."""
    ext, owner = _ext(c), _owner(c)
    caps = native_capabilities()
    for conn in await ext.connectors.list_connectors(owner):
        listed = (argocd_capabilities if conn.type == "argocd" else github_capabilities)(
            conn.connector_id, owner
        )
        for cap in listed:
            caps.append(cap.model_copy(update={"status": conn.status}))
    for server in await ext.mcp.list_servers(owner):
        caps += [tool_capability(server, t) for t in await ext.mcp.tools(owner, server.mcp_id)]
    for skill in await ext.skills.search(owner, "all"):
        caps.append(
            Capability(
                capability_id=f"skill.{skill.skill_id}",
                type="skill",
                name=skill.name,
                description=skill.description,
                source_id=skill.skill_id,
                owner_id=skill.owner_id,
                visibility=skill.visibility,
                version=skill.latest_version,
                status=skill.status,
            )
        )
    return caps


@router.get("/capabilities/available")
async def available_capabilities(
    project_id: str, agent: str, c: ContainerDep, context: str = ""
) -> CapabilityManifest:
    """What would this agent receive in this project? (spec section 224)"""
    ext = _ext(c)
    project = await ext.projects.get(_owner(c), project_id)
    resolution = await ext.resolver.resolve(
        owner_id=_owner(c), workflow_id="preview", project=project, agent=agent, context=context
    )
    return resolution.manifest


@router.get("/audit/capabilities")
async def capability_audit(
    c: ContainerDep, workflow_id: str | None = None, limit: int = 200
) -> list[CapabilityAudit]:
    query: dict[str, Any] = {"owner_id": _owner(c)}
    if workflow_id:
        query["workflow_id"] = workflow_id
    docs = await _ext(c).store.find(
        "capability_audit", query, sort="-timestamp", limit=min(limit, 1000)
    )
    return [CapabilityAudit.model_validate(d) for d in docs]


@router.get("/workflows/{workflow_id}/capabilities")
async def workflow_capabilities(workflow_id: str, c: ContainerDep) -> dict[str, Any]:
    """Snapshot taken at execution start and the manifest each task actually used."""
    wf = await c.store.get_workflow(workflow_id)
    docs = await _ext(c).store.find("capability_manifests", {"workflow_id": workflow_id})
    return {
        "snapshot": wf.capability_snapshot.model_dump(mode="json")
        if wf.capability_snapshot
        else None,
        "task_manifests": docs,
    }
