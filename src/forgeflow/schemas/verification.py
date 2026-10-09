"""Verification contracts: change analysis, review, security, QA, CI, A2A, final report.

Spec sections 11, 13, 14, 50, 54, 56, 58, 186, 190, 191.

Agent outputs (`*Assessment` / `*Report` produced by an LLM) are kept separate from
what ForgeFlow decides deterministically (e.g. whether a result is *blocking*, the
CI build status, scanner findings), so an agent can never talk a failing check into
a pass.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["info", "low", "medium", "high", "critical"]
BLOCKING_SEVERITIES: frozenset[str] = frozenset({"high", "critical"})

OWASP_TOP_10_2021: dict[str, str] = {
    "A01": "Broken Access Control",
    "A02": "Cryptographic Failures",
    "A03": "Injection",
    "A04": "Insecure Design",
    "A05": "Security Misconfiguration",
    "A06": "Vulnerable and Outdated Components",
    "A07": "Identification and Authentication Failures",
    "A08": "Software and Data Integrity Failures",
    "A09": "Security Logging and Monitoring Failures",
    "A10": "Server-Side Request Forgery",
}


# ---------------------------------------------------------------- change analysis


class ChangeAnalysis(BaseModel):
    """Deterministic routing input (spec section 54)."""

    files: list[str]
    domains: list[str]
    files_by_domain: dict[str, list[str]]
    risk: Literal["low", "medium", "high"]
    reviewers: list[str]
    review_focus: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------------ review


class ReviewFinding(BaseModel):
    severity: Severity
    category: Literal[
        "correctness",
        "architecture",
        "maintainability",
        "readability",
        "test_coverage",
        "security",
        "performance",
        "api_compatibility",
        "database_safety",
        "requirements",
    ]
    file: str | None = None
    line: int | None = None
    message: str
    suggestion: str = ""


class ReviewReport(BaseModel):
    decision: Literal["approved", "changes_requested", "blocked"]
    summary: str = Field(description="Concrete summary; never just 'looks good'.")
    findings: list[ReviewFinding] = Field(default_factory=list)
    requirements_alignment: str = Field(
        default="", description="How the change satisfies (or misses) the acceptance criteria."
    )


# ---------------------------------------------------------------------- security


class ScannerFinding(BaseModel):
    tool: str
    rule_id: str
    severity: Severity
    category: str = Field(description="OWASP Top 10 id (e.g. A03) or 'unmapped'.")
    file: str | None = None
    line: int | None = None
    message: str
    evidence: str = ""


class ScanResult(BaseModel):
    tool: str
    status: Literal["completed", "unavailable", "error", "skipped"]
    findings: list[ScannerFinding] = Field(default_factory=list)
    detail: str = ""
    duration_ms: int = 0


class SecurityFinding(BaseModel):
    category: str = Field(description="OWASP Top 10 id, e.g. 'A01'.")
    severity: Severity
    file: str | None = None
    line: int | None = None
    evidence: str = Field(description="The code or configuration that shows the problem.")
    impact: str
    remediation: str
    source: str = Field(default="agent", description="'agent' or the scanner name.")


class OwaspCategoryResult(BaseModel):
    id: str
    status: Literal["pass", "fail", "not_applicable", "uncertain"]
    notes: str = ""


class SecurityAssessment(BaseModel):
    """What the Security agent returns."""

    summary: str
    categories: list[OwaspCategoryResult] = Field(default_factory=list)
    findings: list[SecurityFinding] = Field(default_factory=list)
    false_positives: list[str] = Field(
        default_factory=list, description="Scanner rule ids judged not applicable, with reason."
    )


class SecurityReport(BaseModel):
    """What ForgeFlow stores: agent reasoning + every scanner result."""

    owasp_edition: str
    summary: str
    categories: list[OwaspCategoryResult]
    findings: list[SecurityFinding]
    scanners: list[ScanResult]
    false_positives: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------- QA


class CriterionResult(BaseModel):
    id: str
    status: Literal["PASS", "FAIL", "UNCERTAIN"]
    evidence: str
    checks: list[str] = Field(default_factory=list, description="Commands that provide evidence.")
    artifacts: list[str] = Field(
        default_factory=list, description="Evidence artifact ids (e.g. browser screenshots)."
    )


class QAAssessment(BaseModel):
    summary: str
    criteria: list[CriterionResult]
    gaps: list[str] = Field(default_factory=list)


class QAReport(BaseModel):
    summary: str
    criteria: list[CriterionResult]
    gaps: list[str] = Field(default_factory=list)
    downgraded: list[str] = Field(
        default_factory=list, description="Criteria ForgeFlow downgraded for lack of evidence."
    )
    # Browser verification (Playwright MCP): where the app was served and what happened.
    browser_url: str | None = None
    browser_note: str | None = None
    browser_actions: int = 0
    artifacts: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------- CI


class CIStage(BaseModel):
    name: str
    status: str
    duration_ms: int = 0


class CIBuild(BaseModel):
    provider: str
    job: str
    build_number: int | None = None
    url: str | None = None
    status: Literal["SUCCESS", "FAILURE", "UNSTABLE", "ABORTED", "TIMEOUT"]
    stages: list[CIStage] = Field(default_factory=list)
    log_tail: str = ""
    duration_ms: int = 0
    commit: str


class CIAnalysis(BaseModel):
    """CI agent's diagnosis of a failed build."""

    failing_stage: str
    summary: str
    suspected_cause: str
    evidence: list[str] = Field(default_factory=list, description="Relevant log lines.")
    recommended_fix: str = ""


class CIReport(BaseModel):
    build: CIBuild
    pipeline: str = Field(description="The rendered pipeline definition that ran.")
    analysis: CIAnalysis | None = None


# --------------------------------------------------------------------------- A2A


class A2AAnswer(BaseModel):
    answer: str
    references: list[str] = Field(default_factory=list, description="Files or AC ids cited.")


class A2AMessage(BaseModel):
    """Recorded bounded exchange between two agents (spec section 186)."""

    message_id: str
    workflow_id: str
    task_id: str | None
    sender: str
    receiver: str
    message_type: Literal["question", "clarification", "failure_report"] = "question"
    request: str
    context: str = ""
    timeout_seconds: float
    status: Literal["answered", "timeout", "error", "refused"]
    response: str | None = None
    created_at: datetime
    answered_at: datetime | None = None


# ------------------------------------------------------------------ final report


class StageOutcome(BaseModel):
    stage: str
    verdict: Literal["pass", "fail", "uncertain", "not_run"]
    summary: str
    round: int = 0


class FinalReport(BaseModel):
    outcome: Literal["passed", "passed_with_accepted_risks", "failed", "completed"]
    summary: str
    stages: list[StageOutcome]
    acceptance_criteria: list[CriterionResult] = Field(default_factory=list)
    open_findings: list[str] = Field(default_factory=list)
    accepted_risks: list[str] = Field(default_factory=list)
    repair_rounds: int = 0
    files_changed: list[str] = Field(default_factory=list)
    branch: str | None = None
    commit: str | None = None
    ci_build_url: str | None = None
    pull_request_url: str | None = None
    pr_title: str = ""
    pr_body: str = ""
    generated_at: datetime
