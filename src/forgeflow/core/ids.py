from __future__ import annotations

import secrets
from datetime import UTC, datetime


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def utcnow() -> datetime:
    return datetime.now(UTC)
