"""Deterministic AgentGateway for tests and FAKE_LLM=true demos.

Behaviour:
- intake: keyword-based intent classification;
- analysis: feature/bugfix requests get one clarification question in the first
  round; everything else (and every later round) is finalized immediately;
- development: a three-subtask plan (backend + frontend in parallel, then docs that
  depends on both) under `forgeflow-demo/`;
- implementation: writes one small, real file per scope pattern;
- conflict resolution: keeps both sides of every conflict region.
"""

from __future__ import annotations

from forgeflow.agents.context import AgentRuntimeContext
from forgeflow.platform.orchestration.gateway import (
    AgentOutcome,
    AnalysisRequest,
    ConflictRequest,
    DevelopmentRequest,
    ImplementationRequest,
)
from forgeflow.schemas.requirement import (
    AnalyzerAcceptanceCriterion,
    AnalyzerOption,
    AnalyzerQuestion,
    AnalyzerResult,
    RequiredCapabilities,
)
from forgeflow.schemas.task import (
    DevelopmentPlan,
    ImplementationReport,
    ResolutionReport,
    SubtaskSpec,
)
from forgeflow.schemas.workflow import IntakeAssessment, Intent, ProviderAttempt
from forgeflow.tools.filesystem.globs import literal_prefix

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

    async def plan_development(
        self, ctx: AgentRuntimeContext, request: DevelopmentRequest
    ) -> AgentOutcome[DevelopmentPlan]:
        all_ac = [ac.id for ac in request.specification.acceptance_criteria]
        subtasks = [
            SubtaskSpec(
                key="backend",
                title="Backend changes",
                instructions="Implement the server-side part of the requirement.",
                file_scope=["forgeflow-demo/backend/**"],
                acceptance_criteria=all_ac,
            ),
            SubtaskSpec(
                key="frontend",
                title="Frontend changes",
                instructions="Implement the user-facing part of the requirement.",
                file_scope=["forgeflow-demo/frontend/**"],
                acceptance_criteria=all_ac,
            ),
            SubtaskSpec(
                key="docs",
                title="Document the change",
                instructions="Describe the new behaviour.",
                file_scope=["forgeflow-demo/CHANGES.md"],
                depends_on=["backend", "frontend"],
            ),
        ][: request.max_subtasks]
        for sub in subtasks:  # trimmed plans must not reference removed keys
            sub.depends_on = [d for d in sub.depends_on if d in {s.key for s in subtasks}]
        plan = DevelopmentPlan(summary=request.specification.summary, subtasks=subtasks)
        return AgentOutcome(plan, FAKE_PROMPT_VERSION, [_FAKE_ATTEMPT])

    async def implement_subtask(
        self, ctx: AgentRuntimeContext, request: ImplementationRequest
    ) -> AgentOutcome[ImplementationReport]:
        workspace = ctx.workspace()
        task = request.task
        written = []
        for pattern in task.file_scope:
            prefix = literal_prefix(pattern)
            path = f"{prefix}{task.key}.md" if prefix.endswith("/") or not prefix else prefix
            body = f"# {task.title}\n\n{request.specification.summary}\n"
            if path == prefix and workspace.resolve(path).is_file():
                body = workspace.resolve(path).read_text(encoding="utf-8") + body
            workspace.write_file(path, body)
            written.append(path)
        if ctx.checks is not None and "test" in request.available_checks:
            await ctx.checks.run("test")
        report = ImplementationReport(summary=f"{task.title}: wrote {', '.join(written)}")
        return AgentOutcome(report, FAKE_PROMPT_VERSION, [_FAKE_ATTEMPT])

    async def resolve_conflicts(
        self, ctx: AgentRuntimeContext, request: ConflictRequest
    ) -> AgentOutcome[ResolutionReport]:
        workspace = ctx.workspace()
        for path in request.conflicted_files:
            text = workspace.resolve(path).read_text(encoding="utf-8")
            kept = [
                line
                for line in text.splitlines(keepends=True)
                if not line.startswith(("<<<<<<<", "=======", ">>>>>>>"))
            ]
            workspace.write_file(path, "".join(kept))
        report = ResolutionReport(
            summary="Kept both sides of every conflict.", resolved_files=request.conflicted_files
        )
        return AgentOutcome(report, FAKE_PROMPT_VERSION, [_FAKE_ATTEMPT])
