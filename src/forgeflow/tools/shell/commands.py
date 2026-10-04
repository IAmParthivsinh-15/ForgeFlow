"""Allowlisted repository commands (spec sections 73-77).

Agents never run arbitrary shell. They ask for a check *kind* ("test", "lint",
...). The command comes from the repository's `forgeflow.yaml` or from
auto-detection, is validated against an executable allowlist and a dangerous
pattern denylist, and runs without a shell, with a timeout and a minimal
environment that carries no ForgeFlow secrets.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import time
from pathlib import Path

import yaml

from forgeflow.core.errors import PolicyViolation
from forgeflow.schemas.task import CheckRun

CHECK_KINDS = ("setup", "test", "lint", "typecheck", "build")
ALLOWED_EXECUTABLES = frozenset(
    {
        "python",
        "python3",
        "pytest",
        "uv",
        "ruff",
        "mypy",
        "pip",
        "npm",
        "npx",
        "node",
        "pnpm",
        "yarn",
        "go",
        "cargo",
        "mvn",
        "gradle",
        "make",
        "dotnet",
    }
)
DENIED_TOKENS = (
    "rm -rf /",
    "git push",
    "git reset --hard",
    "--force",
    "drop database",
    "kubectl",
    "terraform",
    "curl ",
    "wget ",
    "sudo",
    "ssh ",
    "scp ",
)
# Commands never run through a shell, so operators inside a quoted argument are inert.
# A bare operator token (or one glued to a word, e.g. `pytest;`) signals an attempt to
# chain or redirect and is rejected.
OPERATOR_CHARS = "|&;<>"
MAX_OUTPUT_CHARS = 12_000
_PASSTHROUGH_ENV = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PATHEXT",
    "COMSPEC",
    "NODE_PATH",
)


def validate_command(command: str) -> list[str]:
    lowered = command.lower()
    if any(t in lowered for t in DENIED_TOKENS):
        raise PolicyViolation(f"command is blocked by policy: {command}")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise PolicyViolation(f"cannot parse command: {exc}") from exc
    if not argv:
        raise PolicyViolation("empty command")
    for token in argv:
        if (
            token[0] in OPERATOR_CHARS
            or token[-1] in OPERATOR_CHARS
            or "`" in token
            or "$(" in token
        ):
            raise PolicyViolation(f"shell operators are not allowed: {command}")
    exe = Path(argv[0]).name.lower().removesuffix(".exe").removesuffix(".cmd")
    if exe not in ALLOWED_EXECUTABLES:
        raise PolicyViolation(f"executable '{argv[0]}' is not on the allowlist")
    return argv


def detect_commands(root: Path) -> dict[str, list[str]]:
    """Repository manifest (forgeflow.yaml) first, then conventions (spec section 76)."""
    manifest = root / "forgeflow.yaml"
    if manifest.is_file():
        data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        commands = data.get("commands", {}) or {}
        return {
            kind: [str(c) for c in (v if isinstance(v, list) else [v])]
            for kind, v in commands.items()
            if kind in CHECK_KINDS and v
        }

    detected: dict[str, list[str]] = {}
    package_json = root / "package.json"
    if package_json.is_file():
        try:
            scripts = json.loads(package_json.read_text(encoding="utf-8")).get("scripts", {})
        except (ValueError, OSError):
            scripts = {}
        lock = (root / "package-lock.json").is_file()
        detected["setup"] = ["npm ci" if lock else "npm install --no-audit --no-fund"]
        for kind, script in (
            ("test", "test"),
            ("lint", "lint"),
            ("build", "build"),
            ("typecheck", "typecheck"),
        ):
            if script in scripts and "no test specified" not in str(scripts[script]):
                detected[kind] = [f"npm run {script} --silent"]
    if (
        (root / "pyproject.toml").is_file()
        or (root / "pytest.ini").is_file()
        or (root / "tests").is_dir()
    ):
        detected.setdefault("test", ["python -m pytest -q"])
    return detected


class CheckRunner:
    def __init__(self, root: Path, timeout: float = 600, exclude_path: str | None = None) -> None:
        self.root = root
        self.timeout = timeout
        # ForgeFlow's own virtualenv is removed from PATH so repository commands
        # cannot import or modify the platform's environment.
        self.exclude_path = exclude_path
        self.runs: list[CheckRun] = []
        self._setup_done = False

    def available(self) -> dict[str, list[str]]:
        return detect_commands(self.root)

    def _env(self) -> dict[str, str]:
        env = {k: os.environ[k] for k in _PASSTHROUGH_ENV if k in os.environ}
        if self.exclude_path and "PATH" in env:
            env["PATH"] = os.pathsep.join(
                p for p in env["PATH"].split(os.pathsep) if not p.startswith(self.exclude_path)
            )
        env.update(CI="true", PYTHONDONTWRITEBYTECODE="1", NO_COLOR="1")
        return env

    async def run(self, kind: str) -> list[CheckRun]:
        if kind not in CHECK_KINDS:
            raise PolicyViolation(f"unknown check kind '{kind}'; use one of {CHECK_KINDS}")
        commands = self.available()
        if kind != "setup" and not self._setup_done and commands.get("setup"):
            await self.run("setup")
        if kind == "setup":
            self._setup_done = True
        results = [await self._execute(kind, cmd) for cmd in commands.get(kind, [])]
        return results

    async def _execute(self, kind: str, command: str) -> CheckRun:
        argv = validate_command(command)
        env = self._env()
        exe = shutil.which(argv[0], path=env.get("PATH"))
        if exe is None:
            run = CheckRun(
                kind=kind,
                command=command,
                exit_code=None,
                passed=False,
                duration_ms=0,
                output=f"executable not found: {argv[0]}",
            )
            self.runs.append(run)
            return run
        started = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(
            exe,
            *argv[1:],
            cwd=str(self.root),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        timed_out = False
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), self.timeout)
        except TimeoutError:
            proc.kill()
            out, _ = await proc.communicate()
            timed_out = True
        text = out.decode("utf-8", errors="replace")
        run = CheckRun(
            kind=kind,
            command=command,
            exit_code=None if timed_out else proc.returncode,
            passed=not timed_out and proc.returncode == 0,
            duration_ms=int((time.perf_counter() - started) * 1000),
            output=text[-MAX_OUTPUT_CHARS:],
            timed_out=timed_out,
        )
        self.runs.append(run)
        return run
