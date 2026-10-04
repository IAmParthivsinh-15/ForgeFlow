import pytest
from pydantic import ValidationError

from forgeflow.schemas.requirement import (
    AnalyzerAcceptanceCriterion,
    AnalyzerOption,
    AnalyzerQuestion,
    AnalyzerResult,
    RequiredCapabilities,
)


def option(label, recommended=False, reason=None):
    return AnalyzerOption(label=label, recommended=recommended, reason=reason)


def test_question_requires_exactly_one_recommendation_with_reason():
    AnalyzerQuestion(
        question="q", why_it_matters="w", options=[option("a", True, "because"), option("b")]
    )
    with pytest.raises(ValidationError, match="exactly one"):
        AnalyzerQuestion(question="q", why_it_matters="w", options=[option("a"), option("b")])
    with pytest.raises(ValidationError, match="reason"):
        AnalyzerQuestion(question="q", why_it_matters="w", options=[option("a", True), option("b")])


@pytest.mark.parametrize("count", [1, 4])
def test_question_needs_two_or_three_options(count):
    options = [option("a", True, "r")] + [option(str(i)) for i in range(count - 1)]
    with pytest.raises(ValidationError):
        AnalyzerQuestion(question="q", why_it_matters="w", options=options)


def base(**overrides):
    data = dict(
        outcome="finalized",
        summary="s",
        goal="g",
        checklist=["do it"],
        acceptance_criteria=[
            AnalyzerAcceptanceCriterion(description="d", verification="unit_test")
        ],
        required_capabilities=RequiredCapabilities(development=True),
    )
    data.update(overrides)
    return data


def test_finalized_requires_checklist_and_acceptance_criteria():
    AnalyzerResult(**base())
    with pytest.raises(ValidationError, match="checklist"):
        AnalyzerResult(**base(checklist=[]))
    with pytest.raises(ValidationError, match="acceptance criteria"):
        AnalyzerResult(**base(acceptance_criteria=[]))


def test_needs_clarification_requires_questions():
    with pytest.raises(ValidationError, match="at least one question"):
        AnalyzerResult(**base(outcome="needs_clarification"))
