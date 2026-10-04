"""Workflow state machine (spec sections 18, 173).

Transitions are explicit; every transition the services perform emits a
`workflow.status_changed` event.

The execution states form one group: the workflow moves freely between them as
the task graph progresses (e.g. TESTING -> EXECUTING when QA fails and a repair
runs, then back to REVIEWING for the next verification round).
"""

from __future__ import annotations

from forgeflow.core.errors import InvalidStateTransition
from forgeflow.schemas.workflow import WorkflowStatus as S

TERMINAL = frozenset({S.COMPLETED, S.CANCELLED, S.FAILED})
EXECUTION_STATES = frozenset({S.EXECUTING, S.INTEGRATING, S.REVIEWING, S.TESTING, S.CI})

_TRANSITIONS: dict[S, frozenset[S]] = {
    S.CREATED: frozenset({S.PLANNING, S.CANCELLED, S.FAILED}),
    S.PLANNING: frozenset({S.AWAITING_CLARIFICATION, S.PLANNED, S.PAUSED, S.CANCELLED, S.FAILED}),
    S.AWAITING_CLARIFICATION: frozenset({S.PLANNING, S.CANCELLED, S.FAILED}),
    S.PLANNED: EXECUTION_STATES | {S.PAUSED, S.CANCELLED},
    **{
        state: (EXECUTION_STATES - {state}) | {S.COMPLETED, S.PAUSED, S.CANCELLED, S.FAILED}
        for state in EXECUTION_STATES
    },
    S.DEPLOYING: frozenset({S.VERIFYING, S.CANCELLED, S.FAILED}),
    S.VERIFYING: frozenset({S.COMPLETED, S.FAILED}),
    S.PAUSED: EXECUTION_STATES | {S.PLANNING, S.PLANNED, S.COMPLETED, S.CANCELLED},
    S.COMPLETED: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
}


def can_transition(current: S, target: S) -> bool:
    return target in _TRANSITIONS[current]


def ensure_transition(current: S, target: S) -> None:
    if not can_transition(current, target):
        raise InvalidStateTransition(f"workflow cannot move from {current} to {target}")
