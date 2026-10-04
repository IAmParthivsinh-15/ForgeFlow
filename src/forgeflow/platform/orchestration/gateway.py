"""Boundary between ForgeFlow orchestration and LLM agents.

The workflow service only depends on `AgentGateway`. `SdkAgentGateway` runs the
OpenAI Agents SDK agents against the configured provider chain;
`FakeAgentGateway` (fake_gateway.py) is deterministic and needs no provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from agents import AgentOutputSchema, Model

from forgeflow.agents.context import AgentRuntimeContext
from forgeflow.agents.orchestrator import agent as orchestrator
from forgeflow.agents.requirement_analyzer import agent as requirement_analyzer
from forgeflow.models.executor import AgentExecutor
from forgeflow.schemas.requirement import AnalyzerResult, ClarificationQuestion
from forgeflow.schemas.requirement import RequirementSpecification as Spec
from forgeflow.schemas.workflow import IntakeAssessment, ProviderAttempt


@dataclass
class AnalysisRequest:
    request: str
    intake: IntakeAssessment
    repository_attached: bool
    previous_specification: Spec | None
    answered_questions: list[ClarificationQuestion]
    must_finalize: bool
    max_questions: int


@dataclass
class AgentOutcome[T]:
    output: T
    prompt_version: str
    attempts: list[ProviderAttempt] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)


class AgentGateway(Protocol):
    async def assess_intake(
        self, ctx: AgentRuntimeContext, request: str, repository_attached: bool
    ) -> AgentOutcome[IntakeAssessment]: ...

    async def analyze_requirements(
        self, ctx: AgentRuntimeContext, analysis: AnalysisRequest
    ) -> AgentOutcome[AnalyzerResult]: ...


def render_analysis_input(analysis: AnalysisRequest) -> str:
    """Build the Requirement Analyzer's input message."""
    lines = [
        "# User request",
        analysis.request,
        "",
        "# Orchestrator intake",
        f"intent: {analysis.intake.intent}",
        f"summary: {analysis.intake.summary}",
    ]
    if analysis.intake.notes:
        lines.append(f"notes: {analysis.intake.notes}")
    lines += [
        "",
        "# Repository",
        "A repository is attached; inspect it with your tools."
        if analysis.repository_attached
        else "No repository is attached. Do not call tools.",
    ]
    if analysis.previous_specification is not None:
        prev = analysis.previous_specification
        lines += [
            "",
            f"# Previous specification (version {prev.version})",
            prev.model_dump_json(
                include={
                    "summary",
                    "goal",
                    "scope",
                    "constraints",
                    "assumptions",
                    "checklist",
                    "acceptance_criteria",
                    "required_capabilities",
                    "repository_observations",
                }
            ),
        ]
    if analysis.answered_questions:
        lines += ["", "# Clarifications answered by the user (authoritative)"]
        for q in analysis.answered_questions:
            lines.append(f"- Q: {q.question}\n  A: {q.answer_text()}")
    lines += ["", "# Instructions for this round"]
    if analysis.must_finalize:
        lines.append(
            "You MUST finalize now (outcome = finalized). Do not ask questions; record any "
            "remaining uncertainty as assumptions."
        )
    else:
        lines.append(
            f"Ask at most {analysis.max_questions} question(s), only if genuinely necessary."
        )
    return "\n".join(lines)


def render_intake_input(request: str, repository_attached: bool) -> str:
    return (
        f"# User request\n{request}\n\n# Repository\n"
        f"{'attached' if repository_attached else 'not attached'}"
    )


class SdkAgentGateway:
    def __init__(self, executor: AgentExecutor) -> None:
        self.executor = executor

    async def assess_intake(
        self, ctx: AgentRuntimeContext, request: str, repository_attached: bool
    ) -> AgentOutcome[IntakeAssessment]:
        def build(model: Model, output: AgentOutputSchema | None, extra: str):
            return orchestrator.create_orchestrator_agent(model, output, extra)

        run = await self.executor.run_structured(
            profile=orchestrator.MODEL_PROFILE,
            build_agent=build,
            input_text=render_intake_input(request, repository_attached),
            output_type=IntakeAssessment,
            context=ctx,
        )
        return AgentOutcome(run.output, orchestrator.prompt().version, run.attempts)

    async def analyze_requirements(
        self, ctx: AgentRuntimeContext, analysis: AnalysisRequest
    ) -> AgentOutcome[AnalyzerResult]:
        def build(model: Model, output: AgentOutputSchema | None, extra: str):
            return requirement_analyzer.create_requirement_analyzer_agent(
                model, output, extra, with_repository_tools=analysis.repository_attached
            )

        run = await self.executor.run_structured(
            profile=requirement_analyzer.MODEL_PROFILE,
            build_agent=build,
            input_text=render_analysis_input(analysis),
            output_type=AnalyzerResult,
            context=ctx,
        )
        return AgentOutcome(
            run.output, requirement_analyzer.prompt().version, run.attempts, list(ctx.tool_calls)
        )
