import json
import sqlite3

import pytest

from sifu import db
from sifu.models import DecisionResult, DecisionType, RiskAssessment, RiskTier, TicketStatus
from sifu.state import InvalidTransition


@pytest.fixture()
def conn():
    connection = db.connect(":memory:")
    db.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture()
def ticket(conn):
    project_id = db.create_project(conn, "TripPlanner", "/tmp/tripplanner-ios", "owner/repo")
    return db.create_ticket(conn, project_id, "Add packing list", "As a traveller...")


def test_new_ticket_starts_in_intent_with_an_event(conn, ticket):
    assert db.get_ticket(conn, ticket)["status"] == "intent"
    assert [e["kind"] for e in db.list_events(conn, ticket)] == ["ticket_created"]


def test_status_changes_are_validated_and_logged(conn, ticket):
    db.set_status(conn, ticket, TicketStatus.SPEC)
    assert db.get_ticket(conn, ticket)["status"] == "spec"
    event = db.list_events(conn, ticket)[-1]
    assert event["kind"] == "status_changed"
    assert json.loads(event["payload"]) == {"from": "intent", "to": "spec"}


def test_illegal_transition_leaves_ticket_unchanged(conn, ticket):
    with pytest.raises(InvalidTransition):
        db.set_status(conn, ticket, TicketStatus.DONE)
    assert db.get_ticket(conn, ticket)["status"] == "intent"


def test_gated_transition_needs_facts(conn, ticket):
    db.set_status(conn, ticket, TicketStatus.SPEC)
    with pytest.raises(InvalidTransition):
        db.set_status(conn, ticket, TicketStatus.BUILD, {"spec_accepted": True})
    db.set_status(conn, ticket, TicketStatus.BUILD, {"spec_accepted": True, "plan_accepted": True})
    assert db.get_ticket(conn, ticket)["status"] == "build"


def test_decision_is_stored_with_rules_and_assessments(conn, ticket):
    assessments = [RiskAssessment(assessor="m", tier=RiskTier.HUMAN, confidence=0.7, rationale="x")]
    result = DecisionResult(
        final_tier=RiskTier.HUMAN,
        floor_tier=RiskTier.AUTO,
        rules_fired=["missing_assessment"],
        assessor_tiers=[RiskTier.HUMAN],
    )
    decision_id = db.record_decision(conn, ticket, DecisionType.ACCEPT_SPEC, result, assessments)
    row = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
    assert row["resolution"] == "pending_human"
    assert json.loads(row["rules_fired"]) == ["missing_assessment"]
    assert json.loads(row["assessments"])[0]["assessor"] == "m"
    db.resolve_decision(conn, decision_id, "human_approved", "lance")
    row = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
    assert (row["resolution"], row["decided_by"]) == ("human_approved", "lance")


def test_human_request_lifecycle(conn, ticket):
    request_id = db.open_human_request(conn, ticket, "Which currency?", ["CAD", "USD"])
    assert [r["id"] for r in db.open_requests(conn)] == [request_id]
    db.answer_human_request(conn, request_id, "CAD")
    assert db.open_requests(conn) == []
    with pytest.raises(ValueError):
        db.answer_human_request(conn, request_id, "USD")


def test_foreign_keys_are_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        db.create_ticket(conn, 999, "orphan")


def test_run_log_accepts_missing_token_counts(conn, ticket):
    assert db.record_run(conn, "reviewer", "gemma4:31b-mlx", ticket_id=ticket, latency_ms=1200) > 0


def test_get_or_create_project_reuses_existing_name(conn):
    first = db.get_or_create_project(conn, "TripPlanner", "/tmp/tripplanner-ios")
    assert db.get_or_create_project(conn, "TripPlanner", "/tmp/tripplanner-ios") == first


def test_get_or_create_project_is_idempotent(conn):
    first = db.get_or_create_project(conn, "TripPlanner", "/tmp/a", "o/r")
    assert db.get_or_create_project(conn, "TripPlanner", "/tmp/b") == first
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1


