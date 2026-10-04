"""Bounded agent-to-agent collaboration (spec sections 186-188).

An A2A exchange is a question from one specialist to another with a timeout. It
returns information only: the receiver cannot change workflow state, create or
cancel tasks, or approve anything (spec section 187). Every exchange is recorded
with sender, receiver, workflow, task, type, request, context and outcome.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from forgeflow.core.ids import new_id, utcnow
from forgeflow.schemas.verification import A2AMessage

# responder(question, context) -> answer text
Responder = Callable[[str, str], Awaitable[str]]


class A2AChannel:
    def __init__(
        self,
        *,
        workflow_id: str,
        task_id: str | None,
        sender: str,
        receiver: str,
        responder: Responder,
        timeout_seconds: float,
        max_messages: int,
    ) -> None:
        self.workflow_id = workflow_id
        self.task_id = task_id
        self.sender = sender
        self.receiver = receiver
        self.responder = responder
        self.timeout_seconds = timeout_seconds
        self.max_messages = max_messages
        self.messages: list[A2AMessage] = []

    async def ask(self, question: str, context: str = "", message_type: str = "question") -> str:
        message = A2AMessage(
            message_id=new_id("a2a"),
            workflow_id=self.workflow_id,
            task_id=self.task_id,
            sender=self.sender,
            receiver=self.receiver,
            message_type=message_type,  # type: ignore[arg-type]
            request=question.strip()[:4000],
            context=context.strip()[:4000],
            timeout_seconds=self.timeout_seconds,
            status="refused",
            created_at=utcnow(),
        )
        if len(self.messages) >= self.max_messages:
            message.response = f"A2A limit of {self.max_messages} question(s) per run reached."
            self.messages.append(message)
            return f"REFUSED: {message.response} Decide with the evidence you have."
        try:
            answer = await asyncio.wait_for(
                self.responder(message.request, message.context), self.timeout_seconds
            )
            message.status = "answered"
            message.response = answer[:8000]
        except TimeoutError:
            message.status = "timeout"
            message.response = f"no answer within {self.timeout_seconds:.0f}s"
        except Exception as exc:  # the asking agent must keep working
            message.status = "error"
            message.response = f"{type(exc).__name__}: {exc}"[:500]
        message.answered_at = utcnow()
        self.messages.append(message)
        if message.status != "answered":
            return f"NO ANSWER ({message.status}): {message.response}"
        return message.response or ""
