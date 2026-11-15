"""The Streamlit app renders the board, the chats, a timeline and the inbox from a database file."""

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from sifu import (  # noqa: E402
    brainstorm,
    db,
    demo,
    jobs,
    live,
    ops,
    preflight,
    settings,
    ui,
    ui_data,
)
from sifu.models import (  # noqa: E402
    DecisionResult,
    DecisionType,
    RiskAssessment,
    RiskTier,
    TicketStatus,
)

PR_URL = "https://github.com/LanceFu/tripplanner-ios/pull/2"


@pytest.fixture(autouse=True)
def no_real_work(monkeypatch):
    """No test starts a worker thread or looks for claude, a token or the product repo."""
    submitted = []
    monkeypatch.setattr(
        jobs,
        "submit",
        lambda path, kind, payload, ticket_id=None: (
            submitted.append((kind, payload, ticket_id)) or len(submitted)
        ),
    )
    monkeypatch.setattr(jobs, "recover", lambda path: 0)
    monkeypatch.setattr(live, "missing_tools", lambda cfg=None: ["No GitHub token found."])
    return submitted


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


def test_needs_human_is_yellow_and_failed_is_red_wherever_a_status_is_shown(database):
    headers = [m.value for m in run("Board").markdown]
    assert ":yellow-badge[**needs_human (2)**]" in headers
    assert ":red-badge[**failed (0)**]" in headers
    assert "**done (0)**" in headers and "**spec (0)**" in headers  # the rest stay plain
    assert "Status: :yellow-badge[**needs_human**]" in text(run("Ticket"))


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
    assert "No GitHub token found." in text(at)


def test_clicking_an_option_answers_the_request(database, no_real_work):
    at = run("Inbox")
    next(b for b in at.button if b.key == "answer-1-approve").click().run()
    assert not at.exception
    assert no_real_work == [("answer", {"request_id": 1, "answer": "approve", "live": False}, 1)]


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
    for view in ui.VIEWS:
        run(view)
    assert database.read_bytes() == before


def fake_reply(calls, title="Reject blank Trip names", draft="Given a blank name, Then fail."):
    """Stands in for the model call: stores the turn as the real function does."""

    def reply(path, session, stage, text, **kw):
        calls.append({"session": session, "stage": stage, "text": text, **kw})
        conn = db.connect(path)
        db.init_db(conn)
        tid = kw.get("ticket_id")
        db.add_chat_message(conn, session, stage, "user", text, ticket_id=tid)
        db.add_chat_message(
            conn, session, stage, "assistant", "Noted.", ticket_id=tid, title=title, draft=draft
        )
        conn.close()
        return brainstorm.BrainstormTurn(reply="Noted.", title=title, draft=draft)

    return reply


def open_intent():
    """The Board tab after pressing New ticket: the draft in place of the board."""
    at = run("Board")
    at.button(key="intent_create").click().run()
    assert not at.exception, at.exception
    return at


def test_new_ticket_on_the_board_swaps_the_board_for_the_draft(database):
    at = run("Board")
    assert at.button(key="intent_create").label == "New ticket"
    assert not at.text_area and not at.chat_input and "needs_human (2)" in text(at)
    at.button(key="intent_create").click().run()
    assert [w.label for w in (*at.text_input, *at.text_area)] == ["Title", "Description"]
    assert len(at.chat_input) == 1 and at.button(key="intent_start").label == "Create"
    assert {"**Chat**", "**Preview**"} <= {m.value for m in at.markdown}
    assert "needs_human (2)" not in text(at)


def test_cancel_goes_back_to_the_board_and_a_new_ticket_starts_clean(database, monkeypatch):
    calls = []
    monkeypatch.setattr(brainstorm, "reply_in_database", fake_reply(calls))
    at = open_intent()
    at.chat_input(key="intent_input").set_value("Blank names save.").run()
    at.button(key="intent_cancel").click().run()
    assert not at.exception, at.exception
    assert not at.text_area and "needs_human (2)" in text(at)
    at.button(key="intent_create").click().run()
    assert chat_text(at) == [] and at.text_area(key="intent_body").value == ""
    assert len(db.list_chat(db.connect(database), calls[0]["session"])) == 2  # still stored


