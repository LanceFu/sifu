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
    assert db.record_run(conn, "reviewer", "gemma4:31b", ticket_id=ticket, latency_ms=1200) > 0
