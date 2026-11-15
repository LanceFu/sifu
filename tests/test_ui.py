"""The Streamlit app renders the board, a ticket timeline and the inbox from a database file."""

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from sifu import db, demo, ui  # noqa: E402
from sifu.models import (  # noqa: E402
    DecisionResult,
    DecisionType,
    RiskAssessment,
    RiskTier,
    TicketStatus,
)

PR_URL = "https://github.com/LanceFu/tripplanner-ios/pull/2"


@pytest.fixture()
def database(tmp_path, monkeypatch):
    """Two tickets: one waiting on a Design-stage approval, one waiting on a merge decision."""
    path = tmp_path / "ui.db"
    conn = db.connect(path)
    db.init_db(conn)
    project = db.create_project(conn, "TripPlanner", "/repo", "LanceFu/tripplanner-ios")

    spec = db.create_ticket(conn, project, "Rename a Trip", "As a Traveller I rename a Trip.")
    result = DecisionResult(
        floor_tier=RiskTier.HUMAN, final_tier=RiskTier.HUMAN, rules_fired=["touches_protected_path"]
    )
    opinion = RiskAssessment(
        assessor="qwen", tier=RiskTier.AUTO, confidence=0.8, rationale="Small and clear."
    )
    decision = db.record_decision(conn, spec, DecisionType.ACCEPT_SPEC, result, [opinion])
    db.set_status(conn, spec, TicketStatus.NEEDS_HUMAN)
    db.open_human_request(conn, spec, "Approve this spec?", ["approve", "close"], decision)

    merge = db.create_ticket(conn, project, "Limit a Day Plan to 10 Stops")
    db.append_event(conn, merge, "pr_opened", "supervisor", {"number": 2, "url": PR_URL})
    merge_decision = db.record_decision(conn, merge, DecisionType.MERGE, result, [opinion])
    db.set_status(conn, merge, TicketStatus.NEEDS_HUMAN)
    db.open_human_request(conn, merge, "Merge this PR?", ["approve", "close"], merge_decision)
    conn.close()
    monkeypatch.setenv(ui.DB_ENV, str(path))
    return path


def run(view):
    at = AppTest.from_file(ui.__file__, default_timeout=30)
    at.session_state["view"] = view
    at.run()
    assert not at.exception, at.exception
    return at


def text(at):
    return "\n".join(str(el.value) for el in [*at.markdown, *at.caption, *at.info, *at.code])


def test_board_shows_each_ticket_in_its_column(database):
    page = text(run("Board"))
    assert "Rename a Trip" in page and "Limit a Day Plan to 10 Stops" in page
    assert "needs_human (2)" in page and "done (0)" in page
    assert f"[PR #2]({PR_URL})" in page


def test_ticket_view_shows_the_timeline_with_the_decision(database):
    at = run("Ticket")
    page = text(at)
    assert "decision_recorded" in page
    assert "touches_protected_path" in page and "pending_human" in page
    assert "qwen: AUTO (confidence 0.8). Small and clear." in page


def test_inbox_offers_an_answer_button_per_option_for_a_design_request(database):
    at = run("Inbox")
    assert "Approve this spec?" in text(at)
    assert [b.label for b in at.button if b.key and b.key.startswith("answer-1-")] == [
        "approve",
        "close",
    ]


def test_inbox_sends_a_merge_request_to_the_command_line(database):
    at = run("Inbox")
    assert not [b for b in at.button if b.key and b.key.startswith("answer-2-")]
    assert "scripts/live_run.py" in text(at) and "--answer 2" in text(at)


def test_clicking_an_option_answers_the_request(database, monkeypatch):
    calls = []
    monkeypatch.setattr(
        demo,
        "answer_in_database",
        lambda path, request, answer: calls.append((str(path), request, answer)),
    )
    at = run("Inbox")
    next(b for b in at.button if b.key == "answer-1-approve").click().run()
    assert not at.exception
    assert calls == [(str(database), 1, "approve")]


def test_the_views_are_tabs_and_only_the_open_one_is_built(database):
    at = run("Board")
    assert [tab.label for tab in at.tabs] == list(ui.VIEWS)
    assert not at.radio
    assert "needs_human (2)" in text(at) and "Approve this spec?" not in text(at)


def test_the_timeline_button_opens_that_ticket_in_the_ticket_tab(database):
    at = run("Board")
    next(b for b in at.button if b.key == "open-2").click().run()
    assert not at.exception, at.exception
    assert at.session_state["view"] == "Ticket" and at.session_state["ticket"] == 2
    assert "pr_opened" in text(at) and "needs_human (2)" not in text(at)


def test_viewing_never_writes_to_the_database(database):
    before = database.read_bytes()
    for view in ("Board", "Ticket", "Inbox"):
        run(view)
    assert database.read_bytes() == before
