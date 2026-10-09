"""Capability resolver (spec sections 203, 224, 234, 242-244).

    candidates -> user access -> project enabled -> agent allowed -> dependencies
               -> risk policy -> version resolution -> deterministic manifest

A manifest taken at execution start ("snapshot") pins skill versions; later runs
of the same workflow reuse those exact versions, while revocations still apply.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from forgeflow.extensibility.catalog import NATIVE_TOOLS_BY_AGENT, github_capabilities
from forgeflow.extensibility.mcp.service import MCPService, tool_capability
from forgeflow.extensibility.policy import decide
from forgeflow.extensibility.skills.selection import Candidate, choose_levels, manifest_entry
from forgeflow.extensibility.store import DocumentStore
from forgeflow.schemas.extensibility import (
    CapabilityManifest,
    ManifestTool,
    MCPServerConfig,
    MCPToolInfo,
    Project,
    Skill,
    SkillInstallation,
    SkillVersion,
)


@dataclass
class ResolvedTool:
    server: MCPServerConfig
    tool: MCPToolInfo


@dataclass
class Resolution:
    manifest: CapabilityManifest
    skills: list[tuple[Candidate, Any]] = field(default_factory=list)
    tools: list[ResolvedTool] = field(default_factory=list)


def manifest_hash(manifest: CapabilityManifest) -> str:
    payload = manifest.model_dump(mode="json", exclude={"hash"})
    return "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class CapabilityResolver:
    def __init__(self, store: DocumentStore, mcp: MCPService, skill_budget_chars: int) -> None:
        self.store = store
        self.mcp = mcp
        self.skill_budget_chars = skill_budget_chars

    async def resolve(
        self,
        *,
        owner_id: str,
        workflow_id: str,
        project: Project | None,
        agent: str,
        context: str,
        pinned: CapabilityManifest | None = None,
    ) -> Resolution:
        unavailable: list[str] = []
        candidates = await self._skill_candidates(owner_id, project, agent, pinned, unavailable)
        if pinned is not None:
            levels: dict[str, str] = {s.skill_id: s.level for s in pinned.skills}
            chosen: list[tuple[Candidate, Any]] = [
                (c, levels[c.skill.skill_id]) for c in candidates
            ]
        else:
            chosen = choose_levels(candidates, agent, context, self.skill_budget_chars)
        tools = await self._mcp_tools(project, agent, unavailable)
        if pinned is not None:
            allowed = {t.capability_id for t in pinned.mcp_tools}
            tools = [t for t in tools if f"mcp.{t.server.mcp_id}.{t.tool.name}" in allowed]

        connector_caps: list[ManifestTool] = []
        if project and project.github:
            doc = await self.store.get("connectors", project.github.connector_id)
            if doc and doc["status"] == "active":
                for cap in github_capabilities(doc["connector_id"], owner_id):
                    connector_caps.append(
                        ManifestTool(
                            capability_id=cap.capability_id,
                            name=cap.name,
                            source_id=cap.source_id or "",
                            policy=decide(cap, agent, project).policy,
                        )
                    )
            else:
                unavailable.append("github: connector is not active")

        manifest = CapabilityManifest(
            workflow_id=workflow_id,
            agent=agent,
            project_id=project.project_id if project else None,
            skills=[manifest_entry(c, level) for c, level in chosen],
            mcp_tools=[
                ManifestTool(
                    capability_id=f"mcp.{t.server.mcp_id}.{t.tool.name}",
                    name=f"{t.server.name}.{t.tool.name}",
                    source_id=t.server.mcp_id,
                    policy=decide(
                        tool_capability(t.server, t.tool),
                        agent,
                        project,
                        tool_override=t.tool.policy_override,
                    ).policy,
                )
                for t in tools
            ],
            connector_capabilities=connector_caps,
            native_tools=list(NATIVE_TOOLS_BY_AGENT.get(agent, [])),
            unavailable=unavailable,
        )
        manifest.hash = manifest_hash(manifest)
        return Resolution(manifest=manifest, skills=chosen, tools=tools)

    async def _skill_candidates(
        self,
        owner_id: str,
        project: Project | None,
        agent: str,
        pinned: CapabilityManifest | None,
        unavailable: list[str],
    ) -> list[Candidate]:
        if pinned is not None:
            result = []
            for entry in pinned.skills:
                skill_doc = await self.store.get("skills", entry.skill_id)
                version_doc = await self.store.get(
                    "skill_versions", f"{entry.skill_id}@{entry.version}"
                )
                if skill_doc and version_doc:
                    result.append(
                        Candidate(
                            Skill.model_validate(skill_doc),
                            SkillVersion.model_validate(version_doc),
                        )
                    )
                else:
                    unavailable.append(f"skill {entry.slug}@{entry.version}: no longer exists")
            return result

        docs = await self.store.find("skill_installations", {"user_id": owner_id})
        installs = [SkillInstallation.model_validate(d) for d in docs]
        project_id = project.project_id if project else None
        # A project-specific installation overrides a global one for the same skill.
        chosen: dict[str, SkillInstallation] = {}
        for inst in sorted(installs, key=lambda i: i.project_id is not None):
            if inst.project_id in (None, project_id):
                chosen[inst.skill_id] = inst

        candidates: list[Candidate] = []
        for inst in chosen.values():
            if not inst.enabled:
                continue
            skill_doc = await self.store.get("skills", inst.skill_id)
            version_doc = await self.store.get("skill_versions", f"{inst.skill_id}@{inst.version}")
            if not skill_doc or not version_doc:
                continue
            skill, version = (
                Skill.model_validate(skill_doc),
                SkillVersion.model_validate(version_doc),
            )
            visible = skill.owner_id == owner_id or skill.visibility == "public"
            if not visible or skill.status == "archived":
                unavailable.append(f"skill {skill.slug}: not available ({skill.status})")
                continue
            agents = inst.enabled_agents or version.metadata.allowed_agents
            if agents and agent not in agents:
                continue
            missing = await self._missing_dependencies(version, project, installs)
            if missing:
                unavailable.append(f"skill {skill.slug}@{version.version}: missing {missing}")
                continue
            candidates.append(Candidate(skill, version))
        return sorted(candidates, key=lambda c: c.skill.slug)

    async def _missing_dependencies(
        self, version: SkillVersion, project: Project | None, installs: list[SkillInstallation]
    ) -> list[str]:
        deps = version.metadata.dependencies
        missing: list[str] = []
        for connector_type in deps.connectors:
            if connector_type == "github":
                ok = bool(project and project.github)
                if ok and project and project.github:
                    doc = await self.store.get("connectors", project.github.connector_id)
                    ok = bool(doc and doc["status"] == "active")
                if not ok:
                    missing.append("connector:github")
            else:
                missing.append(f"connector:{connector_type}")
        if deps.mcp:
            names = set()
            for mcp_id in project.enabled_mcp_ids if project else []:
                doc = await self.store.get("mcp_servers", mcp_id)
                if doc and doc["status"] == "active":
                    names.add(doc["name"].lower())
            missing += [f"mcp:{m}" for m in deps.mcp if m.lower() not in names]
        installed_slugs = set()
        for inst in installs:
            doc = await self.store.get("skills", inst.skill_id) if inst.enabled else None
            if doc:
                installed_slugs.add(doc["slug"])
        missing += [f"skill:{s}" for s in deps.skills if s.split("@")[0] not in installed_slugs]
        return missing

    async def _mcp_tools(
        self, project: Project | None, agent: str, unavailable: list[str]
    ) -> list[ResolvedTool]:
        tools: list[ResolvedTool] = []
        for mcp_id in project.enabled_mcp_ids if project else []:
            doc = await self.store.get("mcp_servers", mcp_id)
            if doc is None:
                continue
            server = MCPServerConfig.model_validate(doc)
            if server.status != "active":
                unavailable.append(f"mcp {server.name}: {server.status}")
                continue
            if server.allowed_agents and agent not in server.allowed_agents:
                continue
            for tool in await self.mcp.tools(server.owner_id, mcp_id):
                if not tool.enabled:
                    continue
                cap = tool_capability(server, tool)
                decision = decide(cap, agent, project, tool_override=tool.policy_override)
                if decision.policy == "deny":
                    unavailable.append(f"mcp {server.name}.{tool.name}: {decision.reason}")
                    continue
                tools.append(ResolvedTool(server, tool))
        return sorted(tools, key=lambda t: (t.server.name, t.tool.name))
