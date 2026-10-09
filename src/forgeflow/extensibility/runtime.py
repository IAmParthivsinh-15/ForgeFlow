"""Per-run capability runtime: base agent + resolved skills + MCP proxies = runtime agent.

Spec section 225. MCP tools are never handed to the SDK agent directly. Each
allowed tool becomes a FunctionTool proxy whose every call goes through the
capability gateway (policy, approval, revocation check, audit).

Tools with a URL guard (e.g. Playwright's browser_navigate) may only open the
origins allowed for this run: the app under test plus the project's extra origins.
Images returned by a tool (screenshots) are stored as artifacts; the agent gets a
short reference instead of the bytes.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from agents import FunctionTool

from forgeflow.extensibility.gateway import CapabilityDenied, CapabilityGateway, Invocation
from forgeflow.extensibility.mcp.service import MCPService, tool_capability
from forgeflow.extensibility.resolver import CapabilityResolver, ResolvedTool
from forgeflow.extensibility.skills.selection import reference_files, render
from forgeflow.schemas.extensibility import CapabilityManifest, Project
from forgeflow.tools.skill_tools import SKILL_TOOLS

MAX_RESULT_CHARS = 20_000


@dataclass
class AgentCapabilities:
    manifest: CapabilityManifest
    prompt: str = ""
    skill_files: dict[str, str] = field(default_factory=dict)
    tools: list[Any] = field(default_factory=list)
    # One entry per MCP call: {"tool", "server", "status", "artifacts"}.
    calls: list[dict[str, Any]] = field(default_factory=list)


def proxy_name(server_name: str, tool_name: str) -> str:
    raw = f"mcp_{server_name}_{tool_name}".lower()
    return re.sub(r"[^a-z0-9_-]+", "_", raw)[:64]


def _schema(schema: dict[str, Any]) -> dict[str, Any]:
    out = dict(schema or {})
    out.setdefault("type", "object")
    out.setdefault("properties", {})
    return out


def origin(url: str) -> str | None:
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"{parts.scheme}://{parts.hostname.lower()}:{port}"


def guard_url(value: Any, allowed: list[str]) -> str | None:
    """None if the URL may be opened, else the reason it may not."""
    if not isinstance(value, str) or not value.strip():
        return None  # e.g. browser_tabs "list" carries no URL
    if value.strip() == "about:blank":
        return None
    target = origin(value)
    if target is None:
        return f"only http(s) URLs may be opened, not {value[:80]!r}"
    allowed_origins = {o for o in (origin(a) for a in allowed) if o}
    if not allowed_origins:
        return "no app under test is being served for this run"
    if target not in allowed_origins:
        return f"{target} is not an allowed origin ({', '.join(sorted(allowed_origins))})"
    return None


def format_result(result: Any) -> str:
    parts = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        parts.append(text if text is not None else f"[{getattr(item, 'type', 'content')} omitted]")
    structured = getattr(result, "structured_content", None) or getattr(
        result, "structuredContent", None
    )
    if not parts and structured is not None:
        parts.append(json.dumps(structured)[:MAX_RESULT_CHARS])
    text = "\n".join(parts) or "(no output)"
    is_error = getattr(result, "is_error", None) or getattr(result, "isError", False)
    return (("ERROR: " if is_error else "") + text)[:MAX_RESULT_CHARS]


class CapabilityRuntime:
    def __init__(
        self,
        resolver: CapabilityResolver,
        mcp: MCPService,
        gateway: CapabilityGateway,
        artifacts: Any = None,
    ) -> None:
        self.resolver = resolver
        self.mcp = mcp
        self.gateway = gateway
        self.artifacts = artifacts

    @asynccontextmanager
    async def for_agent(
        self,
        *,
        owner_id: str,
        workflow_id: str,
        task_id: str | None,
        project: Project | None,
        agent: str,
        context: str,
        pinned: CapabilityManifest | None = None,
        allowed_origins: list[str] | None = None,
    ) -> AsyncIterator[AgentCapabilities]:
        resolution = await self.resolver.resolve(
            owner_id=owner_id,
            workflow_id=workflow_id,
            project=project,
            agent=agent,
            context=context,
            pinned=pinned,
        )
        inv = Invocation(
            owner_id=owner_id,
            workflow_id=workflow_id,
            task_id=task_id,
            agent=agent,
            project=project,
        )
        bundle = AgentCapabilities(
            manifest=resolution.manifest,
            prompt=render(resolution.skills),
            skill_files=reference_files(resolution.skills),
        )
        for candidate, level in resolution.skills:
            await self.gateway.record_skill_use(
                inv, candidate.skill.skill_id, candidate.version.version, level
            )
        if bundle.skill_files:
            bundle.tools.extend(SKILL_TOOLS)

        async with AsyncExitStack() as stack:
            sessions: dict[str, Any] = {}
            for resolved in resolution.tools:
                server = resolved.server
                if server.mcp_id not in sessions:
                    try:
                        sessions[server.mcp_id] = await stack.enter_async_context(
                            self.mcp.session(server)
                        )
                    except Exception as exc:  # an unreachable MCP server must not fail the task
                        sessions[server.mcp_id] = None
                        bundle.manifest.unavailable.append(
                            f"mcp {server.name}: connection failed ({type(exc).__name__})"
                        )
                if sessions[server.mcp_id] is not None:
                    bundle.tools.append(
                        self._proxy(
                            resolved, sessions[server.mcp_id], inv, bundle, allowed_origins or []
                        )
                    )
            yield bundle

    async def _render(self, result: Any, inv: Invocation, source: str) -> tuple[str, list[str]]:
        """Text for the agent; images become artifacts (spec section 155)."""
        saved: list[str] = []
        parts: list[str] = []
        for item in getattr(result, "content", None) or []:
            kind = getattr(item, "type", "")
            data = getattr(item, "data", None)
            mime = getattr(item, "mime_type", None) or getattr(item, "mimeType", None)
            if kind == "image" and data and self.artifacts is not None:
                try:
                    artifact = await self.artifacts.save(
                        workflow_id=inv.workflow_id,
                        task_id=inv.task_id,
                        type="screenshot",
                        name=f"{source} screenshot",
                        content=base64.b64decode(data, validate=True),
                        content_type=mime or "image/png",
                        source=source,
                    )
                except Exception as exc:  # evidence capture is best-effort, never fatal
                    parts.append(f"[image not stored: {type(exc).__name__}]")
                    continue
                saved.append(artifact.artifact_id)
                parts.append(f"[screenshot stored as evidence artifact {artifact.artifact_id}]")
        text = format_result(result)
        if saved:
            text = "\n".join([text.replace("[image omitted]", "").strip(), *parts]).strip()
        elif parts:
            text = "\n".join([text, *parts])
        return text, saved

    def _proxy(
        self,
        resolved: ResolvedTool,
        session: Any,
        inv: Invocation,
        bundle: AgentCapabilities,
        allowed_origins: list[str],
    ) -> FunctionTool:
        server, tool = resolved.server, resolved.tool
        capability = tool_capability(server, tool)
        mcp = self.mcp
        gateway = self.gateway
        url_argument = server.url_guard.get(tool.name)
        extra_origins = list(inv.project.browser_allowed_origins) if inv.project else []

        async def on_invoke(_ctx: Any, raw: str) -> str:
            try:
                arguments = json.loads(raw or "{}")
            except ValueError:
                return "ERROR: arguments must be a JSON object"
            if url_argument is not None:
                reason = guard_url(arguments.get(url_argument), allowed_origins + extra_origins)
                if reason is not None:
                    bundle.calls.append(
                        {"tool": tool.name, "server": server.name, "status": "blocked"}
                    )
                    return f"DENIED: {reason}. Only the app under test may be opened."
            preview = json.dumps(arguments)[:200]
            try:
                result = await gateway.invoke(
                    inv,
                    capability,
                    lambda: session.call_tool(tool.name, arguments),
                    summary=f"{server.name}.{tool.name}({preview})",
                    details={
                        "server": server.name,
                        "tool": tool.name,
                        "operation": tool.operation,
                        "arguments": arguments,
                    },
                    tool_override=tool.policy_override,
                    source_status=lambda: mcp.status(server.mcp_id),
                )
            except CapabilityDenied as exc:
                bundle.calls.append({"tool": tool.name, "server": server.name, "status": "denied"})
                return f"DENIED: {exc}. Continue without this tool."
            except Exception as exc:
                bundle.calls.append({"tool": tool.name, "server": server.name, "status": "error"})
                return f"ERROR: {type(exc).__name__}: {str(exc)[:300]}"
            text, saved = await self._render(result, inv, f"{server.name}.{tool.name}")
            failed = bool(getattr(result, "is_error", None) or getattr(result, "isError", False))
            bundle.calls.append(
                {
                    "tool": tool.name,
                    "server": server.name,
                    "status": "error" if failed else "ok",
                    "artifacts": saved,
                    "excerpt": text[:500],
                }
            )
            return text

        return FunctionTool(
            name=proxy_name(server.name, tool.name),
            description=f"[MCP {server.name}, {tool.operation}] {tool.description}"[:1000],
            params_json_schema=_schema(tool.input_schema),
            on_invoke_tool=on_invoke,
            strict_json_schema=False,
        )
