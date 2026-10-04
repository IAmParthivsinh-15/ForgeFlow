"""Requirement Specification contract (spec sections 171-175).

`AnalyzerResult` is what the Requirement Analyzer agent returns. It carries no
identifiers: ForgeFlow assigns requirement ids, versions, checklist/AC ids and
question ids deterministically when it persists the result as a
`RequirementSpecification` and `ClarificationQuestion` documents.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

RiskLevel = Literal["low", "medium", "high", "critical"]
Verification = Literal[
    "unit_test",
    "integration_test",
    "api_test",
    "e2e_test",
    "smoke_test",
    "manual_review",
    "code_review",
    "security_scan",
    "ci_pipeline",
]


class RequiredCapabilities(BaseModel):
    development: bool = False
    qa: bool = False
    code_review: bool = False
    security: bool = False
    ci: bool = False


# ---------------------------------------------------------------------------
# Agent output
# ---------------------------------------------------------------------------


class AnalyzerOption(BaseModel):
    label: str = Field(description="Short, concrete answer the user can pick.")
    description: str = Field(default="", description="What choosing this option implies.")
    recommended: bool = False
    reason: str | None = Field(
        default=None, description="Why this is recommended. Required for the recommended option."
    )


class AnalyzerQuestion(BaseModel):
    question: str
    why_it_matters: str = Field(description="What would be wrong if ForgeFlow guessed.")
    options: list[AnalyzerOption] = Field(min_length=2, max_length=3)

    @model_validator(mode="after")
    def _exactly_one_recommendation(self) -> AnalyzerQuestion:
        recommended = [o for o in self.options if o.recommended]
        if len(recommended) != 1:
            raise ValueError("exactly one option must have recommended=true")
        if not (recommended[0].reason or "").strip():
            raise ValueError("the recommended option must include a reason")
        return self


class AnalyzerAcceptanceCriterion(BaseModel):
    description: str
    verification: Verification


class AnalyzerResult(BaseModel):
    outcome: Literal["finalized", "needs_clarification"]
    summary: str
    goal: str
    in_scope: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    checklist: list[str] = Field(default_factory=list)
    acceptance_criteria: list[AnalyzerAcceptanceCriterion] = Field(default_factory=list)
    required_capabilities: RequiredCapabilities
    external_systems: list[str] = Field(default_factory=list)
    requires_human_approval: bool = False
    risk_level: RiskLevel = "medium"
    repository_observations: list[str] = Field(
        default_factory=list, description="Concrete facts found while inspecting the repository."
    )
    questions: list[AnalyzerQuestion] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent_outcome(self) -> AnalyzerResult:
        if self.outcome == "needs_clarification" and not self.questions:
            raise ValueError("needs_clarification requires at least one question")
        if self.outcome == "finalized":
            if self.questions:
                raise ValueError("a finalized specification must not contain open questions")
            if not self.checklist:
                raise ValueError("a finalized specification requires a checklist")
            if not self.acceptance_criteria:
                raise ValueError("a finalized specification requires acceptance criteria")
        return self


# ---------------------------------------------------------------------------
# Persisted documents
# ---------------------------------------------------------------------------


class ChecklistItem(BaseModel):
    id: str
    description: str
    status: Literal["pending", "done", "skipped"] = "pending"


class AcceptanceCriterion(BaseModel):
    id: str
    description: str
    verification: Verification


class RequirementScope(BaseModel):
    in_scope: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)


class SpecStatus(StrEnum):
    AWAITING_CLARIFICATION = "awaiting_clarification"
    FINALIZED = "finalized"


class RequirementSpecification(BaseModel):
    requirement_id: str
    workflow_id: str
    version: int
    summary: str
    goal: str
    scope: RequirementScope
    constraints: list[str]
    assumptions: list[str]
    checklist: list[ChecklistItem]
    acceptance_criteria: list[AcceptanceCriterion]
    required_capabilities: RequiredCapabilities
    external_systems: list[str]
    requires_human_approval: bool
    risk_level: RiskLevel
    repository_observations: list[str]
    clarifications: list[str] = Field(
        default_factory=list, description="question_ids whose answers informed this version"
    )
    status: SpecStatus
    created_at: datetime


class QuestionStatus(StrEnum):
    AWAITING_USER = "awaiting_user"
    ANSWERED = "answered"


CUSTOM_OPTION_ID = "CUSTOM"


class QuestionOption(BaseModel):
    id: str
    label: str
    description: str = ""
    recommended: bool = False
    reason: str | None = None


class ClarificationAnswer(BaseModel):
    selected_option: str
    custom_text: str | None = None
    answered_at: datetime | None = None

    @field_validator("custom_text")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return v.strip() if v is not None else None


class ClarificationQuestion(BaseModel):
    question_id: str
    workflow_id: str
    requirement_version: int
    round: int
    status: QuestionStatus
    question: str
    why_it_matters: str
    options: list[QuestionOption]
    answer: ClarificationAnswer | None = None
    created_at: datetime

    def answer_text(self) -> str:
        """Human-readable answer used when feeding answers back to the analyzer."""
        if self.answer is None:
            return ""
        if self.answer.selected_option == CUSTOM_OPTION_ID:
            return self.answer.custom_text or ""
        for option in self.options:
            if option.id == self.answer.selected_option:
                return f"{option.label}" + (
                    f" - {option.description}" if option.description else ""
                )
        return self.answer.selected_option
