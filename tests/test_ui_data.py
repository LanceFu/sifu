"""The UI's board, timeline and inbox are plain queries over the audit tables."""

import pytest

from sifu import db, ui_data
from sifu.models import DecisionResult, DecisionType, RiskAssessment, RiskTier, TicketStatus

PR_URL = "https://github.com/LanceFu/tripplanner-ios/pull/2"


@pytest.fixture()
def conn():
    conn = db.connect(":memory:")
    db.init_db(conn)
    return conn


@pytest.fixture()
def project(conn):
    return db.create_project(conn, "TripPlanner", "/repo", "LanceFu/tripplanner-ios")


def decide(conn, tid, kind=DecisionType.ACCEPT_SPEC, tier=RiskTier.HUMAN, rules=("rule_x",)):
    return db.record_decision(
        conn,
        tid,
        kind,
        DecisionResult(floor_tier=tier, final_tier=tier, rules_fired=list(rules)),
        [RiskAssessment(assessor="qwen", tier=RiskTier.AUTO, confidence=0.8, rationale="Fine.")],
    )


def test_board_has_a_column_for_every_status_in_pipeline_order(conn, project):
    assert list(ui_data.board(conn)) == [s.value for s in TicketStatus]


def test_board_puts_each_ticket_under_its_status(conn, project):
    first = db.create_ticket(conn, project, "Rename a Trip")
    second = db.create_ticket(conn, project, "Remove a Stop")
    db.set_status(conn, second, TicketStatus.NEEDS_HUMAN)
    columns = ui_data.board(conn)
    assert [t["id"] for t in columns["intent"]] == [first]
    assert [t["title"] for t in columns["needs_human"]] == ["Remove a Stop"]


def test_board_card_carries_the_pull_request_and_the_waiting_flag(conn, project):
    tid = db.create_ticket(conn, project, "Rename a Trip")
    db.append_event(conn, tid, "pr_opened", "supervisor", {"number": 2, "url": PR_URL, "sha": "a"})
    db.open_human_request(conn, tid, "Approve?", ["approve", "close"])
    [card] = ui_data.board(conn)["intent"]
    assert card["pr_url"] == PR_URL and card["pr_number"] == 2
    assert card["waiting"] is True


def test_board_filters_by_project(conn, project):
    other = db.create_project(conn, "Other", "/other")
    db.create_ticket(conn, project, "Rename a Trip")
    kept = db.create_ticket(conn, other, "Something else")
    cards = [t for column in ui_data.board(conn, other).values() for t in column]
    assert [t["id"] for t in cards] == [kept]


def test_timeline_is_in_order_and_summarises_status_changes(conn, project):
    tid = db.create_ticket(conn, project, "Rename a Trip")
    db.set_status(conn, tid, TicketStatus.NEEDS_HUMAN)
    rows = ui_data.timeline(conn, tid)
    assert [r["kind"] for r in rows] == ["ticket_created", "status_changed"]
    assert rows[1]["summary"] == "intent → needs_human"
    assert rows[1]["actor"] == "supervisor"


def test_timeline_decision_row_shows_tier_rules_assessors_and_resolution(conn, project):
    tid = db.create_ticket(conn, project, "Rename a Trip")
    decision = decide(conn, tid, rules=("touches_protected_path",))
    db.resolve_decision(conn, decision, "human_approved", "human")
    [row] = [r for r in ui_data.timeline(conn, tid) if r["kind"] == "decision_recorded"]
    assert row["is_decision"] is True
    assert "accept_spec" in row["summary"] and "HUMAN" in row["summary"]
    assert "touches_protected_path" in row["summary"]
    assert "human_approved" in row["summary"]
    assert row["detail"] == ["qwen: AUTO (confidence 0.8). Fine."]


def test_timeline_summarises_a_failed_verify_round(conn, project):
    tid = db.create_ticket(conn, project, "Rename a Trip")
    finding = {
        "source": "swiftlint",
        "rule": "force_cast",
        "file": "A.swift",
        "line": 3,
        "message": "Force casts should be avoided",
    }
    db.append_event(
        conn,
        tid,
        "verify_result",
        "verify",
        {"ok": False, "steps": [{"name": "swiftlint", "ok": False}], "findings": [finding]},
    )
    row = ui_data.timeline(conn, tid)[-1]
    assert row["summary"] == "failed: swiftlint FAILED; 1 finding"
    assert row["detail"] == ["swiftlint force_cast A.swift:3: Force casts should be avoided"]


