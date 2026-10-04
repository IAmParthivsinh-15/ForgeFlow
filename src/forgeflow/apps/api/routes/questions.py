from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from forgeflow.apps.api.deps import ContainerDep
from forgeflow.schemas.requirement import ClarificationQuestion
from forgeflow.schemas.workflow import Workflow

router = APIRouter(prefix="/api/v1", tags=["clarifications"])


class AnswerRequest(BaseModel):
    selected_option: str
    custom_text: str | None = None


class AnswerResponse(BaseModel):
    question: ClarificationQuestion
    workflow: Workflow


@router.post("/questions/{question_id}/answer")
async def answer_question(question_id: str, body: AnswerRequest, c: ContainerDep) -> AnswerResponse:
    question, workflow = await c.service.answer_question(
        question_id, body.selected_option, body.custom_text
    )
    return AnswerResponse(question=question, workflow=workflow)
