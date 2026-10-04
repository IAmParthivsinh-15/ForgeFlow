"""Deterministic AgentGateway for tests and FAKE_LLM=true demos.

Behaviour:
- intake: keyword-based intent classification;
- analysis: feature/bugfix requests get one clarification question in the first
  round; everything else (and every later round) is finalized immediately.
"""

from __future__ import annotations

from forgeflow.agents.context import AgentRuntimeContext
from forgeflow.platform.orchestration.gateway import AgentOutcome, AnalysisRequest
from forgeflow.schemas.requirement import (
    AnalyzerAcceptanceCriterion,
    AnalyzerOption,
    AnalyzerQuestion,
    AnalyzerResult,
    RequiredCapabilities,
)
from forgeflow.schemas.workflow import IntakeAssessment, Intent, ProviderAttempt

FAKE_PROMPT_VERSION = "fake-1"
_FAKE_ATTEMPT = ProviderAttempt(
    provider="fake", model="deterministic", status="succeeded", latency_ms=0
)

_KEYWORDS: list[tuple[Intent, tuple[str, ...]]] = [
    ("code_review", ("review pr", "review the pr", "pull request", "code review")),
    ("security", ("owasp", "security", "vulnerab")),
    ("qa", ("smoke test", "run tests", "regression", "qa ")),
    ("ci", ("ci pipeline", "jenkins", "build #", "run the ci")),
    ("bugfix", ("fix", "bug", "failing", "broken", "error")),
    ("investigation", ("investigate", "why ")),
]

_CAPABILITIES: dict[str, RequiredCapabilities] = {
    "feature": RequiredCapabilities(development=True, code_review=True, qa=True, ci=True),
    "bugfix": RequiredCapabilities(development=True, code_review=True, qa=True, ci=True),
    "code_review": RequiredCapabilities(code_review=True, security=True),
    "security": RequiredCapabilities(security=True),
    "qa": RequiredCapabilities(qa=True),
    "ci": RequiredCapabilities(ci=True),
    "investigation": RequiredCapabilities(),
    "other": RequiredCapabilities(),
}

_SECURITY_TERMS = ("auth", "password", "login", "token", "oauth", "secret", "permission")


def classify(request: str) -> Intent:
    lowered = request.lower()
    for intent, words in _KEYWORDS:
        if any(w in lowered for w in words):
            return intent
    return "feature"


class FakeAgentGateway:
    async def assess_intake(
        self, ctx: AgentRuntimeContext, request: str, repository_attached: bool
    ) -> AgentOutcome[IntakeAssessment]:
        intent = classify(request)
        summary = request.strip().rstrip(".")
        return AgentOutcome(
            IntakeAssessment(
                intent=intent, summary=summary, repository_required=repository_attached
            ),
            FAKE_PROMPT_VERSION,
            [_FAKE_ATTEMPT],
        )

    async def analyze_requirements(
        self, ctx: AgentRuntimeContext, analysis: AnalysisRequest
    ) -> AgentOutcome[AnalyzerResult]:
        intent = analysis.intake.intent
        capabilities = _CAPABILITIES[intent].model_copy()
        if any(t in analysis.request.lower() for t in _SECURITY_TERMS):
            capabilities.security = True
        ask = (
            intent in ("feature", "bugfix")
            and not analysis.answered_questions
            and not analysis.must_finalize
        )
        summary = analysis.intake.summary
        assumptions = [
            f"Clarified: {q.question} -> {q.answer_text()}" for q in analysis.answered_questions
        ]
        if not analysis.repository_attached:
            assumptions.append(
                "No repository attached; specification is based on the request only."
            )
        result = AnalyzerResult(
            outcome="needs_clarification" if ask else "finalized",
            summary=summary,
            goal=f"Deliver: {summary}",
            in_scope=[summary],
            out_of_scope=["Unrelated refactoring"],
            assumptions=assumptions,
            checklist=[f"Implement: {summary}", "Add or update automated tests"]
            if capabilities.development
            else [f"Perform: {summary}"],
            acceptance_criteria=[
                AnalyzerAcceptanceCriterion(
                    description=f"{summary} works as requested",
                    verification="integration_test"
                    if capabilities.development
                    else "manual_review",
                )
            ],
            required_capabilities=capabilities,
            risk_level="high" if capabilities.security else "medium",
            repository_observations=[],
            questions=[
                AnalyzerQuestion(
                    question="Should this change include automated end-to-end tests?",
                    why_it_matters="It changes the QA scope and the acceptance criteria.",
                    options=[
                        AnalyzerOption(
                            label="Yes - add end-to-end tests",
                            recommended=True,
                            reason="User-facing behaviour is best verified end to end.",
                        ),
                        AnalyzerOption(label="No - unit and integration tests only"),
                    ],
                )
            ]
            if ask
            else [],
        )
        return AgentOutcome(result, FAKE_PROMPT_VERSION, [_FAKE_ATTEMPT])