def test_timeline_of_an_unknown_event_kind_still_shows_the_kind(conn, project):
    tid = db.create_ticket(conn, project, "Rename a Trip")
    db.append_event(conn, tid, "something_new", "supervisor", {"a": 1})
    row = ui_data.timeline(conn, tid)[-1]
    assert row["kind"] == "something_new" and row["summary"] == "a=1"


def test_inbox_lists_only_open_requests_with_their_ticket_and_options(conn, project):
    tid = db.create_ticket(conn, project, "Rename a Trip")
    done = db.open_human_request(conn, tid, "Old question", ["approve", "close"])
    db.answer_human_request(conn, done, "approve")
    decision = decide(conn, tid)
    request = db.open_human_request(conn, tid, "Approve this spec?", ["approve", "close"], decision)
    [item] = ui_data.inbox(conn)
    assert item["id"] == request and item["ticket_title"] == "Rename a Trip"
    assert item["options"] == ["approve", "close"]
    assert item["stage"] == "accept_spec" and item["rules"] == ["rule_x"]


def test_design_stage_request_can_be_answered_in_the_ui(conn, project):
    tid = db.create_ticket(conn, project, "Rename a Trip")
    db.open_human_request(conn, tid, "Which name?", [])
    assert ui_data.inbox(conn)[0]["needs_live_tools"] is False


@pytest.mark.parametrize("kind", ["implementer_result", "pr_opened", "draft_pr_opened"])
def test_request_after_build_started_needs_the_live_tools(conn, project, kind):
    tid = db.create_ticket(conn, project, "Rename a Trip")
    db.append_event(conn, tid, kind, "supervisor", {})
    db.open_human_request(conn, tid, "Merge?", ["approve", "close"])
    assert ui_data.inbox(conn)[0]["needs_live_tools"] is True


def test_plan_or_merge_decision_needs_the_live_tools(conn, project):
    tid = db.create_ticket(conn, project, "Rename a Trip")
    decision = decide(conn, tid, kind=DecisionType.ACCEPT_PLAN)
    db.open_human_request(conn, tid, "Approve this plan?", ["approve", "close"], decision)
    assert ui_data.inbox(conn)[0]["needs_live_tools"] is True


def test_list_databases_finds_db_files_and_skips_sidecars(tmp_path):
    for name in ("live.db", "probe-once.db", "live.db-wal", "notes.txt"):
        (tmp_path / name).write_text("")
    assert [p.name for p in ui_data.list_databases(tmp_path)] == ["live.db", "probe-once.db"]


def test_timeline_key_events_drop_tool_calls_and_status_changes(conn, project):
    tid = db.create_ticket(conn, project, "Rename a Trip")
    db.append_event(conn, tid, "tool_call", "supervisor", {"tool": "get_ticket"})
    db.set_status(conn, tid, TicketStatus.NEEDS_HUMAN)
    decide(conn, tid)
    db.append_event(conn, tid, "verify_result", "verify", {"ok": True, "steps": []})
    db.append_event(conn, tid, "pr_opened", "supervisor", {"number": 2, "url": PR_URL})
    kinds = [r["kind"] for r in ui_data.timeline(conn, tid, key_only=True)]
    assert kinds == ["ticket_created", "decision_recorded", "verify_result", "pr_opened"]


def test_timeline_summarises_the_deterministic_checks(conn, project):
    tid = db.create_ticket(conn, project, "Rename a Trip")
    db.append_event(
        conn, tid, "agent_output", "deterministic_checks", {"findings": [], "deterministic": True}
    )
    assert ui_data.timeline(conn, tid)[-1]["summary"] == "0 findings"