def test_a_chat_is_stored_in_order_and_can_be_linked_to_its_ticket(conn, ticket):
    db.add_chat_message(conn, "s1", "intent", "user", "Blank names save.")
    db.add_chat_message(conn, "s1", "intent", "assistant", "Which?", title="T", draft="D")
    db.add_chat_message(conn, "other", "intent", "user", "Something else.")
    rows = db.list_chat(conn, "s1")
    assert [(r["role"], r["content"], r["ticket_id"]) for r in rows] == [
        ("user", "Blank names save.", None),
        ("assistant", "Which?", None),
    ]
    assert (rows[1]["title"], rows[1]["draft"]) == ("T", "D")
    assert db.attach_chat(conn, "s1", ticket) == 2
    assert {r["ticket_id"] for r in db.list_chat(conn, "s1")} == {ticket}
    assert db.list_chat(conn, "other")[0]["ticket_id"] is None


def test_init_db_adds_the_chat_table_to_an_older_database(conn):
    conn.execute("DROP TABLE chat_messages")
    db.init_db(conn)
    assert db.list_chat(conn, "s1") == []


def test_a_job_moves_from_queued_to_running_to_done(conn, ticket):
    job = db.create_job(conn, "run", {"ticket_id": ticket, "live": False}, ticket)
    [row] = db.list_jobs(conn)
    assert (row["status"], row["kind"], json.loads(row["payload"])["live"]) == (
        "queued",
        "run",
        False,
    )
    assert db.start_job(conn, job) is True and db.start_job(conn, job) is False
    assert db.list_jobs(conn)[0]["started_at"] is not None
    db.finish_job(conn, job, result="spec")
    [row] = db.list_jobs(conn)
    assert (row["status"], row["result"], row["error"]) == ("done", "spec", None)
    assert row["finished_at"] is not None and db.list_jobs(conn, active_only=True) == []


def test_a_job_that_ends_with_an_error_is_failed(conn, ticket):
    job = db.create_job(conn, "run", {}, ticket)
    db.start_job(conn, job)
    db.finish_job(conn, job, error="ConnectionError: ollama is down")
    assert tuple(db.list_jobs(conn)[0][k] for k in ("status", "error")) == (
        "failed",
        "ConnectionError: ollama is down",
    )


def test_a_ticket_has_at_most_one_active_job(conn, ticket):
    first = db.create_job(conn, "run", {}, ticket)
    with pytest.raises(db.JobConflict):
        db.create_job(conn, "answer", {}, ticket)
    db.start_job(conn, first)
    with pytest.raises(db.JobConflict):
        db.create_job(conn, "answer", {}, ticket)
    db.finish_job(conn, first, result="spec")
    assert db.create_job(conn, "answer", {}, ticket) > first  # allowed once the first ended


def test_jobs_without_a_ticket_do_not_conflict(conn):
    assert db.create_job(conn, "lessons", {}) != db.create_job(conn, "lessons", {})


def test_only_a_queued_job_can_be_cancelled(conn, ticket):
    job = db.create_job(conn, "run", {}, ticket)
    assert db.cancel_job(conn, job) is True and db.list_jobs(conn)[0]["status"] == "cancelled"
    assert db.start_job(conn, job) is False  # a cancelled job never starts
    other = db.create_job(conn, "run", {}, ticket)
    db.start_job(conn, other)
    assert db.cancel_job(conn, other) is False


def test_stale_jobs_are_marked_interrupted(conn, ticket):
    running = db.create_job(conn, "run", {}, ticket)
    db.start_job(conn, running)
    queued = db.create_job(conn, "lessons", {})
    assert db.interrupt_stale_jobs(conn) == 2
    assert {r["id"]: r["status"] for r in db.list_jobs(conn)} == {
        running: "interrupted",
        queued: "interrupted",
    }
    assert db.interrupt_stale_jobs(conn) == 0


def test_list_jobs_is_newest_first(conn, ticket):
    first = db.create_job(conn, "lessons", {})
    second = db.create_job(conn, "lessons", {})
    assert [r["id"] for r in db.list_jobs(conn)] == [second, first]
