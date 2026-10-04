"""Change Analyzer (spec sections 53, 54, 189).

Deterministic, path-based classification of a change into review domains. It
decides which reviewers a change needs (a change should not trigger every
reviewer) and what the Code Review agent should focus on.
"""

from __future__ import annotations

import re
from typing import Literal

from forgeflow.schemas.verification import ChangeAnalysis

# (domain, path regex). A file can belong to several domains.
_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "auth",
        re.compile(
            r"(auth|login|logout|password|passwd|session|token|oauth|jwt|"
            r"permission|rbac|acl|credential|crypto|secret)",
            re.I,
        ),
    ),
    (
        "database",
        re.compile(
            r"(migrations?/|alembic/|\.sql$|schema\.prisma$|/models?/|"
            r"models?\.py$)",
            re.I,
        ),
    ),
    (
        "infra",
        re.compile(
            r"(^|/)(dockerfile|docker-compose[^/]*\.ya?ml|jenkinsfile)$|"
            r"(^|/)(k8s|kubernetes|helm|charts|terraform|infra|deploy)/|"
            r"\.tf$|^\.github/workflows/",
            re.I,
        ),
    ),
    (
        "dependencies",
        re.compile(
            r"(^|/)(package(-lock)?\.json|yarn\.lock|pnpm-lock\.yaml|"
            r"requirements[^/]*\.txt|pyproject\.toml|poetry\.lock|"
            r"uv\.lock|go\.(mod|sum)|pom\.xml|build\.gradle)$",
            re.I,
        ),
    ),
    ("tests", re.compile(r"(^|/)(tests?|__tests__|spec)/|(_test|\.test|\.spec|test_)[^/]*$", re.I)),
    (
        "frontend",
        re.compile(
            r"(\.(tsx|jsx|vue|svelte|css|scss|html)$|(^|/)(frontend|web|ui|"
            r"client|components|pages)/)",
            re.I,
        ),
    ),
    ("docs", re.compile(r"(\.(md|rst|adoc|txt)$|(^|/)docs?/)", re.I)),
    ("backend", re.compile(r"\.(py|go|java|kt|rb|php|cs|rs|ts|js|mjs|cjs)$", re.I)),
]

# Domains whose changes need the Security agent (spec section 53).
SECURITY_DOMAINS = frozenset({"auth", "infra", "dependencies", "database"})
_FOCUS = {
    "auth": "authentication/authorization logic: check access control and session handling",
    "database": "database safety: migrations must be reversible and non-destructive",
    "infra": "DevOps review: container, pipeline and deployment configuration",
    "dependencies": "dependency changes: versions, licences, known vulnerabilities",
    "tests": "test quality: tests must assert the acceptance criteria, not just run",
    "frontend": "frontend: accessibility, input validation, error states",
}


def analyze_changes(files: list[str]) -> ChangeAnalysis:
    files_by_domain: dict[str, list[str]] = {}
    for path in files:
        matched = [domain for domain, pattern in _RULES if pattern.search(path)]
        # A test or doc file is not also "backend" just because of its extension.
        if ("tests" in matched or "docs" in matched) and "backend" in matched:
            matched.remove("backend")
        for domain in matched or ["other"]:
            files_by_domain.setdefault(domain, []).append(path)

    domains = sorted(files_by_domain)
    code_domains = set(domains) - {"docs"}
    reviewers = ["code_review"] if files else []
    if code_domains & SECURITY_DOMAINS:
        reviewers.append("security")
    risk: Literal["low", "medium", "high"]
    if code_domains & {"auth", "infra", "database"}:
        risk = "high"
    elif code_domains & {"backend", "frontend", "dependencies"}:
        risk = "medium"
    else:
        risk = "low"
    return ChangeAnalysis(
        files=files,
        domains=domains,
        files_by_domain=files_by_domain,
        risk=risk,
        reviewers=reviewers,
        review_focus=[_FOCUS[d] for d in domains if d in _FOCUS],
    )
