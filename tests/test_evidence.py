"""The Build and Test evidence tables come from a recorded database alone."""

import sqlite3

import pytest

from sifu import db, evidence
from sifu.models import DecisionResult, DecisionType, RiskAssessment, RiskTier

PR_URL = "https://github.com/LanceFu/tripplanner-ios/pull/3"


def verify_payload(ok, findings=()):
    return {
        "ok": ok,
        "steps": [{"name": "build", "ok": True}, {"name": "swiftlint", "ok": ok}],
        "finding_count": len(findings),
        "findings": list(findings),
        "duration_ms": 4200,
    }


@pytest.fixture()
def recorded(tmp_path):
    """A database file holding one ticket's fix round, merge decision and merged PR."""
    path = tmp_path / "probe-once.db"
    conn = db.connect(path)
    db.init_db(conn)
    project = db.create_project(conn, "TripPlanner", "/repo", "LanceFu/tripplanner-ios")
    tid = db.create_ticket(conn, project, "Limit a Day Plan to 10 Stops")
    finding = {
        "source": "swiftlint",
        "rule": "force_cast",
        "file": "Sources/TripPlannerCore/DayPlan.swift",
        "line": 41,
        "message": "Force casts should be avoided | use as?",
    }
    db.append_event(conn, tid, "verify_result", "verify", verify_payload(False, [finding]))
    db.append_event(conn, tid, "verify_result", "verify", verify_payload(True))
    db.append_event(
        conn, tid, "pr_opened", "supervisor", {"number": 3, "url": PR_URL, "sha": "abc1234def"}
    )
    decision = db.record_decision(
        conn,
        tid,
        DecisionType.MERGE,
        DecisionResult(
            floor_tier=RiskTier.HUMAN, final_tier=RiskTier.HUMAN, rules_fired=["manual_merge_mode"]
        ),
        [
            RiskAssessment(
                assessor="gemma4", tier=RiskTier.AUTO, confidence=0.9, rationale="Small change."
            )
        ],
    )
    db.resolve_decision(conn, decision, "human_approved", "human")
    db.append_event(conn, tid, "pr_merged", "supervisor", {"number": 3})
    conn.close()
    return path


def test_verify_table_lists_each_round_with_its_findings(recorded):
    table = evidence.verify_table(evidence.open_recorded(recorded))
    first, second = [line for line in table.splitlines() if line.startswith("| 1 |")]
    assert "| 1 | 1 | failed |" in first
    assert "swiftlint `force_cast` Sources/TripPlannerCore/DayPlan.swift:41" in first
    assert "| 1 | 2 | passed |" in second


def test_a_pipe_in_a_finding_does_not_break_the_table(recorded):
    table = evidence.verify_table(evidence.open_recorded(recorded))
    assert "avoided \\| use as?" in table


def test_decisions_table_shows_tier_rules_assessors_and_resolution(recorded):
    table = evidence.decisions_table(evidence.open_recorded(recorded))
    row = table.splitlines()[-1]
    assert "| merge | HUMAN | HUMAN | `manual_merge_mode` |" in row
    assert "gemma4: AUTO. Small change." in row
    assert row.rstrip().endswith("| human_approved |")


def test_pr_table_links_each_pull_request_event(recorded):
    table = evidence.pr_table(evidence.open_recorded(recorded))
    assert f"| 1 | pr_opened | [#3]({PR_URL}) | `abc1234` |" in table
    assert f"| 1 | pr_merged | [#3]({PR_URL}) |" in table


def test_recorded_database_is_opened_read_only(recorded):
    conn = evidence.open_recorded(recorded)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM events")


def test_missing_database_is_reported_and_not_created(tmp_path):
    path = tmp_path / "live.db"
    text = evidence.build_test_evidence(path)
    assert "live.db" in text and "not found" in text
    assert not path.exists()


def test_build_test_evidence_has_all_three_sections(recorded):
    text = evidence.build_test_evidence(recorded)
    assert text.index("Verify rounds") < text.index("Decisions") < text.index("Pull requests")
    assert "probe-once.db" in text


def test_build_test_evidence_names_each_ticket_and_its_status(recorded):
    text = evidence.build_test_evidence(recorded)
    assert "Ticket 1: Limit a Day Plan to 10 Stops (status: intent)" in text
