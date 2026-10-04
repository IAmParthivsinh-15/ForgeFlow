import pytest

from forgeflow.core.errors import InvalidStateTransition
from forgeflow.platform.orchestration.state_machine import (
    TERMINAL,
    can_transition,
    ensure_transition,
)
from forgeflow.schemas.workflow import WorkflowStatus as S


def test_clarification_loop_transitions_are_allowed():
    assert can_transition(S.CREATED, S.PLANNING)
    assert can_transition(S.PLANNING, S.AWAITING_CLARIFICATION)
    assert can_transition(S.AWAITING_CLARIFICATION, S.PLANNING)
    assert can_transition(S.PLANNING, S.PLANNED)


def test_cannot_skip_requirement_analysis():
    assert not can_transition(S.CREATED, S.EXECUTING)
    assert not can_transition(S.AWAITING_CLARIFICATION, S.PLANNED)
    with pytest.raises(InvalidStateTransition):
        ensure_transition(S.CREATED, S.PLANNED)


@pytest.mark.parametrize("terminal", sorted(TERMINAL))
def test_terminal_states_have_no_exits(terminal):
    assert all(not can_transition(terminal, target) for target in S)