def test_inbox_actions_are_only_the_options_that_are_commands(conn, project):
    tid = db.create_ticket(conn, project, "Rename a Trip")
    db.open_human_request(conn, tid, "Which name?", ["answer the questions", "close"])
    item = ui_data.inbox(conn)[0]
    assert item["actions"] == ["close"]  # "answer the questions" asks for a typed reply


def test_inbox_question_headings_are_demoted_to_bold(conn, project):
    tid = db.create_ticket(conn, project, "Rename a Trip")
    db.open_human_request(conn, tid, "Needs answers:\n\n# Questions for ticket 1\n\n1. Which?", [])
    assert ui_data.inbox(conn)[0]["question"] == (
        "Needs answers:\n\n**Questions for ticket 1**\n\n1. Which?"
    )


def test_chats_groups_a_tickets_messages_by_session_in_order(conn, project):
    tid = db.create_ticket(conn, project, "Rename a Trip")
    db.add_chat_message(conn, "a", "intent", "user", "Rename it.", ticket_id=tid)
    db.add_chat_message(conn, "b", "spec", "user", "Why stopped?", ticket_id=tid)
    db.add_chat_message(conn, "a", "intent", "assistant", "To what?", ticket_id=tid)
    db.add_chat_message(conn, "elsewhere", "intent", "user", "Not this ticket.")
    found = ui_data.chats(conn, tid)
    assert [(c["session"], c["stage"], len(c["messages"])) for c in found] == [
        ("a", "intent", 2),
        ("b", "spec", 1),
    ]
    assert [m["content"] for m in ui_data.chat_messages(conn, "a")] == ["Rename it.", "To what?"]


def test_chat_queries_are_empty_on_a_database_recorded_before_chats(conn, project):
    tid = db.create_ticket(conn, project, "Rename a Trip")
    conn.execute("DROP TABLE chat_messages")
    assert ui_data.chats(conn, tid) == [] and ui_data.chat_messages(conn, "a") == []


def test_a_saved_chat_is_a_key_event_on_the_timeline(conn, project):
    tid = db.create_ticket(conn, project, "Rename a Trip")
    db.append_event(
        conn, tid, "chat_saved", "human", {"stage": "intent", "messages": 4, "path": "/a/chat.md"}
    )
    row = ui_data.timeline(conn, tid, key_only=True)[-1]
    assert (row["summary"], row["detail"]) == ("intent chat, 4 messages", ["/a/chat.md"])


def test_design_request_is_the_open_request_the_ui_can_answer(conn, project):
    spec = db.create_ticket(conn, project, "Rename a Trip")
    db.open_human_request(conn, spec, "Approve?", ["approve", "close"], decide(conn, spec))
    merge = db.create_ticket(conn, project, "Limit a Day Plan")
    db.open_human_request(
        conn, merge, "Merge?", ["approve", "close"], decide(conn, merge, DecisionType.MERGE)
    )
    idle = db.create_ticket(conn, project, "Remove a Stop")
    assert ui_data.design_request(conn, spec)["question"] == "Approve?"
    assert ui_data.design_request(conn, merge) is None
    assert ui_data.design_request(conn, idle) is None


def test_jobs_are_read_newest_first_and_the_active_one_is_found(conn, project):
    tid = db.create_ticket(conn, project, "Rename a Trip")
    first = db.create_job(conn, "run", {}, tid)
    db.start_job(conn, first)
    db.finish_job(conn, first, error="boom")
    second = db.create_job(conn, "answer", {}, tid)
    assert [j["id"] for j in ui_data.jobs(conn)] == [second, first]
    assert [j["id"] for j in ui_data.jobs(conn, active_only=True)] == [second]
    assert ui_data.active_job(conn, tid)["kind"] == "answer"
    db.cancel_job(conn, second)
    assert ui_data.active_job(conn, tid) is None
    assert ui_data.last_job(conn, tid)["status"] == "cancelled"


def test_job_queries_are_empty_on_a_database_recorded_before_jobs(conn, project):
    tid = db.create_ticket(conn, project, "Rename a Trip")
    conn.execute("DROP INDEX jobs_one_active_per_ticket")
    conn.execute("DROP TABLE jobs")
    assert ui_data.jobs(conn) == [] and ui_data.active_job(conn, tid) is None
    assert ui_data.last_job(conn, tid) is None
    assert ui_data.board(conn)["intent"][0]["running"] is False


