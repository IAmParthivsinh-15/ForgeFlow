"""Built-in capability catalog: native tools and connector operations (spec sections 29, 223, 235).

Native tools are ForgeFlow's own deterministic tools; they are listed here so every
agent's manifest is complete and reproducible. Connector operations are what the
GitHub plugin can do, each with permissions, risk and a default approval policy.
"""

from __future__ import annotations

from forgeflow.schemas.extensibility import Capability

# Native tools per agent type (mirrors agents/<agent>/tools.py).
_READ = ["list_files", "read_file", "search_code"]
_KNOWLEDGE = ["search_engineering_history", "search_repository_index"]
NATIVE_TOOLS_BY_AGENT: dict[str, list[str]] = {
    "orchestrator": [],
    "requirement_analyzer": _READ,
    "developer": [*_READ, *_KNOWLEDGE],
    "developer_subagent": [
        *_READ,
        *_KNOWLEDGE,
        "write_file",
        "replace_in_file",
        "delete_file",
        "run_check",
    ],
    "integrator": [*_READ, "write_file"],
    "code_review": [*_READ, "view_diff", "ask_developer"],
    "security": [*_READ, "view_diff", "ask_developer"],
    "qa": [*_READ, "view_diff", "run_check", "ask_developer"],
    "ci": [*_READ, "search_engineering_history"],
}

_NATIVE_META = {
    "list_files": (["READ"], "low"),
    "read_file": (["READ"], "low"),
    "search_code": (["READ"], "low"),
    "view_diff": (["READ"], "low"),
    "search_engineering_history": (["READ"], "low"),
    "search_repository_index": (["READ"], "low"),
    "ask_developer": (["READ"], "low"),
    "write_file": (["WRITE"], "medium"),
    "replace_in_file": (["WRITE"], "medium"),
    # Deletion is confined to the task's own worktree and file scope, and is reversible
    # because every change lands on a forgeflow/* branch first.
    "delete_file": (["WRITE"], "high"),
    "run_check": (["EXECUTE"], "medium"),
}


def native_capabilities() -> list[Capability]:
    caps = []
    for name, (permissions, risk) in _NATIVE_META.items():
        agents = [a for a, tools in NATIVE_TOOLS_BY_AGENT.items() if name in tools]
        caps.append(
            Capability(
                capability_id=f"native.{name}",
                type="native_tool",
                name=name,
                description=f"ForgeFlow native tool {name} (workspace-scoped)",
                visibility="public",
                permissions=permissions,  # type: ignore[arg-type]
                risk=risk,  # type: ignore[arg-type]
                default_policy="auto",
                allowed_agents=agents,
            )
        )
    return caps


# GitHub plugin operations (spec sections 58, 192, 236). Merge is deliberately absent.
GITHUB_OPERATIONS: dict[str, dict] = {
    "github.repository.read": {
        "description": "Read repository metadata and permissions",
        "permissions": ["READ", "NETWORK"],
        "risk": "low",
        "policy": "auto",
    },
    "github.branch.push": {
        "description": "Push a forgeflow/* branch to the repository",
        "permissions": ["WRITE", "NETWORK"],
        "risk": "medium",
        "policy": "auto",
    },
    "github.pull_request.create": {
        "description": "Open a pull request from a forgeflow/* branch",
        "permissions": ["WRITE", "NETWORK"],
        "risk": "medium",
        "policy": "ask",
    },
    "github.pull_request.create_draft": {
        "description": "Open a draft pull request from a forgeflow/* branch (L4 action profile)",
        "permissions": ["WRITE", "NETWORK"],
        "risk": "low",
        "policy": "ask",
    },
    "github.issue.read": {
        "description": "Read an issue and its comments",
        "permissions": ["READ", "NETWORK"],
        "risk": "low",
        "policy": "auto",
    },
    "github.issue.comment": {
        "description": "Post an evidence/status comment on the source issue",
        "permissions": ["WRITE", "NETWORK"],
        "risk": "low",
        "policy": "ask",
    },
    "github.pull_request.comment": {
        "description": "Comment on a pull request",
        "permissions": ["WRITE", "NETWORK"],
        "risk": "low",
        "policy": "auto",
    },
    "github.pull_request.status": {
        "description": "Read a pull request's state",
        "permissions": ["READ", "NETWORK"],
        "risk": "low",
        "policy": "auto",
    },
}


def github_capabilities(connector_id: str, owner_id: str) -> list[Capability]:
    return [
        Capability(
            capability_id=name,
            type="connector",
            name=name,
            description=spec["description"],
            source_id=connector_id,
            owner_id=owner_id,
            permissions=spec["permissions"],
            risk=spec["risk"],
            default_policy=spec["policy"],
        )
        for name, spec in GITHUB_OPERATIONS.items()
    ]


# Argo CD plugin operations (spec sections 43, 157). ForgeFlow never applies manifests;
# Argo CD reconciles. Sync and rollback change what runs, so they always ask.
ARGOCD_OPERATIONS: dict[str, dict] = {
    "deployment.status": {
        "description": "Read an Argo CD application's sync and health status",
        "permissions": ["READ", "NETWORK"],
        "risk": "low",
        "policy": "auto",
    },
    "deployment.sync": {
        "description": "Ask Argo CD to sync an application (no pruning)",
        "permissions": ["DEPLOY", "WRITE", "NETWORK"],
        "risk": "high",
        "policy": "ask",
    },
    "deployment.rollback": {
        "description": "Roll an Argo CD application back to an earlier deployed revision",
        "permissions": ["DEPLOY", "WRITE", "NETWORK"],
        "risk": "high",
        "policy": "ask",
    },
}


def argocd_capabilities(connector_id: str, owner_id: str) -> list[Capability]:
    return [
        Capability(
            capability_id=name,
            type="connector",
            name=name,
            description=spec["description"],
            source_id=connector_id,
            owner_id=owner_id,
            permissions=spec["permissions"],
            risk=spec["risk"],
            default_policy=spec["policy"],
        )
        for name, spec in ARGOCD_OPERATIONS.items()
    ]
