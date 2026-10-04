"""Workflow state machine (spec sections 18, 173).

Transitions are explicit; every transition the service performs emits a
`workflow.status_changed` event.
"""

from __future__ import annotations

from forgeflow.core.errors import InvalidStateTransition
from forgeflow.schemas.workflow import WorkflowStatus as S

TERMINAL = frozenset({S.COMPLETED, S.CANCELLED, S.FAILED})

_TRANSITIONS: dict[S, frozenset[S]] = {
    S.CREATED: frozenset({S.PLANNING, S.CANCELLED, S.FAILED}),
    S.PLANNING: frozenset({S.AWAITING_CLARIFICATION, S.PLANNED, S.PAUSED, S.CANCELLED, S.FAILED}),
    S.AWAITING_CLARIFICATION: frozenset({S.PLANNING, S.CANCELLED, S.FAILED}),
    S.PLANNED: frozenset({S.EXECUTING, S.REVIEWING, S.TESTING, S.CI, S.PAUSED, S.CANCELLED}),
    S.EXECUTING: frozenset({S.INTEGRATING, S.PAUSED, S.CANCELLED, S.FAILED}),
    S.INTEGRATING: frozenset({S.REVIEWING, S.TESTING, S.CI, S.PAUSED, S.CANCELLED, S.FAILED}),
    S.REVIEWING: frozenset({S.EXECUTING, S.TESTING, S.CI, S.COMPLETED, S.CANCELLED, S.FAILED}),
    S.TESTING: frozenset({S.EXECUTING, S.CI, S.COMPLETED, S.CANCELLED, S.FAILED}),
    S.CI: frozenset({S.EXECUTING, S.DEPLOYING, S.COMPLETED, S.CANCELLED, S.FAILED}),
    S.DEPLOYING: frozenset({S.VERIFYING, S.CANCELLED, S.FAILED}),
    S.VERIFYING: frozenset({S.COMPLETED, S.FAILED}),
    S.PAUSED: frozenset({S.PLANNING, S.PLANNED, S.EXECUTING, S.CANCELLED}),
    S.COMPLETED: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
}


def can_transition(current: S, target: S) -> bool:
    return target in _TRANSITIONS[current]


def ensure_transition(current: S, target: S) -> None:
    if not can_transition(current, target):
        raise InvalidStateTransition(f"workflow cannot move from {current} to {target}")
