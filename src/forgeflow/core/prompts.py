"""Loads version-controlled agent prompts (spec section 40)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_VERSION_RE = re.compile(r"^Prompt Version:\s*(\S+)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Prompt:
    text: str
    version: str


@lru_cache
def load_prompt(path: Path) -> Prompt:
    text = path.read_text(encoding="utf-8")
    match = _VERSION_RE.search(text)
    if match is None:
        raise ValueError(f"prompt is missing a 'Prompt Version:' line: {path}")
    return Prompt(text=text, version=match.group(1))
