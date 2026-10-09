"""Serve the commit under test for browser verification.

The repository declares how in `forgeflow.yaml`:

    preview:
      command: npm run preview -- --port {port} --host 0.0.0.0   # allowlisted like checks
      ready_path: /
      setup: true          # run the `setup` check first (e.g. npm ci)

    preview:
      static: public        # or serve a directory of static files

Without a `preview` section, a repository with `index.html` at its root (or in
`public/`) is served as static files by ForgeFlow itself, so no repository code runs.

Ports come from a small fixed pool (PREVIEW_PORTS) so the Playwright MCP container can
be limited to known origins. The browser reaches the app at http://<host>:<port> where
<host> is this worker's address on the network shared with the browser container.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import http.server
import os
import signal
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

from forgeflow.core.errors import PolicyViolation, ValidationFailed
from forgeflow.tools.shell.commands import CheckRunner, validate_command

STATIC_CANDIDATES = (".", "public", "site", "docs")


@dataclass
class PreviewConfig:
    command: str | None = None
    static_dir: str | None = None
    ready_path: str = "/"
    setup: bool = False


def preview_config(root: Path) -> PreviewConfig | None:
    manifest = root / "forgeflow.yaml"
    if manifest.is_file():
        data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        section = data.get("preview")
        if isinstance(section, dict):
            ready = str(section.get("ready_path") or "/")
            if not ready.startswith("/"):
                ready = "/" + ready
            if section.get("command"):
                return PreviewConfig(
                    command=str(section["command"]),
                    ready_path=ready,
                    setup=bool(section.get("setup", False)),
                )
            if section.get("static"):
                return PreviewConfig(static_dir=str(section["static"]), ready_path=ready)
    for candidate in STATIC_CANDIDATES:
        if (root / candidate / "index.html").is_file():
            return PreviewConfig(static_dir=candidate)
    return None


def parse_ports(spec: str) -> list[int]:
    ports: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            low, high = (int(x) for x in part.split("-", 1))
            ports += list(range(low, high + 1))
        else:
            ports.append(int(part))
    return ports


def preview_host(configured: str, peer: str) -> str:
    """Address of this process as seen from `peer` (the browser container)."""
    if configured:
        return configured
    with contextlib.suppress(OSError):
        # No packets are sent: connect() on UDP only selects the outgoing interface.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((socket.gethostbyname(peer), 9))
            return sock.getsockname()[0]
    return "127.0.0.1"


class PortPool:
    def __init__(self, ports: list[int]) -> None:
        self.free = list(ports)
        self.lock = asyncio.Lock()

    async def acquire(self) -> int:
        async with self.lock:
            while self.free:
                port = self.free.pop(0)
                with socket.socket() as probe:
                    try:
                        probe.bind(("0.0.0.0", port))  # noqa: S104 - the browser runs elsewhere
                    except OSError:
                        continue  # in use by something else; skip it
                return port
        raise ValidationFailed("no free preview port; too many browser checks at once")

    async def release(self, port: int) -> None:
        async with self.lock:
            if port not in self.free:
                self.free.append(port)


class _ConfinedHandler(http.server.SimpleHTTPRequestHandler):
    """Static files only from the served directory; symlinks may not escape it."""

    def translate_path(self, path: str) -> str:
        translated = Path(super().translate_path(path)).resolve()
        root = Path(self.directory).resolve()
        hidden = any(part.startswith(".") for part in path.split("?")[0].split("/") if part)
        if hidden or (translated != root and not translated.is_relative_to(root)):
            return str(root / "__forbidden__")
        return str(translated)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return None


class _StaticServer:
    def __init__(self, directory: Path, port: int) -> None:
        handler = functools.partial(_ConfinedHandler, directory=str(directory))
        self.server = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler)  # noqa: S104
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@dataclass
class Preview:
    url: str
    port: int
    mode: str  # "static" | "command"
    log: str = ""


class PreviewServer:
    """Async context manager: serve a checkout, yield the URL, stop afterwards."""

    def __init__(
        self,
        root: Path,
        pool: PortPool,
        host: str,
        timeout: float = 60,
        check_timeout: float = 600,
        exclude_path: str | None = None,
    ) -> None:
        self.root = root
        self.pool = pool
        self.host = host
        self.timeout = timeout
        self.check_timeout = check_timeout
        self.exclude_path = exclude_path
        self._static: _StaticServer | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._port: int | None = None

    async def __aenter__(self) -> Preview:
        config = preview_config(self.root)
        if config is None:
            raise ValidationFailed(
                "the repository declares no preview (forgeflow.yaml `preview:`) and has no "
                "index.html to serve"
            )
        self._port = await self.pool.acquire()
        try:
            return await self._start(config, self._port)
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def _start(self, config: PreviewConfig, port: int) -> Preview:
        url = f"http://{self.host}:{port}"
        if config.static_dir is not None:
            directory = (self.root / config.static_dir).resolve()
            if not directory.is_relative_to(self.root.resolve()) or not directory.is_dir():
                raise ValidationFailed(f"preview static directory not found: {config.static_dir}")
            self._static = _StaticServer(directory, port)
            self._static.start()
            await self._wait_ready(port, config.ready_path)
            return Preview(url=url, port=port, mode="static")

        assert config.command is not None
        runner = CheckRunner(self.root, self.check_timeout, exclude_path=self.exclude_path)
        if config.setup:
            setup = await runner.run("setup")
            failed = [r for r in setup if not r.passed]
            if failed:
                raise ValidationFailed(f"preview setup failed: {failed[0].command}")
        command = config.command.replace("{port}", str(port))
        argv = validate_command(command)
        env = runner._env()
        env.update(PORT=str(port), HOST="0.0.0.0")  # noqa: S104
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(self.root),
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=os.name != "nt",
            )
        except (FileNotFoundError, PermissionError) as exc:
            raise PolicyViolation(f"preview command cannot start: {exc}") from exc
        await self._wait_ready(port, config.ready_path)
        return Preview(url=url, port=port, mode="command")

    async def _wait_ready(self, port: int, path: str) -> None:
        deadline = time.monotonic() + self.timeout
        async with httpx.AsyncClient(timeout=3) as client:
            while time.monotonic() < deadline:
                if self._proc is not None and self._proc.returncode is not None:
                    raise ValidationFailed(
                        f"preview command exited with code {self._proc.returncode}"
                    )
                with contextlib.suppress(httpx.HTTPError):
                    response = await client.get(f"http://127.0.0.1:{port}{path}")
                    if response.status_code < 500:
                        return
                await asyncio.sleep(0.5)
        raise ValidationFailed(f"the app did not respond on port {port} within {self.timeout:.0f}s")

    def _signal(self, name: str) -> None:
        """Signal the preview's whole process group (npm spawns children) on POSIX."""
        assert self._proc is not None
        killpg = getattr(os, "killpg", None)
        sig = getattr(signal, name, None)
        if killpg is not None and sig is not None:
            killpg(self._proc.pid, sig)
        elif name == "SIGKILL":
            self._proc.kill()
        else:
            self._proc.terminate()

    async def __aexit__(self, *exc: object) -> None:
        if self._static is not None:
            await asyncio.to_thread(self._static.stop)
            self._static = None
        if self._proc is not None and self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                self._signal("SIGTERM")
            try:
                await asyncio.wait_for(self._proc.wait(), 10)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError, OSError):
                    self._signal("SIGKILL")
        self._proc = None
        if self._port is not None:
            await self.pool.release(self._port)
            self._port = None
