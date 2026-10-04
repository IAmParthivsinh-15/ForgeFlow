"""Task state machine (spec section 17). Every transition emits an event."""

from __future__ import annotations

from forgeflow.core.errors import InvalidStateTransition
from forgeflow.schemas.task import TaskStatus as T

ACTIVE = frozenset({T.DISPATCHED, T.RUNNING})
TERMINAL = frozenset({T.COMPLETED, T.CANCELLED})

_TRANSITIONS: dict[T, frozenset[T]] = {
    T.PENDING: frozenset({T.READY, T.BLOCKED, T.CANCELLED}),
    T.READY: frozenset({T.DISPATCHED, T.BLOCKED, T.CANCELLED}),
    T.DISPATCHED: frozenset({T.RUNNING, T.FAILED, T.CANCELLED}),
    T.RUNNING: frozenset({T.COMPLETED, T.FAILED, T.BLOCKED, T.CANCELLED}),
    T.FAILED: frozenset({T.RETRYING, T.CANCELLED}),
    T.RETRYING: frozenset({T.READY, T.CANCELLED}),
    T.BLOCKED: frozenset({T.PENDING, T.CANCELLED}),
    T.COMPLETED: frozenset(),
    T.CANCELLED: frozenset(),
}


def can_transition(current: T, target: T) -> bool:
    return target in _TRANSITIONS[current]


def ensure_transition(current: T, target: T) -> None:
    if not can_transition(current, target):
        raise InvalidStateTransition(f"task cannot move from {current} to {target}")
