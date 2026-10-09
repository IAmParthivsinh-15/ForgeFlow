"""Skill content validation (spec sections 214, 218, 251-253).

Skills are untrusted, declarative content that ends up in agent prompts. Some
problems block every skill (embedded secrets, hidden bidirectional text, unsafe
paths, executable files); others block *publishing* and are warnings for a
private skill the owner wrote for themselves (instruction-override phrases,
exfiltration wording, undeclared endpoints).
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from forgeflow.core.redaction import SECRET_RE
from forgeflow.schemas.extensibility import AGENT_TYPES, SkillMetadata, ValidationReport

MAX_PACKAGE_BYTES = 2_000_000
MAX_FILE_BYTES = 512_000
MAX_FILES = 50
ALLOWED_EXTENSIONS = frozenset({".md", ".markdown", ".txt", ".json", ".yaml", ".yml"})
ALLOWED_DIRS = frozenset({"examples", "references", "assets"})

_INJECTION = [
    r"ignore\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier)\s+(instructions|rules|guidance)",
    r"disregard\s+(your|the|all)\s+(system|previous|safety)",
    r"(override|bypass)\s+(the\s+)?(system\s+prompt|safety|guardrails|policy|permissions)",
    r"you\s+are\s+now\s+(?!able)",
    r"do\s+not\s+(tell|inform|mention\s+to)\s+the\s+user",
    r"(reveal|print|output|show)\s+(the\s+)?(system\s+prompt|secrets?|api[\s_-]?keys?|tokens?|"
    r"credentials?|environment\s+variables)",
    r"(send|post|upload|exfiltrat\w*|transmit)\b.{0,60}\b(tokens?|secrets?|passwords?|"
    r"credentials?|api[\s_-]?keys?|\.env)",
    r"(call|use|invoke)\s+(any|every|all)\s+tools?\s+(you|available)",
]
_INJECTION_RE = [re.compile(p, re.IGNORECASE | re.DOTALL) for p in _INJECTION]
_SECRET_RE = SECRET_RE
_BIDI_RE = re.compile("[‪-‮⁦-⁩]")
_ZERO_WIDTH_RE = re.compile("[​-‍⁠-⁤﻿]")
_URL_RE = re.compile(r"https?://[^\s)\]>'\"`]+", re.IGNORECASE)
_BASE64_RE = re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")


def safe_path(path: str) -> str | None:
    """Normalised relative path, or None if unsafe (absolute, traversal, wrong dir/ext)."""
    if not path or "\\" in path or path.startswith("/") or ":" in path:
        return None
    parts = PurePosixPath(path).parts
    if any(p in ("..", ".", "") for p in parts):
        return None
    if len(parts) > 1 and parts[0] not in ALLOWED_DIRS:
        return None
    if PurePosixPath(path).suffix.lower() not in ALLOWED_EXTENSIONS:
        return None
    return "/".join(parts)


def validate_skill(
    metadata: SkillMetadata, instructions: str, files: dict[str, str], public: bool
) -> ValidationReport:
    errors: list[str] = []
    warnings: list[str] = []

    def strict(message: str) -> None:
        (errors if public else warnings).append(message)

    if not instructions.strip():
        errors.append("skill.md must not be empty")
    unknown_agents = sorted(set(metadata.allowed_agents) - set(AGENT_TYPES))
    if unknown_agents:
        errors.append(f"unknown agents in allowed_agents: {unknown_agents}")
    if len(files) > MAX_FILES:
        errors.append(f"too many files ({len(files)} > {MAX_FILES})")

    documents = {"skill.md": instructions, **files}
    total = 0
    allowed_hosts = {urlsplit(u).hostname for u in metadata.endpoints if urlsplit(u).hostname}
    for path, text in documents.items():
        size = len(text.encode("utf-8"))
        total += size
        if path != "skill.md" and safe_path(path) is None:
            errors.append(
                f"{path}: path or file type not allowed "
                f"(allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))})"
            )
            continue
        if size > MAX_FILE_BYTES:
            errors.append(f"{path}: larger than {MAX_FILE_BYTES} bytes")
        if text.startswith("#!"):
            errors.append(f"{path}: executable content (shebang) is not allowed")
        if _SECRET_RE.search(text):
            errors.append(f"{path}: contains what looks like a credential; remove it")
        if _BIDI_RE.search(text):
            errors.append(f"{path}: contains hidden bidirectional control characters")
        if _ZERO_WIDTH_RE.search(text):
            strict(f"{path}: contains zero-width characters that can hide instructions")
        for pattern in _INJECTION_RE:
            match = pattern.search(text)
            if match:
                strict(
                    f"{path}: instruction-override/exfiltration wording: '{match.group(0)[:80]}'"
                )
        for url in _URL_RE.findall(text):
            host = urlsplit(url).hostname
            if host and host not in allowed_hosts:
                strict(
                    f"{path}: references undeclared endpoint {host}; list it in metadata.endpoints"
                )
        if _BASE64_RE.search(text):
            strict(f"{path}: contains a long encoded blob")
    if total > MAX_PACKAGE_BYTES:
        errors.append(f"package larger than {MAX_PACKAGE_BYTES} bytes")
    return ValidationReport(passed=not errors, errors=errors, warnings=warnings)
