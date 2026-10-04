import pytest

from forgeflow.core.errors import (
    AllProvidersFailed,
    ConcurrencyConflict,
    PolicyViolation,
    ValidationFailed,
)
from forgeflow.platform.orchestration.fake_gateway import FakeAgentGateway
from forgeflow.platform.state.store import Commit
from forgeflow.schemas.events import EventType
from forgeflow.schemas.requirement import QuestionStatus, SpecStatus
from forgeflow.schemas.workflow import ProviderAttempt, WorkflowStatus


async def event_types(store, workflow_id):
    return [se.event.event_type for se in await store.list_events(workflow_id)]


async def test_create_moves_to_planning_and_requests_analysis(service, store):
    wf = await service.create_workflow("Add forgot-password flow", "demo-app")
    assert wf.status == WorkflowStatus.PLANNING
    assert wf.repository_path == "demo-app"
    assert await event_types(store, wf.workflow_id) == [
        EventType.WORKFLOW_CREATED,
        EventType.WORKFLOW_STATUS_CHANGED,
        EventType.ANALYSIS_REQUESTED,
    ]
    seqs = [se.seq for se in await store.list_events(wf.workflow_id)]
    assert seqs == [1, 2, 3]


async def test_full_clarification_loop_reaches_planned(service, store):
    wf = await service.create_workflow("Add forgot-password flow", "demo-app")

    wf = await service.process_analysis(wf.workflow_id, 1)
    assert wf.status == WorkflowStatus.AWAITING_CLARIFICATION
    assert wf.requirement_version == 1 and wf.clarification_round == 1
    spec_v1 = await store.get_specification(wf.workflow_id)
    assert spec_v1.status == SpecStatus.AWAITING_CLARIFICATION

    [question] = await store.list_questions(wf.workflow_id)
    assert question.question_id == f"{wf.workflow_id}.Q-001"
    assert [o.id for o in question.options] == ["A", "B", "CUSTOM"]
    assert sum(o.recommended for o in question.options) == 1

    _, wf = await service.answer_question(question.question_id, "CUSTOM", "Only via e-mail link")
    assert wf.status == WorkflowStatus.PLANNING

    wf = await service.process_analysis(wf.workflow_id, 2)
    assert wf.status == WorkflowStatus.PLANNED
    spec_v2 = await store.get_specification(wf.workflow_id)
    assert spec_v2.version == 2 and spec_v2.status == SpecStatus.FINALIZED
    assert spec_v2.clarifications == [question.question_id]
    assert any("Only via e-mail link" in a for a in spec_v2.assumptions)
    assert spec_v2.checklist[0].id == "CHK-001"
    assert spec_v2.acceptance_criteria[0].id == "AC-001"
    # "password" makes the fake analyzer require a security review.
    assert [s.agent for s in wf.route_plan.stages] == [
        "developer",
        "code_review",
        "security",
        "qa",
        "ci",
    ]
    assert len(await store.list_agent_runs(wf.workflow_id)) == 3  # intake + 2 analyses
    assert EventType.WORKFLOW_ROUTED in await event_types(store, wf.workflow_id)


async def test_review_request_routes_without_developer(service):
    wf = await service.create_workflow("Review PR #142 for correctness and security", None)
    wf = await service.process_analysis(wf.workflow_id, 1)
    assert wf.status == WorkflowStatus.PLANNED
    assert [s.agent for s in wf.route_plan.stages] == ["code_review", "security"]


async def test_duplicate_and_stale_analysis_requests_are_ignored(service, store):
    wf = await service.create_workflow("Run the CI pipeline", None)
    assert await service.process_analysis(wf.workflow_id, 1) is not None
    before = len(store.events)
    assert await service.process_analysis(wf.workflow_id, 1) is None
    assert await service.process_analysis(wf.workflow_id, 5) is None
    assert len(store.events) == before


async def test_answer_validation(service, store):
    wf = await service.create_workflow("Add a profile page", None)
    await service.process_analysis(wf.workflow_id, 1)
    [question] = await store.list_questions(wf.workflow_id)
    with pytest.raises(ValidationFailed, match="unknown option"):
        await service.answer_question(question.question_id, "Z", None)
    with pytest.raises(ValidationFailed, match="custom_text is required"):
        await service.answer_question(question.question_id, "CUSTOM", "   ")
    question, _ = await service.answer_question(question.question_id, "A", "ignored")
    assert question.status == QuestionStatus.ANSWERED
    assert question.answer.custom_text is None
    with pytest.raises(ValidationFailed):
        await service.answer_question(question.question_id, "A", None)


async def test_repository_path_must_stay_inside_repos_root(service):
    with pytest.raises(PolicyViolation):
        await service.create_workflow("x", "../../etc")
    with pytest.raises(ValidationFailed, match="not found"):
        await service.create_workflow("x", "missing-repo")


async def test_cancel_then_analysis_result_is_dropped(service, store):
    wf = await service.create_workflow("Add a profile page", None)
    await service.cancel_workflow(wf.workflow_id)
    assert await service.process_analysis(wf.workflow_id, 1) is None
    assert (await store.get_workflow(wf.workflow_id)).status == WorkflowStatus.CANCELLED
    with pytest.raises(ValidationFailed, match="already"):
        await service.cancel_workflow(wf.workflow_id)


async def test_provider_failure_fails_workflow_with_recorded_attempts(service, store):
    class FailingGateway(FakeAgentGateway):
        async def analyze_requirements(self, ctx, analysis):
            attempt = ProviderAttempt(
                provider="nvidia", model="m", status="failed", latency_ms=5, fallback_reason="boom"
            )
            raise AllProvidersFailed("all providers failed", [attempt])

    service.gateway = FailingGateway()
    wf = await service.create_workflow("Add a profile page", None)
    wf = await service.process_analysis(wf.workflow_id, 1)
    assert wf.status == WorkflowStatus.FAILED
    assert "all providers failed" in wf.error
    [_, failed] = await store.list_agent_runs(wf.workflow_id)
    assert failed.status == "failed" and failed.attempts[0].fallback_reason == "boom"


async def test_clarification_round_limit_forces_finalize(service, settings):
    settings.max_clarification_rounds = 0
    wf = await service.create_workflow("Add a profile page", None)
    wf = await service.process_analysis(wf.workflow_id, 1)
    # The fake analyzer honours must_finalize, so no question is asked.
    assert wf.status == WorkflowStatus.PLANNED


async def test_store_rejects_stale_revision(store, service):
    wf = await service.create_workflow("Run the CI pipeline", None)
    with pytest.raises(ConcurrencyConflict):
        await store.commit(Commit(workflow=wf, expected_revision=wf.revision - 1))
