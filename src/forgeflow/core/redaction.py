"""Credential patterns shared by skill validation, indexing and tracing."""

from __future__ import annotations

import re

SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
    r"|nvapi-[A-Za-z0-9_-]{20,}|gsk_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|xox[bp]-[A-Za-z0-9-]{10,})"
)


def redact(text: str) -> str:
    return SECRET_RE.sub("[REDACTED]", text)
