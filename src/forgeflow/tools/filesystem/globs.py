"""Path-glob semantics shared by scope enforcement and conflict detection.

**   any number of path segments (including none)
*    any characters within one segment
?    one character within one segment
dir/ or dir/**   everything under dir
"""

from __future__ import annotations

import re
from functools import lru_cache

_WILDCARDS = "*?["


def normalise(pattern: str) -> str:
    p = pattern.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    if p.endswith("/"):
        p += "**"
    return p


@lru_cache(maxsize=1024)
def _regex(pattern: str) -> re.Pattern[str]:
    p = normalise(pattern)
    out: list[str] = []
    i = 0
    while i < len(p):
        c = p[i]
        if p.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif p.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out) + r"\Z")


def matches(path: str, pattern: str) -> bool:
    return _regex(pattern).match(normalise(path)) is not None


def matches_any(path: str, patterns: list[str]) -> bool:
    return any(matches(path, p) for p in patterns)


def literal_prefix(pattern: str) -> str:
    """Characters before the first wildcard, e.g. 'src/auth/' for 'src/auth/**'."""
    p = normalise(pattern)
    for i, c in enumerate(p):
        if c in _WILDCARDS:
            return p[:i]
    return p


def patterns_may_overlap(a: str, b: str) -> bool:
    """Conservative: True unless the two patterns provably address disjoint paths."""
    pa, pb = literal_prefix(a), literal_prefix(b)
    if pa == normalise(a) and pb == normalise(b):  # two concrete paths
        return pa == pb or pa.startswith(pb + "/") or pb.startswith(pa + "/")
    if pa == normalise(a):  # a is a concrete path, b is a glob
        return matches(pa, b) or pb.startswith(pa + "/") or pa.startswith(pb)
    if pb == normalise(b):
        return matches(pb, a) or pa.startswith(pb + "/") or pb.startswith(pa)
    return pa.startswith(pb) or pb.startswith(pa)
