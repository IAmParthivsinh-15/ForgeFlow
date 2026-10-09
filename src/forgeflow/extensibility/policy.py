"""Policy engine: AUTO / ASK / DENY for a capability request (spec sections 29, 235-237, 241).

All of these must allow a capability: the capability's own default, its source's
configuration (MCP approval policy, per-tool override), the project's override, and
the agent allowlist. The most restrictive answer wins, with two hard rules:

- DESTRUCTIVE operations are DENY unless something explicitly sets them to ASK.
- Nothing can lower a capability below what the user configured for the project.
"""

from __future__ import annotations

from dataclasses import dataclass

from forgeflow.schemas.extensibility import Capability, Policy, Project

_RANK: dict[Policy, int] = {"auto": 0, "ask": 1, "deny": 2}


def strictest(*policies: Policy | None) -> Policy:
    chosen = [p for p in policies if p is not None]
    return max(chosen, key=lambda p: _RANK[p]) if chosen else "auto"


@dataclass(frozen=True)
class Decision:
    policy: Policy
    reason: str


def decide(
    capability: Capability,
    agent: str,
    project: Project | None = None,
    source_policy: Policy | None = None,
    tool_override: Policy | None = None,
) -> Decision:
    if capability.status not in ("active", "published"):
        return Decision("deny", f"{capability.name} is {capability.status}")
    if capability.allowed_agents and agent not in capability.allowed_agents:
        return Decision("deny", f"{agent} is not allowed to use {capability.name}")

    project_policy = project.capability_policies.get(capability.capability_id) if project else None
    if "DESTRUCTIVE" in capability.permissions:
        explicit = tool_override or project_policy
        if explicit in (None, "auto"):
            return Decision(
                "deny", "destructive operations are denied unless explicitly set to ask"
            )
        return Decision(strictest(explicit, "ask"), "destructive operation, explicitly allowed")

    if tool_override is not None:
        # An explicit per-tool setting replaces the source default but not the project's.
        policy = strictest(tool_override, project_policy)
    else:
        policy = strictest(capability.default_policy, source_policy, project_policy)
    reason = {
        "auto": "allowed by policy",
        "ask": "requires human approval",
        "deny": "denied by policy",
    }[policy]
    return Decision(policy, reason)
