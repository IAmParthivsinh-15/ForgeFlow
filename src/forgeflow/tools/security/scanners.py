"""Deterministic security scanners (spec sections 14, 190).

Gitleaks (secrets), Bandit (Python SAST), Semgrep (multi-language SAST with rules
bundled in this repository, run offline with metrics disabled), and dependency
audits (npm audit / pip-audit) which need network access.

A scanner whose binary is missing reports status "unavailable" - never a silent
pass. Findings are normalised to ScannerFinding with an OWASP Top 10 category.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from forgeflow.schemas.verification import ScannerFinding, ScanResult, Severity
from forgeflow.tools.filesystem.repository import IGNORED_DIRS

# CWE -> OWASP Top 10 (2021), for scanners that report CWEs.
CWE_TO_OWASP: dict[int, str] = {
    22: "A01",
    284: "A01",
    285: "A01",
    639: "A01",
    352: "A01",
    259: "A07",
    287: "A07",
    798: "A07",
    384: "A07",
    295: "A02",
    326: "A02",
    327: "A02",
    328: "A02",
    330: "A02",
    338: "A02",
    77: "A03",
    78: "A03",
    79: "A03",
    89: "A03",
    94: "A03",
    95: "A03",
    611: "A05",
    16: "A05",
    209: "A05",
    732: "A05",
    377: "A05",
    703: "A09",
    532: "A09",
    502: "A08",
    494: "A08",
    829: "A08",
    918: "A10",
    400: "A04",
    1104: "A06",
}
MAX_EVIDENCE = 300

Runner = Callable[[Path, float], Awaitable[ScanResult]]


async def _exec(argv: list[str], cwd: Path, limit_s: float) -> tuple[int | None, str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k in ("PATH", "HOME", "LANG", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE")
    }
    env["SEMGREP_SEND_METRICS"] = "off"
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), limit_s)
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        return None, "", "timed out"
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _rel(root: Path, path: str) -> str:
    try:
        return Path(path).resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return path.replace("\\", "/").lstrip("./")


def _timed(tool: str):
    def wrap(fn):
        async def run(root: Path, limit_s: float) -> ScanResult:
            started = time.perf_counter()
            result = await fn(root, limit_s)
            result.duration_ms = int((time.perf_counter() - started) * 1000)
            return result

        run.tool = tool  # type: ignore[attr-defined]
        return run

    return wrap


# ----------------------------------------------------------------------- gitleaks


@_timed("gitleaks")
async def run_gitleaks(root: Path, limit_s: float) -> ScanResult:
    exe = shutil.which("gitleaks")
    if exe is None:
        return ScanResult(tool="gitleaks", status="unavailable", detail="gitleaks not installed")
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "gitleaks.json"
        code, _, err = await _exec(
            [
                exe,
                "dir",
                str(root),
                "--report-format",
                "json",
                "--report-path",
                str(report),
                "--redact",
                "--no-banner",
                "--exit-code",
                "0",
                "--log-level",
                "error",
            ],
            root,
            limit_s,
        )
        if code is None or not report.exists():
            return ScanResult(tool="gitleaks", status="error", detail=(err or "no report")[:500])
        data = json.loads(report.read_text(encoding="utf-8") or "[]")
    findings = [
        ScannerFinding(
            tool="gitleaks",
            rule_id=str(item.get("RuleID", "secret")),
            severity="high",
            category="A07",
            file=_rel(root, item.get("File", "")),
            line=item.get("StartLine"),
            message=item.get("Description", "Potential secret"),
            evidence=str(item.get("Match", ""))[:MAX_EVIDENCE],  # redacted by --redact
        )
        for item in data or []
    ]
    return ScanResult(tool="gitleaks", status="completed", findings=findings)


# ------------------------------------------------------------------------- bandit

_BANDIT_SEVERITY: dict[str, Severity] = {"LOW": "low", "MEDIUM": "medium", "HIGH": "high"}


@_timed("bandit")
async def run_bandit(root: Path, limit_s: float) -> ScanResult:
    exe = shutil.which("bandit")
    if exe is None:
        return ScanResult(tool="bandit", status="unavailable", detail="bandit not installed")
    if not any(root.rglob("*.py")):
        return ScanResult(tool="bandit", status="skipped", detail="no Python files")
    excludes = ",".join(str(root / d) for d in sorted(IGNORED_DIRS))
    code, out, err = await _exec(
        [exe, "-r", str(root), "-f", "json", "-q", "-x", excludes], root, limit_s
    )
    if code is None or not out.strip():
        return ScanResult(tool="bandit", status="error", detail=(err or "no output")[:500])
    data = json.loads(out)
    findings = []
    for item in data.get("results", []):
        cwe = (item.get("issue_cwe") or {}).get("id")
        rel = _rel(root, item.get("filename", ""))
        if "/tests/" in f"/{rel}" and item.get("test_id") == "B101":
            continue  # assert usage in tests is expected
        findings.append(
            ScannerFinding(
                tool="bandit",
                rule_id=item.get("test_id", "bandit"),
                severity=_BANDIT_SEVERITY.get(item.get("issue_severity", ""), "low"),
                category=CWE_TO_OWASP.get(cwe or 0, "unmapped"),
                file=rel,
                line=item.get("line_number"),
                message=item.get("issue_text", ""),
                evidence=str(item.get("code", "")).strip()[:MAX_EVIDENCE],
            )
        )
    return ScanResult(tool="bandit", status="completed", findings=findings)


# ------------------------------------------------------------------------ semgrep

_SEMGREP_SEVERITY: dict[str, Severity] = {"ERROR": "high", "WARNING": "medium", "INFO": "low"}


def make_semgrep(rules: Path) -> Runner:
    @_timed("semgrep")
    async def run_semgrep(root: Path, limit_s: float) -> ScanResult:
        exe = shutil.which("semgrep")
        if exe is None:
            return ScanResult(tool="semgrep", status="unavailable", detail="semgrep not installed")
        if not rules.exists():
            return ScanResult(tool="semgrep", status="error", detail=f"rules not found: {rules}")
        code, out, err = await _exec(
            [
                exe,
                "scan",
                "--config",
                str(rules.resolve()),
                "--json",
                "--metrics=off",
                "--disable-version-check",
                "--quiet",
                "--timeout",
                "30",
                str(root),
            ],
            root,
            limit_s,
        )
        if code is None or not out.strip():
            return ScanResult(tool="semgrep", status="error", detail=(err or "no output")[:500])
        data = json.loads(out)
        findings = []
        for item in data.get("results", []):
            extra = item.get("extra", {})
            owasp = (extra.get("metadata") or {}).get("owasp") or []
            owasp = owasp if isinstance(owasp, list) else [owasp]
            category = str(owasp[0])[:3] if owasp else "unmapped"
            findings.append(
                ScannerFinding(
                    tool="semgrep",
                    rule_id=str(item.get("check_id", "semgrep")).rsplit(".", 1)[-1],
                    severity=_SEMGREP_SEVERITY.get(extra.get("severity", ""), "low"),
                    category=category,
                    file=_rel(root, item.get("path", "")),
                    line=(item.get("start") or {}).get("line"),
                    message=extra.get("message", ""),
                    evidence=str(extra.get("lines", "")).strip()[:MAX_EVIDENCE],
                )
            )
        errors = [e.get("message", "") for e in data.get("errors", [])][:3]
        return ScanResult(
            tool="semgrep", status="completed", findings=findings, detail="; ".join(errors)[:500]
        )

    return run_semgrep


# ------------------------------------------------------------- dependency audits

_AUDIT_SEVERITY: dict[str, Severity] = {
    "info": "info",
    "low": "low",
    "moderate": "medium",
    "high": "high",
    "critical": "critical",
}


@_timed("npm-audit")
async def run_npm_audit(root: Path, limit_s: float) -> ScanResult:
    if not (root / "package-lock.json").is_file():
        return ScanResult(tool="npm-audit", status="skipped", detail="no package-lock.json")
    exe = shutil.which("npm")
    if exe is None:
        return ScanResult(tool="npm-audit", status="unavailable", detail="npm not installed")
    code, out, err = await _exec([exe, "audit", "--json", "--omit=dev"], root, limit_s)
    try:
        data = json.loads(out)
    except ValueError:
        return ScanResult(
            tool="npm-audit",
            status="error",
            detail=f"audit unavailable (network?): {(err or out)[:300]}",
        )
    if "error" in data:
        return ScanResult(tool="npm-audit", status="error", detail=str(data["error"])[:300])
    findings = [
        ScannerFinding(
            tool="npm-audit",
            rule_id=name,
            severity=_AUDIT_SEVERITY.get(v.get("severity"), "low"),
            category="A06",
            file="package-lock.json",
            message=f"{name}: vulnerable version range {v.get('range', '?')}",
        )
        for name, v in (data.get("vulnerabilities") or {}).items()
    ]
    return ScanResult(tool="npm-audit", status="completed", findings=findings)


@_timed("pip-audit")
async def run_pip_audit(root: Path, limit_s: float) -> ScanResult:
    requirements = root / "requirements.txt"
    if not requirements.is_file():
        return ScanResult(tool="pip-audit", status="skipped", detail="no requirements.txt")
    exe = shutil.which("pip-audit")
    if exe is None:
        return ScanResult(tool="pip-audit", status="unavailable", detail="pip-audit not installed")
    code, out, err = await _exec(
        [exe, "-r", str(requirements), "-f", "json", "--progress-spinner", "off"], root, limit_s
    )
    try:
        data = json.loads(out)
    except ValueError:
        return ScanResult(
            tool="pip-audit",
            status="error",
            detail=f"audit unavailable (network?): {(err or out)[:300]}",
        )
    deps = data.get("dependencies", data) if isinstance(data, dict) else data
    findings = [
        ScannerFinding(
            tool="pip-audit",
            rule_id=vuln.get("id", "vuln"),
            severity="high",
            category="A06",
            file="requirements.txt",
            message=f"{dep.get('name')} {dep.get('version')}: {vuln.get('id')} "
            f"(fixed in {', '.join(vuln.get('fix_versions', [])) or 'n/a'})",
        )
        for dep in deps or []
        for vuln in dep.get("vulns", [])
    ]
    return ScanResult(tool="pip-audit", status="completed", findings=findings)


# --------------------------------------------------------------------------- suite


class ScannerSuite:
    def __init__(self, runners: list[Runner], limit_s: float = 300) -> None:
        self.runners = runners
        self.timeout = limit_s

    @classmethod
    def default(cls, semgrep_rules: Path, limit_s: float = 300) -> ScannerSuite:
        return cls(
            [run_gitleaks, run_bandit, make_semgrep(semgrep_rules), run_npm_audit, run_pip_audit],
            limit_s,
        )

    async def run(self, root: Path) -> list[ScanResult]:
        async def guarded(runner: Runner) -> ScanResult:
            tool = getattr(runner, "tool", "scanner")
            try:
                return await runner(root, self.timeout)
            except Exception as exc:  # a broken scanner must not fail the security stage
                return ScanResult(tool=tool, status="error", detail=f"{type(exc).__name__}: {exc}")

        return list(await asyncio.gather(*(guarded(r) for r in self.runners)))
