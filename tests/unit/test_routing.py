from forgeflow.platform.orchestration.routing import plan_route
from forgeflow.schemas.requirement import RequiredCapabilities as Caps


def deps(plan):
    by_id = {s.stage_id: s.agent for s in plan.stages}
    return {s.agent: sorted(by_id[d] for d in s.depends_on) for s in plan.stages}


def test_full_route_orders_review_and_security_in_parallel_before_qa_then_ci():
    plan = plan_route(Caps(development=True, code_review=True, security=True, qa=True, ci=True))
    assert [s.agent for s in plan.stages] == ["developer", "code_review", "security", "qa", "ci"]
    assert deps(plan) == {
        "developer": [],
        "code_review": ["developer"],
        "security": ["developer"],
        "qa": ["code_review", "security"],
        "ci": ["qa"],
    }
    assert plan.skipped == []
    assert "developer -> code_review + security -> qa -> ci" in plan.rationale


def test_pure_code_review_has_no_developer():
    plan = plan_route(Caps(code_review=True, security=True))
    assert [s.agent for s in plan.stages] == ["code_review", "security"]
    assert all(s.depends_on == [] for s in plan.stages)
    assert set(plan.skipped) == {"development", "qa", "ci"}


def test_qa_only_and_ci_only():
    assert [s.agent for s in plan_route(Caps(qa=True)).stages] == ["qa"]
    assert [s.agent for s in plan_route(Caps(ci=True)).stages] == ["ci"]


def test_qa_depends_on_development_when_no_review():
    plan = plan_route(Caps(development=True, qa=True))
    assert deps(plan) == {"developer": [], "qa": ["developer"]}


def test_ci_without_qa_depends_on_review_layer():
    plan = plan_route(Caps(development=True, security=True, ci=True))
    assert deps(plan)["ci"] == ["security"]


def test_no_capabilities_yields_empty_plan():
    plan = plan_route(Caps())
    assert plan.stages == []
    assert "No execution capabilities" in plan.rationale


def test_all_capabilities_are_executable():
    plan = plan_route(Caps(development=True, code_review=True, security=True, qa=True, ci=True))
    assert all(s.implemented for s in plan.stages)