def chat_text(at):
    return [(m.name, m.markdown[0].value) for m in at.chat_message]


def test_intent_chat_stores_the_messages_and_fills_the_draft(database, monkeypatch):
    calls = []
    monkeypatch.setattr(brainstorm, "reply_in_database", fake_reply(calls))
    at = open_intent()
    assert at.button(key="intent_start").disabled
    at.chat_input(key="intent_input").set_value("Blank names save.").run()
    assert not at.exception, at.exception
    assert [(c["stage"], c["text"], c["title"], c["draft"]) for c in calls] == [
        ("intent", "Blank names save.", "", "")
    ]
    assert chat_text(at) == [("user", "Blank names save."), ("assistant", "Noted.")]
    assert at.text_input(key="intent_title").value == "Reject blank Trip names"
    assert at.text_area(key="intent_body").value == "Given a blank name, Then fail."
    assert not at.button(key="intent_start").disabled
    assert "No requirement id (REQ-n)." in text(at)
    rows = db.list_chat(db.connect(database), calls[0]["session"])
    assert [r["role"] for r in rows] == ["user", "assistant"]


def test_the_next_message_sends_the_draft_as_the_person_edited_it(database, monkeypatch):
    calls = []
    monkeypatch.setattr(brainstorm, "reply_in_database", fake_reply(calls))
    at = open_intent()
    at.chat_input(key="intent_input").set_value("Blank names save.").run()
    at.text_area(key="intent_body").set_value("My own words.").run()
    at.chat_input(key="intent_input").set_value("Add REQ-1.").run()
    assert not at.exception, at.exception
    assert calls[1]["draft"] == "My own words." and calls[1]["session"] == calls[0]["session"]


def test_a_reloaded_page_reopens_the_chat_named_in_the_url(database):
    session = "ab" * 16
    conn = db.connect(database)
    db.add_chat_message(conn, session, "intent", "user", "Blank names save.")
    db.add_chat_message(conn, session, "intent", "assistant", "Noted.", title="T", draft="D")
    conn.close()
    at = AppTest.from_file(ui.__file__, default_timeout=30)
    at.session_state["view"] = "Board"
    at.query_params[ui.CHAT_PARAM] = session
    at.run()
    assert not at.exception, at.exception
    assert chat_text(at) == [("user", "Blank names save."), ("assistant", "Noted.")]
    assert at.text_input(key="intent_title").value == "T"
    assert at.text_area(key="intent_body").value == "D"


def test_start_the_workflow_files_the_reviewed_draft_and_opens_the_ticket(
    database, monkeypatch, no_real_work
):
    calls, started = [], []

    def create(path, title, body, session):
        started.append((str(path), title, body, session))
        conn = db.connect(path)
        ticket_id = db.create_ticket(conn, 1, title, body)
        conn.close()
        return ticket_id

    monkeypatch.setattr(brainstorm, "reply_in_database", fake_reply(calls))
    monkeypatch.setattr(ops, "create_ticket", create)
    at = open_intent()
    at.chat_input(key="intent_input").set_value("Blank names save.").run()
    at.text_input(key="intent_title").set_value("Reject blank names").run()
    at.button(key="intent_start").click().run()
    assert not at.exception, at.exception
    assert started == [
        (str(database), "Reject blank names", "Given a blank name, Then fail.", calls[0]["session"])
    ]
    assert no_real_work == [("run", {"ticket_id": 3, "live": False}, 3)]
    assert at.session_state["view"] == "Ticket" and at.session_state["ticket"] == 3
    assert "Ticket #3 created; the Design stage is running." in [s.value for s in at.success]
    at.session_state["view"] = "Board"
    at.run()
    assert at.button(key="intent_create") and not at.text_area  # the board again


