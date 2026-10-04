"""Workflow service: the deterministic workflow authority for Milestone 1.

    create -> PLANNING -> (Requirement Analyzer)
                           |-- needs clarification -> AWAITING_CLARIFICATION
                           |       user answers all questions -> PLANNING (version + 1)
                           '-- finalized -> conditional routing -> PLANNED

Every state change is committed atomically with its outbox events.
"""

from __future__ import annotations

import logging
from datetime import datetime

from forgeflow.agents.context import AgentRuntimeContext
from forgeflow.core.config import Settings
from forgeflow.core.errors import AllProvidersFailed, ConcurrencyConflict, ValidationFailed
from forgeflow.core.ids import new_id, utcnow
from forgeflow.core.logging import bind_context, log_event
from forgeflow.platform.orchestration.execution import TaskChanges
from forgeflow.platform.orchestration.gateway import AgentGateway, AgentOutcome, AnalysisRequest
from forgeflow.platform.orchestration.repositories import RepositoryResolver
from forgeflow.platform.orchestration.routing import plan_route
from forgeflow.platform.orchestration.state_machine import TERMINAL, ensure_transition
from forgeflow.platform.state.store import Commit, WorkflowStore
from forgeflow.schemas.events import Event, EventType, Topics
from forgeflow.schemas.requirement import (
    CUSTOM_OPTION_ID,
    AcceptanceCriterion,
    AnalyzerResult,
    ChecklistItem,
    ClarificationAnswer,
    ClarificationQuestion,
    QuestionOption,
    QuestionStatus,
    RequirementScope,
    RequirementSpecification,
    SpecStatus,
)
from forgeflow.schemas.task import TaskStatus
from forgeflow.schemas.workflow import AgentRunRecord, Workflow, WorkflowStatus

logger = logging.getLogger(__name__)

MAX_REQUEST_CHARS = 10_000
MAX_CUSTOM_ANSWER_CHARS = 2_000
OPTION_IDS = ("A", "B", "C")


