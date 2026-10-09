"""MCP gateway: registration, discovery, classification and sessions (spec sections 207-211, 246).

MCP servers are untrusted external capabilities. ForgeFlow:
- only starts stdio servers from an admin allowlist (no user-supplied commands);
- keeps credentials in the secret store and sends them as a header, never in a URL;
- discovers tools once and caches them (invalidated on refresh or config change);
- classifies every tool as read / write / destructive and derives a default policy;
- never exposes every tool to every agent (agents receive proxies, see runtime.py);
- offers admin-reviewed presets (config/mcp_presets.yaml) with per-tool policies.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from agents.mcp import MCPServer, MCPServerSse, MCPServerStdio, MCPServerStreamableHttp

from forgeflow.core.errors import NotFoundError, ValidationFailed
from forgeflow.core.ids import new_id, utcnow
from forgeflow.extensibility.secrets import SecretStore
from forgeflow.extensibility.store import DocumentStore
from forgeflow.schemas.extensibility import (
    AGENT_TYPES,
    Capability,
    MCPServerConfig,
    MCPToolInfo,
    Policy,
)

logger = logging.getLogger(__name__)

_READ = re.compile(
    r"^(get|list|read|search|fetch|query|describe|view|show|find|lookup|browser_snapshot"
    r"|browser_console|browser_network|browser_take_screenshot|echo|ping|status)",
    re.I,
)
_DESTRUCTIVE = re.compile(
    r"(delete|drop|remove|destroy|purge|truncate|reset|wipe|kill|terminate|"
    r"force_push|revoke)",
    re.I,
)


def load_presets(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return dict(data.get("presets") or {})


def load_allowlist(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return dict(data.get("servers") or {})


def classify(name: str, annotations: Any) -> tuple[str, str]:
    """(operation, risk) from MCP tool annotations, falling back to the tool name."""
    read_only = getattr(annotations, "read_only_hint", None) if annotations else None
    destructive = getattr(annotations, "destructive_hint", None) if annotations else None
    if read_only is None and annotations is not None:
        read_only = getattr(annotations, "readOnlyHint", None)
        destructive = getattr(annotations, "destructiveHint", None)
    if read_only:
        return "read", "low"
    if destructive or _DESTRUCTIVE.search(name):
        return "destructive", "high"
    if read_only is None and _READ.search(name):
        return "read", "low"
    return "write", "medium"


def source_policy(server: MCPServerConfig, operation: str) -> Policy:
    if server.approval_policy == "ask_all":
        return "ask"
    if server.approval_policy == "auto_all":
        return "auto" if operation != "destructive" else "ask"
    return "auto" if operation == "read" else "ask"


def tool_capability(server: MCPServerConfig, tool: MCPToolInfo) -> Capability:
    permissions = ["NETWORK"] + {
        "read": ["READ"],
        "write": ["WRITE"],
        "destructive": ["WRITE", "DESTRUCTIVE"],
    }[tool.operation]
    return Capability(
        capability_id=f"mcp.{server.mcp_id}.{tool.name}",
        type="mcp_tool",
        name=f"{server.name}.{tool.name}",
        description=tool.description[:300],
        source_id=server.mcp_id,
        owner_id=server.owner_id,
        visibility=server.visibility,
        risk=tool.risk,
        permissions=permissions,  # type: ignore[arg-type]
        default_policy=source_policy(server, tool.operation),
        status="active" if tool.enabled and server.status == "active" else "disabled",
        allowed_agents=server.allowed_agents,
    )


def config_hash(server: MCPServerConfig) -> str:
    payload = json.dumps(
        {
            "transport": server.transport,
            "url": server.url,
            "stdio": server.stdio_server,
            "header": server.auth_header,
            "has_credential": bool(server.credential_ref),
        },
        sort_keys=True,
    )
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()[:32]


class MCPService:
    def __init__(
        self,
        store: DocumentStore,
        secrets: SecretStore,
        allowlist_path: Path,
        timeout_seconds: float = 30,
        presets_path: Path | None = None,
    ) -> None:
        self.store = store
        self.secrets = secrets
        self.allowlist_path = allowlist_path
        self.timeout_seconds = timeout_seconds
        self.presets_path = presets_path

    def allowlist(self) -> dict[str, dict[str, Any]]:
        return load_allowlist(self.allowlist_path)

    def presets(self) -> dict[str, dict[str, Any]]:
        return load_presets(self.presets_path) if self.presets_path else {}

    async def register_preset(self, owner_id: str, key: str) -> MCPServerConfig:
        """Register (or return the existing) server for a preset."""
        preset = self.presets().get(key)
        if preset is None:
            raise ValidationFailed(f"unknown MCP preset '{key}'")
        for server in await self.list_servers(owner_id):
            if server.preset == key and server.status != "revoked":
                return server
        return await self.register(
            owner_id,
            name=str(preset.get("name") or key),
            transport=str(preset["transport"]),
            url=preset.get("url"),
            stdio_server=preset.get("stdio_server"),
            description=str(preset.get("description") or "")[:500],
            allowed_agents=list(preset.get("allowed_agents") or []),
            trusted=True,
            preset=key,
            url_guard={str(k): str(v) for k, v in (preset.get("url_guard") or {}).items()},
        )

    def _preset_tool(self, server: MCPServerConfig, name: str) -> tuple[Policy | None, bool]:
        """(policy override, enabled) a preset assigns to a newly discovered tool."""
        preset = self.presets().get(server.preset or "") if server.preset else None
        if not preset:
            return None, True
        policy = (preset.get("tool_policies") or {}).get(name) or preset.get("default_policy")
        if policy not in ("auto", "ask", "deny"):
            return None, True
        return policy, policy != "deny"

    # ---------------------------------------------------------------- register

    async def register(
        self,
        owner_id: str,
        *,
        name: str,
        transport: str,
        url: str | None = None,
        stdio_server: str | None = None,
        token: str | None = None,
        auth_header: str = "Authorization",
        description: str = "",
        approval_policy: str = "auto_read_ask_write",
        allowed_agents: list[str] | None = None,
        trusted: bool = False,
        preset: str | None = None,
        url_guard: dict[str, str] | None = None,
    ) -> MCPServerConfig:
        if transport == "stdio":
            if not stdio_server or stdio_server not in self.allowlist():
                raise ValidationFailed(
                    f"stdio servers must be one of the allowlisted entries: "
                    f"{', '.join(sorted(self.allowlist())) or 'none configured'}"
                )
            url = None
        elif transport in ("streamable_http", "sse"):
            parts = urlsplit(url or "")
            if parts.scheme not in ("https", "http") or not parts.hostname:
                raise ValidationFailed("an HTTP MCP server needs an http(s) URL")
            if (
                parts.username
                or parts.password
                or re.search(r"(token|key|secret)=", parts.query, re.I)
            ):
                raise ValidationFailed("put credentials in the token field, never in the URL")
            stdio_server = None
        else:
            raise ValidationFailed("transport must be stdio, streamable_http or sse")
        bad = sorted(set(allowed_agents or []) - set(AGENT_TYPES))
        if bad:
            raise ValidationFailed(f"unknown agents: {bad}")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,63}", auth_header):
            raise ValidationFailed("invalid auth header name")
        now = utcnow()
        mcp_id = new_id("mcp")
        server = MCPServerConfig(
            mcp_id=mcp_id,
            owner_id=owner_id,
            name=name.strip()[:80] or mcp_id,
            description=description[:500],
            transport=transport,  # type: ignore[arg-type]
            url=url,
            stdio_server=stdio_server,
            auth_header=auth_header,
            status="registered",
            trusted=trusted,
            approval_policy=approval_policy,  # type: ignore[arg-type]
            allowed_agents=allowed_agents or [],
            preset=preset,
            url_guard=url_guard or {},
            created_at=now,
            updated_at=now,
        )
        if token:
            server.credential_ref = await self.secrets.put(f"secret://mcp/{mcp_id}", token.strip())
        server.config_hash = config_hash(server)
        await self.store.put("mcp_servers", server)
        return await self.refresh_tools(owner_id, mcp_id)

    # --------------------------------------------------------------- sessions

    async def _build(self, server: MCPServerConfig) -> MCPServer:
        timeout = self.timeout_seconds
        if server.transport == "stdio":
            entry = self.allowlist().get(server.stdio_server or "")
            if entry is None:
                raise ValidationFailed(
                    f"stdio server '{server.stdio_server}' is no longer allowlisted"
                )
            import os

            env = {
                k: os.environ[k]
                for k in (
                    "PATH",
                    "HOME",
                    "LANG",
                    "SYSTEMROOT",
                    "TEMP",
                    "TMP",
                    "USERPROFILE",
                    "APPDATA",
                    "LOCALAPPDATA",
                )
                if k in os.environ
            }
            env.update({k: os.environ[k] for k in entry.get("env", []) if k in os.environ})
            return MCPServerStdio(
                params={
                    "command": entry["command"],
                    "args": list(entry.get("args", [])),
                    "env": env,
                },
                name=server.name,
                client_session_timeout_seconds=timeout,
            )
        headers = {}
        if server.credential_ref:
            token = await self.secrets.get(server.credential_ref)
            headers[server.auth_header] = (
                f"Bearer {token}" if server.auth_header.lower() == "authorization" else token
            )
        params: Any = {"url": server.url or "", "headers": headers, "timeout": timeout}
        if server.transport == "sse":
            return MCPServerSse(
                params=params,
                name=server.name,  # type: ignore[arg-type]
                client_session_timeout_seconds=timeout,
            )
        return MCPServerStreamableHttp(
            params=params,
            name=server.name,  # type: ignore[arg-type]
            client_session_timeout_seconds=timeout,
        )

    @asynccontextmanager
    async def session(self, server: MCPServerConfig) -> AsyncIterator[MCPServer]:
        client = await self._build(server)
        await client.connect()
        try:
            yield client
        finally:
            try:
                await client.cleanup()
            except Exception:  # cleanup of a broken session must not mask the real error
                logger.debug("MCP session cleanup failed", exc_info=True)

    # -------------------------------------------------------------- discovery

    async def refresh_tools(self, owner_id: str, mcp_id: str) -> MCPServerConfig:
        """Connect, list tools, cache them, keep the user's per-tool overrides."""
        server = await self.get(owner_id, mcp_id)
        if server.status in ("revoked", "disabled"):
            raise ValidationFailed(f"the MCP server is {server.status}")
        existing = {t.name: t for t in await self.tools(owner_id, mcp_id)}
        try:
            async with self.session(server) as client:
                listed = await client.list_tools()
        except ValidationFailed:
            raise
        except Exception as exc:
            server.status = "error"
            server.last_error = f"{type(exc).__name__}: {exc}"[:500]
            server.updated_at = utcnow()
            await self.store.put("mcp_servers", server)
            return server
        seen = set()
        preset = self.presets().get(server.preset or "") if server.preset else None
        for tool in listed:
            operation, risk = classify(tool.name, getattr(tool, "annotations", None))
            if preset and preset.get("sandboxed") and operation == "destructive":
                # Reviewed preset: the tool only affects an isolated sandbox (e.g. a
                # throwaway browser), so it is a write there, not a destructive action.
                operation, risk = "write", "medium"
            schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}
            previous = existing.get(tool.name)
            preset_policy, preset_enabled = self._preset_tool(server, tool.name)
            info = MCPToolInfo(
                tool_id=f"{mcp_id}:{tool.name}",
                mcp_id=mcp_id,
                name=tool.name,
                description=(tool.description or "")[:1000],
                input_schema=dict(schema),
                operation=operation,  # type: ignore[arg-type]
                risk=risk,  # type: ignore[arg-type]
                policy_override=previous.policy_override if previous else preset_policy,
                enabled=previous.enabled if previous else preset_enabled,
            )
            await self.store.put("mcp_tools", info)
            seen.add(tool.name)
        for name, stale in existing.items():
            if name not in seen:
                await self.store.delete("mcp_tools", stale.tool_id)
        server.status = "active"
        server.last_error = None
        server.tools_refreshed_at = server.updated_at = utcnow()
        server.config_hash = config_hash(server)
        await self.store.put("mcp_servers", server)
        return server

    async def update_tool(
        self,
        owner_id: str,
        mcp_id: str,
        tool_name: str,
        *,
        enabled: bool | None = None,
        policy_override: Policy | None = None,
        clear_override: bool = False,
    ) -> MCPToolInfo:
        await self.get(owner_id, mcp_id)
        doc = await self.store.get("mcp_tools", f"{mcp_id}:{tool_name}")
        if doc is None:
            raise NotFoundError(f"tool {tool_name} not found")
        tool = MCPToolInfo.model_validate(doc)
        if enabled is not None:
            tool.enabled = enabled
        if clear_override:
            tool.policy_override = None
        elif policy_override is not None:
            tool.policy_override = policy_override
        await self.store.put("mcp_tools", tool)
        return tool

    # ------------------------------------------------------------- lifecycle

    async def set_enabled(self, owner_id: str, mcp_id: str, enabled: bool) -> MCPServerConfig:
        server = await self.get(owner_id, mcp_id)
        if server.status == "revoked":
            raise ValidationFailed("a revoked MCP server cannot be enabled; register it again")
        server.status = "active" if enabled else "disabled"
        server.updated_at = utcnow()
        await self.store.put("mcp_servers", server)
        return server

    async def revoke(self, owner_id: str, mcp_id: str) -> MCPServerConfig:
        server = await self.get(owner_id, mcp_id)
        if server.credential_ref:
            await self.secrets.delete(server.credential_ref)
        server.credential_ref = None
        server.status = "revoked"
        server.updated_at = utcnow()
        await self.store.put("mcp_servers", server)
        return server

    async def delete(self, owner_id: str, mcp_id: str) -> None:
        server = await self.get(owner_id, mcp_id)
        if server.credential_ref:
            await self.secrets.delete(server.credential_ref)
        for tool in await self.tools(owner_id, mcp_id):
            await self.store.delete("mcp_tools", tool.tool_id)
        await self.store.delete("mcp_servers", mcp_id)

    # ------------------------------------------------------------------ reads

    async def get(self, owner_id: str, mcp_id: str) -> MCPServerConfig:
        doc = await self.store.get("mcp_servers", mcp_id)
        if doc is None or doc["owner_id"] != owner_id:
            raise NotFoundError(f"MCP server {mcp_id} not found")
        return MCPServerConfig.model_validate(doc)

    async def list_servers(self, owner_id: str) -> list[MCPServerConfig]:
        docs = await self.store.find("mcp_servers", {"owner_id": owner_id}, sort="created_at")
        return [MCPServerConfig.model_validate(d) for d in docs]

    async def tools(self, owner_id: str, mcp_id: str) -> list[MCPToolInfo]:
        docs = await self.store.find("mcp_tools", {"mcp_id": mcp_id}, sort="name")
        return [MCPToolInfo.model_validate(d) for d in docs]

    async def status(self, mcp_id: str) -> str:
        doc = await self.store.get("mcp_servers", mcp_id)
        return doc["status"] if doc else "deleted"