def test_a_waiting_ticket_offers_the_spec_chat_with_the_spec_beside_it(
    database, monkeypatch, tmp_path
):
    calls = []
    (tmp_path / "1").mkdir()
    (tmp_path / "1" / "spec.md").write_text("# Spec: Rename a Trip\nAC-1 renames it.")
    monkeypatch.setattr(demo, "artifacts_dir", lambda path: tmp_path)
    monkeypatch.setattr(brainstorm, "reply_in_database", fake_reply(calls, draft="Trim the name."))
    at = run("Ticket")
    page = text(at)
    assert "Approve this spec?" in page and "AC-1 renames it." in page
    at.chat_input(key="spec-input-ticket-1-request-1").set_value("Why did it stop?").run()
    assert not at.exception, at.exception
    [call] = calls
    assert (call["stage"], call["ticket_id"], call["draft"]) == ("spec", 1, "")
    assert "AC-1 renames it." in call["document"] and "Approve this spec?" in call["document"]
    assert at.text_area(key="spec-note-1").value == "Trim the name."
    assert ("user", "Why did it stop?") in chat_text(at)


def test_sending_the_reply_saves_the_spec_chat_then_answers_the_request(
    database, monkeypatch, tmp_path
):
    order = []
    monkeypatch.setattr(demo, "artifacts_dir", lambda path: tmp_path)
    monkeypatch.setattr(brainstorm, "reply_in_database", fake_reply([], draft="Trim the name."))
    monkeypatch.setattr(
        demo, "archive_chat_in_database", lambda *args: order.append(("archive", *args[1:]))
    )
    monkeypatch.setattr(
        jobs, "submit", lambda path, kind, payload, ticket_id=None: order.append((kind, payload))
    )
    at = run("Ticket")
    assert at.button(key="spec-send-1").disabled
    at.chat_input(key="spec-input-ticket-1-request-1").set_value("Why did it stop?").run()
    at.button(key="spec-send-1").click().run()
    assert not at.exception, at.exception
    assert order == [
        ("archive", 1, "ticket-1-request-1", "spec", "chat-spec-1.md"),
        ("answer", {"request_id": 1, "answer": "Trim the name.", "live": False}),
    ]


def test_approving_without_a_chat_answers_and_archives_nothing(database, monkeypatch, tmp_path):
    order = []
    monkeypatch.setattr(demo, "artifacts_dir", lambda path: tmp_path)
    monkeypatch.setattr(demo, "archive_chat_in_database", lambda *args: order.append("archive"))
    monkeypatch.setattr(
        jobs, "submit", lambda path, kind, payload, ticket_id=None: order.append((kind, payload))
    )
    at = run("Ticket")
    at.button(key="spec-answer-1-approve").click().run()
    assert not at.exception, at.exception
    assert order == [("answer", {"request_id": 1, "answer": "approve", "live": False})]


def test_a_tickets_saved_chats_are_shown_with_a_download(database):
    conn = db.connect(database)
    db.add_chat_message(conn, "s1", "intent", "user", "Rename it.", ticket_id=2)
    db.add_chat_message(conn, "s1", "intent", "assistant", "To what?", ticket_id=2)
    conn.close()
    at = AppTest.from_file(ui.__file__, default_timeout=30)
    at.session_state["view"] = "Ticket"
    at.session_state["ticket"] = 2
    at.run()
    assert not at.exception, at.exception
    assert [e.label for e in at.expander if "Chat log" in e.label] == [
        "Chat log: intent (2 messages)"
    ]
    assert chat_text(at) == [("user", "Rename it."), ("assistant", "To what?")]


def ready(monkeypatch):
    monkeypatch.setattr(live, "missing_tools", lambda cfg=None: [])


def test_the_sidebar_says_whether_build_is_available(database, monkeypatch):
    assert "Design only. Missing:" in text(run("Board"))
    ready(monkeypatch)
    assert "Build and merge: ready" in text(run("Board"))


def test_with_the_tools_present_a_merge_request_gets_buttons_but_no_free_text(
    database, monkeypatch, no_real_work
):
    ready(monkeypatch)
    at = run("Inbox")
    assert [b.label for b in at.button if b.key and b.key.startswith("answer-2-")] == [
        "approve",
        "close",
    ]
    assert "scripts/live_run.py" not in text(at)
    assert not [w for w in at.text_input if w.key == "reply-2"]
    next(b for b in at.button if b.key == "answer-2-approve").click().run()
    assert no_real_work == [("answer", {"request_id": 2, "answer": "approve", "live": True}, 2)]