def test_the_board_marks_a_ticket_with_an_active_job(conn, project):
    busy = db.create_ticket(conn, project, "Rename a Trip")
    idle = db.create_ticket(conn, project, "Remove a Stop")
    db.create_job(conn, "run", {}, busy)
    cards = {c["id"]: c["running"] for c in ui_data.board(conn)["intent"]}
    assert cards == {busy: True, idle: False}


def test_awaiting_build_is_a_spec_ticket_whose_last_step_was_the_plan_gate(conn, project):
    waiting = db.create_ticket(conn, project, "Rename a Trip")
    db.set_status(conn, waiting, TicketStatus.SPEC)
    db.append_event(conn, waiting, "awaiting_plan", "supervisor", {})
    assert ui_data.awaiting_build(conn, waiting) is True

    fresh = db.create_ticket(conn, project, "Remove a Stop")
    assert ui_data.awaiting_build(conn, fresh) is False  # still at intent

    built = db.create_ticket(conn, project, "Limit a Day Plan")
    db.set_status(conn, built, TicketStatus.SPEC)
    db.append_event(conn, built, "awaiting_plan", "supervisor", {})
    db.append_event(conn, built, "build_requested", "human", {})
    assert ui_data.awaiting_build(conn, built) is False  # already asked for

    asked = db.create_ticket(conn, project, "Reorder Stops")
    db.set_status(conn, asked, TicketStatus.SPEC)
    db.append_event(conn, asked, "awaiting_plan", "supervisor", {})
    db.open_human_request(conn, asked, "Approve?", ["approve", "close"])
    assert ui_data.awaiting_build(conn, asked) is False  # a request is open


def test_can_reopen_is_a_failed_ticket_without_a_pull_request(conn, project):
    failed = db.create_ticket(conn, project, "Rename a Trip")
    db.set_status(conn, failed, TicketStatus.FAILED)
    with_pr = db.create_ticket(conn, project, "Limit a Day Plan")
    db.append_event(conn, with_pr, "draft_pr_opened", "supervisor", {"number": 2, "url": PR_URL})
    db.set_status(conn, with_pr, TicketStatus.FAILED)
    alive = db.create_ticket(conn, project, "Remove a Stop")
    assert [ui_data.can_reopen(conn, t) for t in (failed, with_pr, alive)] == [True, False, False]


def test_a_stuck_build_request_offers_retry_and_close_and_takes_a_note(conn, project):
    tid = db.create_ticket(conn, project, "Rename a Trip")
    db.append_event(conn, tid, "implementer_result", "implementer", {"ok": True})
    db.open_human_request(conn, tid, "Build stopped", ["retry", "retry with a note", "close"])
    [item] = ui_data.inbox(conn)
    assert item["actions"] == ["retry", "close"] and item["takes_note"] is True
    assert item["needs_live_tools"] is True


def test_artifact_files_lists_markdown_files_in_the_folder_only(tmp_path):
    folder = tmp_path / "ticket"
    folder.mkdir()
    (folder / "spec.md").write_text("s")
    (folder / "intent.md").write_text("i")
    (folder / "notes.txt").write_text("n")
    (folder / "sub").mkdir()
    (folder / "sub" / "deep.md").write_text("d")
    outside = tmp_path / "secret.md"
    outside.write_text("x")
    (folder / "link.md").symlink_to(outside)
    assert [p.name for p in ui_data.artifact_files(folder)] == ["intent.md", "spec.md"]
    assert ui_data.artifact_files(folder / "missing") == []


def test_worktree_of_is_the_last_implementer_runs_worktree(conn, project):
    tid = db.create_ticket(conn, project, "Rename a Trip")
    assert ui_data.worktree_of(conn, tid) is None
    db.append_event(conn, tid, "implementer_result", "implementer", {"worktree": "/wt/ticket-1"})
    db.append_event(conn, tid, "implementer_result", "implementer", {"worktree": "/wt/ticket-1-b"})
    assert ui_data.worktree_of(conn, tid) == "/wt/ticket-1-b"
