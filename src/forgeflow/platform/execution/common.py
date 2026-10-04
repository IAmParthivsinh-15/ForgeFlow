"""Shared helpers for task handlers: handler output and run/event records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from forgeflow.core.ids import new_id, utcnow
from forgeflow.platform.orchestration.execution import task_event
from forgeflow.platform.orchestration.gateway import AgentOutcome
from forgeflow.schemas.events import Event, EventType, Topics
from forgeflow.schemas.task import CheckRun, Task, TaskResult, Workspace
from forgeflow.schemas.verification import A2AMessage
from forgeflow.schemas.workflow import AgentRunRecord


@dataclass
class HandlerOutput:
    result: TaskResult
    new_tasks: list[Task] = field(default_factory=list)
    workspaces: list[Workspace] = field(default_factory=list)
    agent_runs: list[AgentRunRecord] = field(default_factory=list)
    a2a_messages: list[A2AMessage] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    workspace_id: str | None = None


def run_record(
    task: Task,
    agent_type: str,
    prompt_version: str,
    attempts: list,
    tool_calls: list,
    started: datetime,
    error: str | None = None,
) -> AgentRunRecord:
    return AgentRunRecord(
        run_id=new_id("run"),
        workflow_id=task.workflow_id,
        task_id=task.task_id,
        agent_type=agent_type,
        prompt_version=prompt_version,
        status="failed" if error else "completed",
        attempts=attempts,
        tool_calls=tool_calls,
        error=error,
        started_at=started,
        completed_at=utcnow(),
    )


def run_from_outcome(
    task: Task, agent_type: str, outcome: AgentOutcome, started: datetime
) -> AgentRunRecord:
    return run_record(
        task, agent_type, outcome.prompt_version, outcome.attempts, outcome.tool_calls, started
    )


def agent_event(task: Task, run: AgentRunRecord) -> Event:
    return Event(
        event_type=EventType.AGENT_RUN_COMPLETED
        if run.status == "completed"
        else EventType.AGENT_RUN_FAILED,
        topic=Topics.AGENT,
        workflow_id=task.workflow_id,
        task_id=task.task_id,
        payload={
            "run_id": run.run_id,
            "agent_type": run.agent_type,
            "prompt_version": run.prompt_version,
            "providers": [f"{a.provider}:{a.model}:{a.status}" for a in run.attempts],
            "tool_calls": len(run.tool_calls),
            "error": run.error,
        },
    )


def check_event(task: Task, check: CheckRun) -> Event:
    return task_event(
        task,
        EventType.CHECK_COMPLETED,
        kind=check.kind,
        command=check.command,
        passed=check.passed,
        exit_code=check.exit_code,
        duration_ms=check.duration_ms,
    )


def a2a_event(task: Task, message: A2AMessage) -> Event:
    return Event(
        event_type=EventType.A2A_EXCHANGE,
        topic=Topics.AGENT,
        workflow_id=task.workflow_id,
        task_id=task.task_id,
        payload={
            "message_id": message.message_id,
            "sender": message.sender,
            "receiver": message.receiver,
            "status": message.status,
            "request": message.request[:200],
        },
    )