def test_a_request_on_a_running_ticket_has_no_buttons(database):
    conn = db.connect(database)
    db.create_job(conn, "answer", {}, 1)
    conn.close()
    at = run("Inbox")
    assert not [b for b in at.button if b.key and b.key.startswith("answer-1-")]
    assert "Running…" in text(at)


def test_a_refused_second_answer_warns_and_submits_nothing(database, monkeypatch):
    def busy(path, kind, payload, ticket_id=None):
        raise db.JobConflict("ticket 1 already has a job queued or running")

    monkeypatch.setattr(jobs, "submit", busy)
    at = run("Inbox")
    next(b for b in at.button if b.key == "answer-1-approve").click().run()
    assert not at.exception
    assert [w.value for w in at.warning] == ["Ticket #1 is already running."]


def test_the_ticket_tab_shows_a_running_job_and_hides_the_spec_chat(database):
    conn = db.connect(database)
    job = db.create_job(conn, "answer", {}, 1)
    conn.close()
    at = run("Ticket")
    assert f"Running: answer (job {job}, queued)" in text(at)
    assert not at.chat_input and at.button(key=f"job-cancel-{job}")


def test_the_ticket_tab_shows_why_the_last_run_failed(database):
    conn = db.connect(database)
    job = db.create_job(conn, "run", {}, 1)
    db.start_job(conn, job)
    db.finish_job(conn, job, error="ConnectionError: ollama is down")
    conn.close()
    assert "The last run failed: ConnectionError: ollama is down" in [
        e.value for e in run("Ticket").error
    ]


def spec_ticket_waiting_at_the_plan_gate(database):
    conn = db.connect(database)
    ticket = db.create_ticket(conn, 1, "Remove a Stop")
    db.set_status(conn, ticket, TicketStatus.SPEC)
    db.append_event(conn, ticket, "awaiting_plan", "supervisor", {})
    conn.close()
    return ticket


def open_ticket(ticket):
    at = AppTest.from_file(ui.__file__, default_timeout=30)
    at.session_state["view"] = "Ticket"
    at.session_state["ticket"] = ticket
    at.run()
    assert not at.exception, at.exception
    return at


def test_continue_to_build_is_offered_only_when_the_tools_are_present(
    database, monkeypatch, no_real_work
):
    ticket = spec_ticket_waiting_at_the_plan_gate(database)
    at = open_ticket(ticket)
    assert not [b for b in at.button if b.key == "ticket_continue"]
    assert "Build needs: No GitHub token found." in text(at)
    ready(monkeypatch)
    at = open_ticket(ticket)
    at.button(key="ticket_continue").click().run()
    assert no_real_work == [("continue", {"ticket_id": ticket}, ticket)]


def test_a_board_card_shows_a_running_ticket(database):
    conn = db.connect(database)
    db.create_job(conn, "run", {}, 2)
    conn.close()
    assert "Running…" in text(run("Board"))


def test_viewing_never_writes_even_with_jobs_and_build_tools(database, monkeypatch):
    conn = db.connect(database)
    job = db.create_job(conn, "run", {}, 1)
    db.start_job(conn, job)
    db.finish_job(conn, job, error="boom")
    conn.close()
    ready(monkeypatch)
    before = database.read_bytes()
    for view in ui.VIEWS:
        run(view)
    assert database.read_bytes() == before


def test_a_stuck_build_can_be_retried_with_a_note(database, monkeypatch, no_real_work):
    conn = db.connect(database)
    ticket = db.create_ticket(conn, 1, "Remove a Stop")
    db.append_event(conn, ticket, "implementer_result", "implementer", {"ok": True})
    db.set_status(conn, ticket, TicketStatus.NEEDS_HUMAN)
    request = db.open_human_request(
        conn, ticket, "Build stopped after 3 rounds", ["retry", "retry with a note", "close"]
    )
    conn.close()
    ready(monkeypatch)
    at = run("Inbox")
    assert not [w for w in at.text_input if w.key == f"reply-{request}"]  # no generic reply
    at.text_input(key=f"note-{request}").set_value("use trimmingCharacters").run()
    next(b for b in at.button if b.key == f"answer-{request}-retry").click().run()
    assert no_real_work == [
        (
            "answer",
            {"request_id": request, "answer": "retry: use trimmingCharacters", "live": True},
            ticket,
        )
    ]


