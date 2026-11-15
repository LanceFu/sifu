"""The Build and Test evidence tables come from a recorded database alone."""

import json
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


def test_decisions_table_has_tier_rules_and_resolution_and_fits_a_page(recorded):
    table, _, notes = evidence.decisions_table(evidence.open_recorded(recorded)).partition("\n\n")
    row = table.splitlines()[-1]
    assert row == "| 1 | 1 | merge | HUMAN | HUMAN | `manual_merge_mode` | human_approved |"
    assert "Small change." not in table  # long rationales would squeeze the table off the page


def test_assessor_rationales_follow_the_decisions_table_as_a_list(recorded):
    _, _, notes = evidence.decisions_table(evidence.open_recorded(recorded)).partition("\n\n")
    assert notes == "- Decision 1, gemma4: AUTO. Small change."


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


SNAPSHOT = {
    "captured_at": "2026-11-14",
    "number": 2,
    "title": "Reject blank Trip names | ticket 1",
    "url": "https://github.com/LanceFu/tripplanner-ios/pull/2",
    "state": "MERGED",
    "created_at": "2026-11-06T23:04:35Z",
    "merged_at": "2026-11-06T23:07:51Z",
    "merged_by": "LanceFu",
    "head_branch": "sifu/ticket-1",
    "head_sha": "9abc542e0a08",
    "merge_commit": "ebe6b67ba765",
    "files": [{"path": "Sources/TripPlannerCore/Trip.swift", "additions": 3, "deletions": 2}],
    "commits": [{"sha": "9abc542e0a08", "message": "Reject blank Trip names", "author": "Sifu"}],
    "checks": [
        {
            "name": "test",
            "conclusion": "SUCCESS",
            "completed_at": "2026-11-06T23:05:46Z",
            "url": "https://github.com/LanceFu/tripplanner-ios/actions/runs/1",
        }
    ],
    "comments": [
        {
            "author": "LanceFu",
            "created_at": "2026-11-06T23:06:48Z",
            "url": "https://github.com/LanceFu/tripplanner-ios/pull/2#issuecomment-1",
            "body": "Sifu merge decision: tier human.\n\n- gemma4: Small change.",
        }
    ],
}


@pytest.fixture()
def snapshot(tmp_path):
    path = tmp_path / "pr-2.json"
    path.write_text(json.dumps(SNAPSHOT), encoding="utf-8")
    return path


def test_snapshot_shows_the_pr_check_files_and_comment(snapshot):
    text = evidence.github_snapshot(snapshot)
    assert "[#2](https://github.com/LanceFu/tripplanner-ios/pull/2)" in text
    assert "Reject blank Trip names \\| ticket 1" in text  # the pipe is escaped
    assert "`sifu/ticket-1` at `9abc542`" in text
    assert "2026-11-06T23:07:51Z by LanceFu, commit `ebe6b67`" in text
    assert "Required check `test`" in text and "SUCCESS" in text
    assert "`Sources/TripPlannerCore/Trip.swift` | +3 -2" in text
    assert "> Sifu merge decision: tier human." in text
    assert "> - gemma4: Small change." in text


def test_missing_database_falls_back_to_the_snapshot_and_says_so(tmp_path, snapshot):
    text = evidence.build_test_evidence(tmp_path / "live.db", snapshot)
    assert "database lost" in text
    assert "verify rounds and the decision rows of this run cannot be shown" in text
    assert "docs/recorded/pr-2.json" in text
    assert "> Sifu merge decision: tier human." in text


def test_missing_database_without_a_snapshot_still_says_not_found(tmp_path):
    text = evidence.build_test_evidence(tmp_path / "live.db", tmp_path / "absent.json")
    assert "not found" in text


def test_a_present_database_wins_over_the_snapshot(recorded, snapshot):
    text = evidence.build_test_evidence(recorded, snapshot)
    assert "database lost" not in text
    assert "**Verify rounds**" in text


def test_the_saved_pr_2_snapshot_renders():
    text = evidence.github_snapshot(evidence.RECORDED / "pr-2.json")
    assert "pull/2" in text and "MERGED" in text and "merge_mode_manual" in text