class WorkflowService:
    def __init__(
        self,
        store: WorkflowStore,
        gateway: AgentGateway,
        repositories: RepositoryResolver,
        settings: Settings,
    ) -> None:
        self.store = store
        self.gateway = gateway
        self.repositories = repositories
        self.settings = settings

    # ------------------------------------------------------------------ create

    async def create_workflow(self, request: str, repository_path: str | None) -> Workflow:
        request = request.strip()
        if not request:
            raise ValidationFailed("request must not be empty")
        if len(request) > MAX_REQUEST_CHARS:
            raise ValidationFailed(f"request exceeds {MAX_REQUEST_CHARS} characters")
        repo = self.repositories.validate(repository_path) if repository_path else None

        now = utcnow()
        wf = Workflow(
            workflow_id=new_id("wf"),
            request=request,
            repository_path=repo,
            status=WorkflowStatus.CREATED,
            created_at=now,
            updated_at=now,
        )
        events = [
            self._event(wf, EventType.WORKFLOW_CREATED, request=request, repository_path=repo)
        ]
        self._transition(wf, WorkflowStatus.PLANNING, events)
        events.append(self._event(wf, EventType.ANALYSIS_REQUESTED, requirement_version=1))
        stored = (
            await self.store.commit(Commit(workflow=wf, create_workflow=True, events=events))
        ).workflow
        assert stored is not None
        log_event(logger, "workflow created", workflow_id=wf.workflow_id)
        return stored

    # ---------------------------------------------------------------- analysis

    async def process_analysis(self, workflow_id: str, requirement_version: int) -> Workflow | None:
        """Run intake + requirement analysis. Idempotent: stale requests are ignored."""
        bind_context(workflow_id=workflow_id)
        wf = await self.store.get_workflow(workflow_id)
        if (
            wf.status != WorkflowStatus.PLANNING
            or requirement_version != wf.requirement_version + 1
        ):
            log_event(
                logger,
                "analysis request ignored (stale or duplicate)",
                status=str(wf.status),
                requested_version=requirement_version,
                current_version=wf.requirement_version,
            )
            return None
        revision = wf.revision
        ctx = AgentRuntimeContext(
            workflow_id=workflow_id,
            repository_root=self.repositories.resolve(wf.repository_path),
        )
        runs: list[AgentRunRecord] = []
        events: list[Event] = []
        current_agent = "orchestrator"

        try:
            if wf.intake is None:
                started = utcnow()
                intake = await self.gateway.assess_intake(
                    ctx, wf.request, wf.repository_path is not None
                )
                wf.intake = intake.output
                runs.append(self._run_record(wf, "orchestrator", intake, started))
                if not wf.intake.is_engineering_request:
                    return await self._fail(
                        wf,
                        revision,
                        "The request is not a software-engineering task.",
                        runs,
                        events,
                    )

            current_agent = "requirement_analyzer"
            questions = await self.store.list_questions(workflow_id)
            answered = [q for q in questions if q.status == QuestionStatus.ANSWERED]
            must_finalize = wf.clarification_round >= self.settings.max_clarification_rounds
            started = utcnow()
            analysis = await self.gateway.analyze_requirements(
                ctx,
                AnalysisRequest(
                    request=wf.request,
                    intake=wf.intake,
                    repository_attached=wf.repository_path is not None,
                    previous_specification=await self.store.get_specification(workflow_id),
                    answered_questions=answered,
                    must_finalize=must_finalize,
                    max_questions=self.settings.max_questions_per_round,
                ),
            )
            runs.append(self._run_record(wf, "requirement_analyzer", analysis, started))
        except AllProvidersFailed as exc:
            runs.append(self._failed_run(wf, current_agent, str(exc), exc.attempts))
            return await self._fail(wf, revision, str(exc), runs, events)
        except Exception as exc:
            logger.exception("agent run failed")
            runs.append(self._failed_run(wf, current_agent, repr(exc), []))
            return await self._fail(
                wf, revision, f"Requirement analysis failed: {exc}", runs, events
            )

        result = analysis.output
        if result.outcome == "needs_clarification" and must_finalize:
            return await self._fail(
                wf,
                revision,
                "The Requirement Analyzer still needed clarification after "
                f"{self.settings.max_clarification_rounds} round(s).",
                runs,
                events,
            )

        for run in runs:
            events.append(self._run_event(wf, run))
        spec, new_questions = self._build_specification(wf, requirement_version, result, answered)
        wf.requirement_version = requirement_version
        events.append(
            self._event(
                wf, EventType.SPECIFICATION_CREATED, version=spec.version, status=str(spec.status)
            )
        )
        if new_questions:
            wf.clarification_round += 1
            events.append(
                self._event(
                    wf,
                    EventType.CLARIFICATION_REQUESTED,
                    question_ids=[q.question_id for q in new_questions],
                    round=wf.clarification_round,
                )
            )
            self._transition(wf, WorkflowStatus.AWAITING_CLARIFICATION, events)
        else:
            wf.route_plan = plan_route(spec.required_capabilities)
            events.append(
                self._event(
                    wf,
                    EventType.WORKFLOW_ROUTED,
                    stages=[s.agent for s in wf.route_plan.stages],
                    skipped=list(wf.route_plan.skipped),
                )
            )
            self._transition(wf, WorkflowStatus.PLANNED, events)

        return await self._commit_or_drop(
            Commit(
                workflow=wf,
                expected_revision=revision,
                specifications=[spec],
                questions=new_questions,
                agent_runs=runs,
                events=events,
            )
        )

    def _build_specification(
        self,
        wf: Workflow,
        version: int,
        result: AnalyzerResult,
        answered: list[ClarificationQuestion],
    ) -> tuple[RequirementSpecification, list[ClarificationQuestion]]:
        now = utcnow()
        questions: list[ClarificationQuestion] = []
        if result.outcome == "needs_clarification":
            existing = len(answered)
            for offset, q in enumerate(result.questions[: self.settings.max_questions_per_round]):
                options = [
                    QuestionOption(
                        id=OPTION_IDS[i],
                        label=o.label,
                        description=o.description,
                        recommended=o.recommended,
                        reason=o.reason,
                    )
                    for i, o in enumerate(q.options)
                ]
                options.append(QuestionOption(id=CUSTOM_OPTION_ID, label="Custom answer"))
                questions.append(
                    ClarificationQuestion(
                        question_id=f"{wf.workflow_id}.Q-{existing + offset + 1:03d}",
                        workflow_id=wf.workflow_id,
                        requirement_version=version,
                        round=wf.clarification_round + 1,
                        status=QuestionStatus.AWAITING_USER,
                        question=q.question,
                        why_it_matters=q.why_it_matters,
                        options=options,
                        created_at=now,
                    )
                )
        spec = RequirementSpecification(
            requirement_id=f"req_{wf.workflow_id.removeprefix('wf_')}",
            workflow_id=wf.workflow_id,
            version=version,
            summary=result.summary,
            goal=result.goal,
            scope=RequirementScope(in_scope=result.in_scope, out_of_scope=result.out_of_scope),
            constraints=result.constraints,
            assumptions=result.assumptions,
            checklist=[
                ChecklistItem(id=f"CHK-{i:03d}", description=d)
                for i, d in enumerate(result.checklist, 1)
            ],
            acceptance_criteria=[
                AcceptanceCriterion(
                    id=f"AC-{i:03d}", description=ac.description, verification=ac.verification
                )
                for i, ac in enumerate(result.acceptance_criteria, 1)
            ],
            required_capabilities=result.required_capabilities,
            external_systems=result.external_systems,
            requires_human_approval=result.requires_human_approval,
            risk_level=result.risk_level,
            repository_observations=result.repository_observations,
            clarifications=[q.question_id for q in answered],
            status=SpecStatus.AWAITING_CLARIFICATION if questions else SpecStatus.FINALIZED,
            created_at=now,
        )
        return spec, questions

    # ----------------------------------------------------------- clarification

    async def answer_question(
        self, question_id: str, selected_option: str, custom_text: str | None
    ) -> tuple[ClarificationQuestion, Workflow]:
        question = await self.store.get_question(question_id)
        wf = await self.store.get_workflow(question.workflow_id)
        bind_context(workflow_id=wf.workflow_id)
        if wf.status != WorkflowStatus.AWAITING_CLARIFICATION:
            raise ValidationFailed(f"workflow is {wf.status}, not awaiting clarification")
        if question.status != QuestionStatus.AWAITING_USER:
            raise ValidationFailed("question has already been answered")
        if selected_option not in {o.id for o in question.options}:
            raise ValidationFailed(f"unknown option: {selected_option}")
        custom = (custom_text or "").strip() or None
        if selected_option == CUSTOM_OPTION_ID:
            if not custom:
                raise ValidationFailed("custom_text is required for a custom answer")
            if len(custom) > MAX_CUSTOM_ANSWER_CHARS:
                raise ValidationFailed(f"custom_text exceeds {MAX_CUSTOM_ANSWER_CHARS} characters")
        else:
            custom = None

        revision = wf.revision
        question.status = QuestionStatus.ANSWERED
        question.answer = ClarificationAnswer(
            selected_option=selected_option, custom_text=custom, answered_at=utcnow()
        )
        events = [
            self._event(
                wf,
                EventType.CLARIFICATION_ANSWERED,
                question_id=question_id,
                selected_option=selected_option,
            )
        ]
        pending = [
            q
            for q in await self.store.list_questions(wf.workflow_id)
            if q.status == QuestionStatus.AWAITING_USER and q.question_id != question_id
        ]
        if not pending:
            self._transition(wf, WorkflowStatus.PLANNING, events)
            events.append(
                self._event(
                    wf, EventType.ANALYSIS_REQUESTED, requirement_version=wf.requirement_version + 1
                )
            )
        else:
            wf.updated_at = utcnow()
        stored = (
            await self.store.commit(
                Commit(workflow=wf, expected_revision=revision, questions=[question], events=events)
            )
        ).workflow
        assert stored is not None
        return question, stored

    # ------------------------------------------------------------------ cancel

    async def cancel_workflow(self, workflow_id: str) -> Workflow:
        wf = await self.store.get_workflow(workflow_id)
        if wf.status in TERMINAL:
            raise ValidationFailed(f"workflow is already {wf.status}")
        revision = wf.revision
        events: list[Event] = []
        self._transition(wf, WorkflowStatus.CANCELLED, events)
        # Running tasks notice the cancellation through their heartbeat and stop.
        changes = TaskChanges()
        for task in await self.store.list_tasks(workflow_id):
            if task.status not in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
                changes.move(
                    task,
                    TaskStatus.CANCELLED,
                    EventType.TASK_CANCELLED,
                    utcnow(),
                    reason="workflow cancelled",
                )
        stored = (
            await self.store.commit(
                Commit(
                    workflow=wf,
                    expected_revision=revision,
                    tasks=changes.task_writes(),
                    events=events + changes.events,
                )
            )
        ).workflow
        assert stored is not None
        return stored

    # ----------------------------------------------------------------- helpers

    def _transition(self, wf: Workflow, target: WorkflowStatus, events: list[Event]) -> None:
        ensure_transition(wf.status, target)
        previous = wf.status
        wf.status = target
        wf.updated_at = utcnow()
        events.append(
            self._event(
                wf, EventType.WORKFLOW_STATUS_CHANGED, previous=str(previous), current=str(target)
            )
        )

    async def _fail(
        self,
        wf: Workflow,
        revision: int,
        message: str,
        runs: list[AgentRunRecord],
        events: list[Event],
    ) -> Workflow | None:
        log_event(logger, "workflow failed", logging.ERROR, error=message)
        for run in runs:
            events.append(self._run_event(wf, run))
        wf.error = message
        events.append(self._event(wf, EventType.WORKFLOW_FAILED, error=message))
        self._transition(wf, WorkflowStatus.FAILED, events)
        return await self._commit_or_drop(
            Commit(workflow=wf, expected_revision=revision, agent_runs=runs, events=events)
        )

    async def _commit_or_drop(self, change: Commit) -> Workflow | None:
        try:
            return (await self.store.commit(change)).workflow
        except ConcurrencyConflict:
            # Another actor (e.g. a cancel) changed the workflow while the agents ran.
            log_event(
                logger, "analysis result dropped: workflow changed concurrently", logging.WARNING
            )
            return None

    @staticmethod
    def _event(wf: Workflow, event_type: str, topic: str = Topics.WORKFLOW, **payload) -> Event:
        return Event(
            event_type=event_type, topic=topic, workflow_id=wf.workflow_id, payload=payload
        )

    def _run_event(self, wf: Workflow, run: AgentRunRecord) -> Event:
        return self._event(
            wf,
            EventType.AGENT_RUN_COMPLETED
            if run.status == "completed"
            else EventType.AGENT_RUN_FAILED,
            topic=Topics.AGENT,
            run_id=run.run_id,
            agent_type=run.agent_type,
            prompt_version=run.prompt_version,
            providers=[f"{a.provider}:{a.model}:{a.status}" for a in run.attempts],
            tool_calls=len(run.tool_calls),
            error=run.error,
        )

    @staticmethod
    def _run_record(
        wf: Workflow, agent_type: str, outcome: AgentOutcome, started: datetime
    ) -> AgentRunRecord:
        return AgentRunRecord(
            run_id=new_id("run"),
            workflow_id=wf.workflow_id,
            agent_type=agent_type,
            prompt_version=outcome.prompt_version,
            status="completed",
            attempts=outcome.attempts,
            tool_calls=outcome.tool_calls,
            started_at=started,
            completed_at=utcnow(),
        )

    @staticmethod
    def _failed_run(wf: Workflow, agent_type: str, error: str, attempts: list) -> AgentRunRecord:
        now = utcnow()
        return AgentRunRecord(
            run_id=new_id("run"),
            workflow_id=wf.workflow_id,
            agent_type=agent_type,
            prompt_version="unknown",
            status="failed",
            attempts=attempts,
            error=error,
            started_at=now,
            completed_at=now,
        )