def test_a_failed_ticket_offers_reopen_unless_it_has_a_pull_request(database, no_real_work):
    conn = db.connect(database)
    ticket = db.create_ticket(conn, 1, "Remove a Stop")
    db.set_status(conn, ticket, TicketStatus.FAILED)
    conn.close()
    at = open_ticket(ticket)
    at.button(key="ticket_reopen").click().run()
    assert no_real_work == [("reopen", {"ticket_id": ticket, "live": False}, ticket)]
    conn = db.connect(database)
    db.append_event(conn, ticket, "pr_opened", "supervisor", {"number": 9, "url": PR_URL})
    conn.close()
    assert not [b for b in open_ticket(ticket).button if b.key == "ticket_reopen"]


def test_an_interrupted_run_offers_resume_in_the_mode_it_was_started_in(database, no_real_work):
    conn = db.connect(database)
    job = db.create_job(conn, "run", {"ticket_id": 1, "live": True}, 1)
    db.start_job(conn, job)
    db.interrupt_stale_jobs(conn)
    conn.close()
    at = open_ticket(1)
    assert "The last run was cut off when the app stopped." in [w.value for w in at.warning]
    assert at.button(key="ticket_resume").disabled  # tools are missing in this test
    assert "Resume needs: No GitHub token found." in text(at)


def test_resume_is_submitted_when_the_tools_are_there(database, monkeypatch, no_real_work):
    conn = db.connect(database)
    job = db.create_job(conn, "run", {"ticket_id": 1, "live": True}, 1)
    db.start_job(conn, job)
    db.interrupt_stale_jobs(conn)
    conn.close()
    ready(monkeypatch)
    open_ticket(1).button(key="ticket_resume").click().run()
    assert no_real_work == [("resume", {"ticket_id": 1, "live": True}, 1)]


def test_settings_shows_each_option_with_the_config_files_value(database):
    at = run("Settings")
    assert at.selectbox(key="opt-router.merge_mode").value == "risk_based"
    assert at.toggle(key="opt-critic.enabled").value is False


def test_changing_an_option_saves_it_for_this_database(database):
    at = run("Settings")
    at.selectbox(key="opt-router.merge_mode").set_value("manual").run()
    assert not at.exception, at.exception
    assert settings.load(database) == {"router.merge_mode": "manual"}
    assert run("Settings").selectbox(key="opt-router.merge_mode").value == "manual"


def test_settings_runs_the_setup_checks_on_demand_and_shows_each_result(database, monkeypatch):
    results = [
        preflight.Check("Ollama", True, "reachable", "design"),
        preflight.Check("GitHub token", False, "No GitHub token found.", "build"),
    ]
    calls = []
    monkeypatch.setattr(preflight, "run_checks", lambda cfg, **kw: calls.append(1) or results)
    at = run("Settings")
    assert calls == []  # opening the tab does not run them
    at.button(key="checks_run").click().run()
    page = text(at)
    assert calls == [1] and "Ollama" in page and "No GitHub token found." in page


def test_a_new_database_is_created_and_selected(database, monkeypatch, tmp_path):
    made = tmp_path / "trial.db"

    def create(name):
        conn = db.connect(made)
        db.init_db(conn)
        conn.close()
        return made

    monkeypatch.setattr(ops, "create_database", create)
    monkeypatch.setattr(ui_data, "list_databases", lambda data_dir: [made])
    at = run("Settings")
    at.text_input(key="db_name").set_value("trial").run()
    at.button(key="db_create").click().run()
    assert not at.exception, at.exception
    assert at.session_state["db"] == made
    assert "Created trial.db." in [s.value for s in at.success]
