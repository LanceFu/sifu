"""Deterministic ticket state machine. Code, not the language model, owns transitions."""

from __future__ import annotations

from collections.abc import Mapping

from sifu.models import TicketStatus

S = TicketStatus

TRANSITIONS: dict[TicketStatus, frozenset[TicketStatus]] = {
    S.INTENT: frozenset({S.SPEC, S.NEEDS_HUMAN, S.FAILED}),
    S.SPEC: frozenset({S.INTENT, S.BUILD, S.NEEDS_HUMAN, S.FAILED}),
    S.BUILD: frozenset({S.TEST, S.NEEDS_HUMAN, S.FAILED}),
    S.TEST: frozenset({S.DONE, S.BUILD, S.NEEDS_HUMAN, S.FAILED}),
    S.NEEDS_HUMAN: frozenset({S.INTENT, S.SPEC, S.BUILD, S.TEST, S.FAILED}),
    S.DONE: frozenset(),
    S.FAILED: frozenset(),
}

# Facts that must be true before entering a stage.
GATES: dict[TicketStatus, tuple[str, ...]] = {
    S.BUILD: ("spec_accepted", "plan_accepted"),
    S.TEST: ("has_diff",),
    S.DONE: ("ci_green", "review_passed"),
}


class InvalidTransition(ValueError):
    """Raised when a transition is not allowed or its gate is not satisfied."""


def can_transition(src: TicketStatus, dst: TicketStatus) -> bool:
    """Return True if the graph allows moving from src to dst."""
    return dst in TRANSITIONS[src]


def gate_violations(dst: TicketStatus, facts: Mapping[str, bool]) -> list[str]:
    """List the facts required to enter dst that are missing or false."""
    return [name for name in GATES.get(dst, ()) if not facts.get(name, False)]


def assert_transition(
    src: TicketStatus, dst: TicketStatus, facts: Mapping[str, bool] | None = None
) -> None:
    """Raise InvalidTransition unless src -> dst is allowed and its gate is open."""
    if not can_transition(src, dst):
        raise InvalidTransition(f"{src.value} -> {dst.value} is not an allowed transition")
    missing = gate_violations(dst, facts or {})
    if missing:
        raise InvalidTransition(f"cannot enter {dst.value}: missing {', '.join(missing)}")
