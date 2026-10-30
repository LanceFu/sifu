import pytest

from sifu.models import TicketStatus as S
from sifu.state import TRANSITIONS, InvalidTransition, assert_transition, gate_violations

ACTIVE = [S.INTENT, S.SPEC, S.BUILD, S.TEST]


def test_happy_path_with_gates_open():
    assert_transition(S.INTENT, S.SPEC)
    assert_transition(S.SPEC, S.BUILD, {"spec_accepted": True, "plan_accepted": True})
    assert_transition(S.BUILD, S.TEST, {"has_diff": True})
    assert_transition(S.TEST, S.DONE, {"ci_green": True, "review_passed": True})


def test_cannot_skip_stages():
    with pytest.raises(InvalidTransition):
        assert_transition(S.INTENT, S.DONE)
    with pytest.raises(InvalidTransition):
        assert_transition(S.SPEC, S.TEST)


def test_build_requires_accepted_spec_and_plan():
    assert gate_violations(S.BUILD, {"spec_accepted": True}) == ["plan_accepted"]
    with pytest.raises(InvalidTransition, match="plan_accepted"):
        assert_transition(S.SPEC, S.BUILD, {"spec_accepted": True})


def test_done_requires_green_ci_and_passed_review():
    with pytest.raises(InvalidTransition):
        assert_transition(S.TEST, S.DONE, {"ci_green": True})


def test_rework_loop_from_test_back_to_build():
    assert_transition(S.TEST, S.BUILD, {"spec_accepted": True, "plan_accepted": True})


def test_terminal_states_have_no_exits():
    assert TRANSITIONS[S.DONE] == frozenset()
    assert TRANSITIONS[S.FAILED] == frozenset()


@pytest.mark.parametrize("stage", ACTIVE)
def test_every_active_stage_can_escalate_or_fail(stage):
    assert_transition(stage, S.NEEDS_HUMAN)
    assert_transition(stage, S.FAILED)


def test_needs_human_can_resume_to_any_active_stage():
    facts = {"spec_accepted": True, "plan_accepted": True, "has_diff": True}
    for stage in ACTIVE:
        assert_transition(S.NEEDS_HUMAN, stage, facts)
