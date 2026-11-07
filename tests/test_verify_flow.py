"""build -> verify -> build loop through the real graph with a scripted implementer and verifier."""

import json

import pytest

pytest.importorskip("langgraph")

from sifu import db  # noqa: E402
from sifu.graph import run_ticket  # noqa: E402
from sifu.implementer import ImplementerResult  # noqa: E402
from sifu.verify import Finding, VerifyResult  # noqa: E402
from tests.fakes import FakeLLM  # noqa: E402

BODY = "Given a Traveller saves a Trip, When the name is blank, Then nameBlank is thrown."


class Implementer:
    def __init__(self):
        self.calls = []

    def __call__(self, ticket_id, spec_text, conventions, feedback=None, session_id=None):
        self.calls.append({"feedback": feedback, "session_id": session_id})
        return ImplementerResult(
            ok=True, changed_files=["Sources/Trip.swift"], session_id=f"s{len(self.calls)}"
        )


class Verifier:
    """Returns the scripted results in order, then repeats the last one."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def __call__(self, ticket_id, spec):
        result = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return result


def failing(message, line=1):
    return VerifyResult(False, [Finding("swiftlint", message, "Sources/Trip.swift", line, "r")])


PASSING = VerifyResult(True)


def run(make_deps, make_ticket, implementer, verifier):
    deps = make_deps(FakeLLM())
    deps.implementer, deps.verifier = implementer, verifier
    ticket = make_ticket("T", BODY)
    state = run_ticket(deps, ticket, facts={"plan_accepted": True})
    return deps, ticket, state


def moves(conn, ticket):
    return [
        json.loads(e["payload"])["to"]
        for e in db.list_events(conn, ticket)
        if e["kind"] == "status_changed"
    ]


def test_a_failed_check_goes_back_to_the_implementer_then_passes(conn, make_deps, make_ticket):
    impl, ver = Implementer(), Verifier(failing("Too long"), PASSING)
    _, ticket, state = run(make_deps, make_ticket, impl, ver)
    assert state["status"] == "done"
    assert moves(conn, ticket) == ["spec", "build", "test", "build", "test", "done"]
    assert impl.calls[0] == {"feedback": None, "session_id": None}
    second = impl.calls[1]
    assert second["session_id"] == "s1" and second["feedback"][0]["message"] == "Too long"
    kinds = [e["kind"] for e in db.list_events(conn, ticket)]
    assert kinds.count("verify_result") == 2 and kinds.count("implementer_result") == 2


def test_the_iteration_cap_sends_the_ticket_to_a_human(conn, cfg, make_deps, make_ticket):
    cfg.router.max_build_iterations = 3
    impl = Implementer()
    ver = Verifier(failing("a"), failing("b"), failing("c"), failing("d"))
    _, ticket, state = run(make_deps, make_ticket, impl, ver)
    assert state["status"] == "needs_human" and state["outcome"] == "verify_cap"
    assert len(impl.calls) == 3 and ver.calls == 3
    assert db.get_ticket(conn, ticket)["status"] == "needs_human"
    assert "verify_cap" in [e["kind"] for e in db.list_events(conn, ticket)]


def test_the_same_findings_twice_stop_early_for_lack_of_progress(conn, cfg, make_deps, make_ticket):
    cfg.router.max_build_iterations = 5
    impl = Implementer()
    ver = Verifier(failing("same", 3), failing("same", 8))  # the line moved, the problem did not
    _, ticket, state = run(make_deps, make_ticket, impl, ver)
    assert state["outcome"] == "verify_no_progress" and len(impl.calls) == 2
    assert db.get_ticket(conn, ticket)["status"] == "needs_human"


def test_without_a_verifier_the_stub_still_passes(conn, make_deps, make_ticket):
    _, ticket, state = run(make_deps, make_ticket, Implementer(), None)
    assert state["status"] == "done"
