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
from forgeflow.agents.developer import agent as developer
from forgeflow.agents.developer_subagent import agent as developer_subagent
from forgeflow.agents.integrator import agent as integrator
from forgeflow.agents.orchestrator import agent as orchestrator
from forgeflow.agents.requirement_analyzer import agent as requirement_analyzer
from forgeflow.models.executor import AgentExecutor
from forgeflow.schemas.requirement import AnalyzerResult, ClarificationQuestion
from forgeflow.schemas.requirement import RequirementSpecification as Spec
from forgeflow.schemas.task import (
    DevelopmentPlan,
    ImplementationReport,
    ResolutionReport,
    Task,
)
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
class DevelopmentRequest:
    specification: Spec
    max_subtasks: int


@dataclass
class ImplementationRequest:
    specification: Spec
    task: Task
    upstream: list[str] = field(default_factory=list)  # summaries of merged predecessors
    available_checks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ConflictRequest:
    specification: Spec
    conflicted_files: list[str]
    ours: list[str]  # titles/summaries already integrated
    theirs: str  # incoming task title + summary


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

    async def plan_development(
        self, ctx: AgentRuntimeContext, request: DevelopmentRequest
    ) -> AgentOutcome[DevelopmentPlan]: ...

    async def implement_subtask(
        self, ctx: AgentRuntimeContext, request: ImplementationRequest
    ) -> AgentOutcome[ImplementationReport]: ...

    async def resolve_conflicts(
        self, ctx: AgentRuntimeContext, request: ConflictRequest
    ) -> AgentOutcome[ResolutionReport]: ...


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


def _spec_brief(spec: Spec) -> str:
    return spec.model_dump_json(
        include={
            "summary",
            "goal",
            "scope",
            "constraints",
            "assumptions",
            "checklist",
            "acceptance_criteria",
            "repository_observations",
        }
    )


def render_development_input(req: DevelopmentRequest) -> str:
    return "\n".join(
        [
            "# Approved Requirement Specification",
            _spec_brief(req.specification),
            "",
            "# Instructions",
            f"Plan at most {req.max_subtasks} subtask(s). Inspect the repository first.",
        ]
    )


def render_implementation_input(req: ImplementationRequest) -> str:
    task = req.task
    criteria = {ac.id: ac.description for ac in req.specification.acceptance_criteria}
    mapped = [f"- {i}: {criteria.get(i, '(unknown id)')}" for i in task.acceptance_criteria]
    lines = [
        f"# Your subtask: {task.title}",
        task.instructions,
        "",
        "# File scope (you may write only here)",
        *[f"- {p}" for p in task.file_scope],
        "",
        "# Acceptance criteria for this subtask",
        *(mapped or ["- (none mapped)"]),
        "",
        "# Overall requirement",
        f"{req.specification.summary} - {req.specification.goal}",
    ]
    if req.upstream:
        lines += ["", "# Upstream subtasks already merged into your worktree"]
        lines += [f"- {u}" for u in req.upstream]
    if req.warnings:
        lines += ["", "# Workspace warnings", *[f"- {w}" for w in req.warnings]]
    lines += [
        "",
        "# Available checks",
        ", ".join(req.available_checks) if req.available_checks else "none configured",
    ]
    return "\n".join(lines)


def render_conflict_input(req: ConflictRequest) -> str:
    return "\n".join(
        [
            "# Conflicted files (you may write only these)",
            *[f"- {f}" for f in req.conflicted_files],
            "",
            "# Ours: already integrated",
            *([f"- {o}" for o in req.ours] or ["- base revision"]),
            "",
            "# Theirs: incoming subtask",
            req.theirs,
            "",
            "# Requirement",
            req.specification.summary,
        ]
    )


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

    async def plan_development(
        self, ctx: AgentRuntimeContext, request: DevelopmentRequest
    ) -> AgentOutcome[DevelopmentPlan]:
        def build(model: Model, output: AgentOutputSchema | None, extra: str):
            return developer.create_developer_agent(model, output, extra)

        run = await self.executor.run_structured(
            profile=developer.MODEL_PROFILE,
            build_agent=build,
            input_text=render_development_input(request),
            output_type=DevelopmentPlan,
            context=ctx,
        )
        return AgentOutcome(
            run.output, developer.prompt().version, run.attempts, list(ctx.tool_calls)
        )

    async def implement_subtask(
        self, ctx: AgentRuntimeContext, request: ImplementationRequest
    ) -> AgentOutcome[ImplementationReport]:
        def build(model: Model, output: AgentOutputSchema | None, extra: str):
            return developer_subagent.create_developer_subagent(
                model, output, extra, with_checks=ctx.checks is not None
            )

        run = await self.executor.run_structured(
            profile=developer_subagent.MODEL_PROFILE,
            build_agent=build,
            input_text=render_implementation_input(request),
            output_type=ImplementationReport,
            context=ctx,
        )
        return AgentOutcome(
            run.output, developer_subagent.prompt().version, run.attempts, list(ctx.tool_calls)
        )

    async def resolve_conflicts(
        self, ctx: AgentRuntimeContext, request: ConflictRequest
    ) -> AgentOutcome[ResolutionReport]:
        def build(model: Model, output: AgentOutputSchema | None, extra: str):
            return integrator.create_integrator_agent(model, output, extra)

        run = await self.executor.run_structured(
            profile=integrator.MODEL_PROFILE,
            build_agent=build,
            input_text=render_conflict_input(request),
            output_type=ResolutionReport,
            context=ctx,
        )
        return AgentOutcome(
            run.output, integrator.prompt().version, run.attempts, list(ctx.tool_calls)
        )
