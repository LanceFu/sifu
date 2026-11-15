"""Sifu's Streamlit front end: a ticket board, a ticket timeline and the human inbox.

Run it with ``uv run --extra agents --extra ui streamlit run src/sifu/ui.py``.

The app is a thin layer over ``ui_data``. Looking at a tab reads the chosen database read-only.
Three kinds of action write to it, each through a function that is tested without Streamlit:

- a chat message (a new ticket's draft, or the spec chat on a waiting ticket) is stored and answered
  by ``brainstorm.reply_in_database``;
- "Create" on a new ticket's draft files it with ``ops.create_ticket`` and queues its run with
  ``jobs.submit``;
- an answer to an open request, and "Continue to Build", are queued with ``jobs.submit``.

Jobs run one at a time on a background worker, so the page stays usable; the sidebar polls the
``jobs`` table and the page reruns when a job starts or ends. Plan and merge requests can be
answered here when the build tools the sidebar lists are present; otherwise the app stays in
design mode and the sidebar says what is missing.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import streamlit as st

from sifu import brainstorm, db, demo, jobs, live, ops, preflight, settings, ui_data
from sifu.evidence import DATA, open_recorded

DB_ENV = "SIFU_UI_DB"  # open this database first instead of the newest one in data/
VIEWS = ("Board", "Ticket", "Inbox", "Settings")
CHAT_PARAM = "chat"  # the open intent chat's session id, kept in the URL so a reload keeps it
_SESSION = re.compile(r"[0-9a-f]{32}")
# A vertical rule between the chat and the preview; Streamlit only has a horizontal divider.
_DIVIDER = (
    '<div style="border-left: 1px solid rgba(128, 128, 128, 0.35); height: 640px; '
    'margin: 0 auto; width: 0;"></div>'
)


# Statuses that need attention are colour coded wherever a status is shown.
STATUS_COLOURS = {"needs_human": "yellow", "failed": "red"}


def status_label(status: str, text: str | None = None) -> str:
    """Markdown for a status: bold, and a coloured badge for the ones that need attention."""
    text = text or status
    colour = STATUS_COLOURS.get(status)
    return f":{colour}-badge[**{text}**]" if colour else f"**{text}**"


def database_choices() -> list[Path]:
    """The databases the sidebar offers: ``$SIFU_UI_DB`` first when set, then ``data/*.db``."""
    found = ui_data.list_databases(DATA)
    preset = os.environ.get(DB_ENV)
    if preset and Path(preset) not in found:
        found.insert(0, Path(preset))
    elif preset:
        found.insert(0, found.pop(found.index(Path(preset))))
    return found


def _open_timeline(ticket_id: int) -> None:
    """Button callback: select the ticket and switch to the Ticket tab.

    A callback runs before the next rerun builds the widgets, which is the only time the
    selected tab and ticket can be set through session state.
    """
    st.session_state["ticket"] = ticket_id
    st.session_state["view"] = "Ticket"


def _missing() -> list[str]:
    """What Build and merge still need on this machine, checked once per browser session."""
    if "live_missing" not in st.session_state:
        st.session_state["live_missing"] = live.missing_tools()
    return st.session_state["live_missing"]


def _recheck() -> None:
    """Button callback: look for the build tools again on the next rerun."""
    st.session_state.pop("live_missing", None)


@st.fragment(run_every=3)
def _watch_jobs(db_path: Path) -> None:
    """Show how many jobs run and rerun the whole app when a job starts, ends or changes.

    It reruns only on a change: a rerun every few seconds would disturb typing.
    """
    conn = open_recorded(db_path)
    try:
        active = ui_data.jobs(conn, active_only=True)
    finally:
        conn.close()
    if active:
        st.caption(f"{len(active)} job{'s' if len(active) != 1 else ''} running")
    seen = tuple((j["id"], j["status"]) for j in active)
    if "jobs_seen" not in st.session_state:
        st.session_state["jobs_seen"] = seen
    elif st.session_state["jobs_seen"] != seen:
        st.session_state["jobs_seen"] = seen
        st.rerun()


def _drafting() -> bool:
    """Whether the new-ticket draft is open; a reloaded page reopens the chat in its URL."""
    if "intent_open" not in st.session_state:
        linked = st.query_params.get(CHAT_PARAM, "")
        st.session_state["intent_open"] = bool(_SESSION.fullmatch(linked))
    return bool(st.session_state["intent_open"])


def show_board(conn: sqlite3.Connection, db_path: Path, project_id: int | None) -> None:
    """One column per status; each card links its pull request and can open the timeline.

    "New ticket" swaps the board for the draft (a chat and the ticket preview) until the ticket
    is created or the draft is cancelled.
    """
    if _drafting():
        show_intent(conn, db_path)
        return
    st.button(
        "New ticket",
        icon=":material/add:",
        type="primary",
        on_click=_open_intent,
        key="intent_create",
    )
    columns = ui_data.board(conn, project_id)
    for slot, (status, cards) in zip(st.columns(len(columns)), columns.items(), strict=True):
        with slot:
            st.markdown(status_label(status, f"{status} ({len(cards)})"))
            for card in cards:
                with st.container(border=True):
                    st.markdown(f"**#{card['id']}** {card['title']}")
                    if card["pr_url"]:
                        st.markdown(f"[PR #{card['pr_number']}]({card['pr_url']})")
                    if card["running"]:
                        st.caption("Running…")
                    if card["waiting"]:
                        st.caption("Waiting for you")
                    st.button(
                        "Details",
                        key=f"open-{card['id']}",
                        on_click=_open_timeline,
                        args=(card["id"],),
                    )


def _show_chat(messages: list[dict[str, Any]], empty: str, height: int = 420) -> None:
    """The conversation so far in a fixed-height box, read back from the database."""
    with st.container(height=height):
        if not messages:
            st.caption(empty)
        for message in messages:
            st.chat_message(message["role"]).markdown(message["content"])


def _chat_turn(
    db_path: Path, session: str, stage: str, text: str, **draft: Any
) -> brainstorm.BrainstormTurn | None:
    """Store the person's message and get the model's turn; an error is shown, not raised."""
    try:
        with st.spinner("Thinking (this calls the local model)…"):
            return brainstorm.reply_in_database(db_path, session, stage, text, **draft)
    except Exception as exc:  # the person's message is already stored
        st.error(f"The chat could not get a reply: {exc}")
        return None


def _last_draft(messages: list[dict[str, Any]]) -> tuple[str, str]:
    """The draft as the model last left it: (title, text) from the newest assistant message."""
    for message in reversed(messages):
        if message["role"] == "assistant":
            return message["title"] or "", message["draft"] or ""
    return "", ""


def _intent_session() -> str:
    """This browser session's intent chat id: the one in the URL, else a new one."""
    if "intent_session" not in st.session_state:
        linked = st.query_params.get(CHAT_PARAM, "")
        st.session_state["intent_session"] = (
            linked if _SESSION.fullmatch(linked) else uuid.uuid4().hex
        )
    return st.session_state["intent_session"]


def _open_intent() -> None:
    """Button callback: show the new ticket's draft in place of the board."""
    st.session_state["intent_open"] = True


def _close_intent() -> None:
    """Button callback: leave the draft (its chat stays in the database) and show the board."""
    st.session_state.pop("intent_session", None)
    st.query_params.pop(CHAT_PARAM, None)
    st.session_state["intent_title"] = st.session_state["intent_body"] = ""
    st.session_state["intent_open"] = False


def show_intent(conn: sqlite3.Connection, db_path: Path) -> None:
    """A new ticket's draft: a chat on the left and the ticket preview on the right."""
    st.markdown("#### New ticket")
    # A widget's value can only be set before the widget is built, so a new draft waits here.
    if (pending := st.session_state.pop("intent_pending", None)) is not None:
        st.session_state["intent_title"], st.session_state["intent_body"] = pending
    session = _intent_session()
    messages = ui_data.chat_messages(conn, session)
    if "intent_body" not in st.session_state:  # a reloaded page picks its draft up again
        st.session_state["intent_title"], st.session_state["intent_body"] = _last_draft(messages)
    chat_col, rule, preview_col = st.columns([20, 1, 20])
    with chat_col:
        st.markdown("**Chat**")
        st.caption("Talk the change through. The chat is saved as you go.")
        _show_chat(messages, "No messages yet. Say what should change, and for whom.")
        prompt = st.chat_input("What should change, and for whom?", key="intent_input")
    rule.html(_DIVIDER)
    with preview_col:
        st.markdown("**Preview**")
        st.caption(
            "The ticket as it would be filed. The model rewrites it as you talk and you can "
            "edit it; nothing is filed until you press Create."
        )
        title = st.text_input("Title", key="intent_title")
        body = st.text_area("Description", key="intent_body", height=300)
        for gap in brainstorm.draft_gaps(title, body):
            st.caption(gap)
        start, cancel, download = st.columns([4, 2, 4])
        ready = bool(title.strip() and body.strip())
        started = start.button("Create", type="primary", disabled=not ready, key="intent_start")
        cancel.button("Cancel", on_click=_close_intent, key="intent_cancel")
        if messages:
            download.download_button(
                "Download chat log",
                brainstorm.transcript_md(messages, "Chat (intent)"),
                file_name=f"chat-intent-{session[:8]}.md",
                mime="text/markdown",
                key="intent_download",
            )
    if prompt:
        turn = _chat_turn(db_path, session, "intent", prompt, title=title, draft=body)
        if turn is not None:
            st.session_state["intent_pending"] = (turn.title, turn.draft)
            st.query_params[CHAT_PARAM] = session
            st.rerun()
    if started:
        _start(db_path, title, body, session if messages else None)


def _start(db_path: Path, title: str, body: str, session: str | None) -> None:
    """Create the ticket, queue its run and open it in the Ticket tab."""
    try:
        ticket_id = ops.create_ticket(db_path, title, body, session)
        payload = {"ticket_id": ticket_id, "live": not _missing()}
        jobs.submit(db_path, "run", payload, ticket_id=ticket_id)
    except Exception as exc:  # shown to the person; the draft stays so nothing typed is lost
        st.error(
            f"Could not start the ticket: {exc}. If it was created it is on the Board at "
            "intent; starting again creates a new one."
        )
        return
    st.session_state.pop("intent_session", None)
    st.query_params.pop(CHAT_PARAM, None)
    st.session_state["intent_pending"] = ("", "")
    st.session_state["intent_open"] = False
    st.session_state["goto"] = ("Ticket", ticket_id)
    st.session_state["notice"] = f"Ticket #{ticket_id} created; the Design stage is running."
    st.rerun()


def _request_document(db_path: Path, ticket: sqlite3.Row, request: dict[str, Any]) -> str:
    """What a spec chat is about: the spec the router stopped, when there is one on disk."""
    if request["stage"] != "accept_spec":
        return ""  # a questions request carries the questions in its own text
    path = demo.artifacts_dir(db_path) / str(ticket["id"]) / "spec.md"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def show_spec_chat(
    conn: sqlite3.Connection, db_path: Path, ticket: sqlite3.Row, request: dict[str, Any]
) -> None:
    """A waiting Design-stage request: the document, a chat about it and the answer to send."""
    session = f"ticket-{ticket['id']}-request-{request['id']}"
    note_key = f"spec-note-{request['id']}"
    if (pending := st.session_state.pop(f"{note_key}-pending", None)) is not None:
        st.session_state[note_key] = pending
    messages = ui_data.chat_messages(conn, session)
    if note_key not in st.session_state:
        st.session_state[note_key] = _last_draft(messages)[1]
    spec = _request_document(db_path, ticket, request)
    with st.container(border=True):
        st.markdown(f"**Waiting for you** · request {request['id']}")
        doc_col, chat_col = st.columns(2)
        with doc_col:
            st.markdown(request["question"])
            if request["rules"]:
                st.caption("Rules fired: " + ", ".join(request["rules"]))
            if spec:
                with st.container(height=320):
                    st.code(spec, language="markdown", wrap_lines=True)
        with chat_col:
            _show_chat(messages, "No messages yet. Ask about the document before you answer.")
            prompt = st.chat_input("Ask about this before you answer", key=f"spec-input-{session}")
        note = st.text_area(
            "Your reply to the Design stage",
            key=note_key,
            help="Answers or a revision note. It is added to the ticket and the spec is rewritten.",
        )
        chat = session if messages else None
        slots = st.columns(len(request["actions"]) + 1)
        for slot, action in zip(slots, request["actions"], strict=False):
            if slot.button(action, key=f"spec-answer-{request['id']}-{action}"):
                _answer(db_path, request, action, chat)
        send = slots[-1].button(
            "Send reply", key=f"spec-send-{request['id']}", disabled=not note.strip()
        )
        if send:
            _answer(db_path, request, note.strip(), chat)
    if prompt:
        document = "\n\n".join(
            part
            for part in (
                f"Ticket: {ticket['title']}\n{ticket['body']}",
                f"Why it stopped: {request['question']}",
                spec,
            )
            if part
        )
        turn = _chat_turn(
            db_path,
            session,
            "spec",
            prompt,
            draft=note,
            document=document,
            ticket_id=ticket["id"],
        )
        if turn is not None:
            st.session_state[f"{note_key}-pending"] = turn.draft
            st.rerun()


def _show_job_state(
    conn: sqlite3.Connection, db_path: Path, ticket_id: int, active: dict[str, Any] | None
) -> None:
    """The ticket's running job, the reason its last run ended badly, or the way on to Build."""
    if active:
        st.info(f"Running: {active['kind']} (job {active['id']}, {active['status']})")
        st.button("Refresh", key="ticket_refresh")  # a click is a rerun, which reads the table
        if active["status"] == "queued" and st.button("Cancel", key=f"job-cancel-{active['id']}"):
            jobs.cancel(db_path, active["id"])
            st.rerun()
        return
    last = ui_data.last_job(conn, ticket_id)
    if last and last["status"] == "failed":
        st.error(f"The last run failed: {last['error']}")
    elif last and last["status"] == "interrupted":
        st.warning("The last run was cut off when the app stopped.")
        _resume_button(db_path, ticket_id, last)
    elif ui_data.awaiting_build(conn, ticket_id):
        missing = _missing()
        if missing:
            st.caption("This ticket's spec is accepted. Build needs: " + "; ".join(missing))
        elif st.button("Continue to Build", type="primary", key="ticket_continue"):
            _submit(db_path, "continue", {"ticket_id": ticket_id}, ticket_id)
    if ui_data.can_reopen(conn, ticket_id) and st.button("Reopen", key="ticket_reopen"):
        _submit(db_path, "reopen", {"ticket_id": ticket_id, "live": not _missing()}, ticket_id)


def _resume_button(db_path: Path, ticket_id: int, last: dict[str, Any]) -> None:
    """Resume a cut-off run in the mode it was started in; live needs the build tools."""
    live_run = json.loads(last["payload"] or "{}").get("live", True)
    missing = _missing() if live_run else []
    if missing:
        st.caption("Resume needs: " + "; ".join(missing))
    if st.button("Resume", key="ticket_resume", disabled=bool(missing)):
        _submit(db_path, "resume", {"ticket_id": ticket_id, "live": live_run}, ticket_id)


def _submit(db_path: Path, kind: str, payload: dict[str, Any], ticket_id: int) -> None:
    """Queue a job; a ticket that is already running is refused with a message, not an error."""
    try:
        jobs.submit(db_path, kind, payload, ticket_id=ticket_id)
    except db.JobConflict:
        st.warning(f"Ticket #{ticket_id} is already running.")
        return
    st.rerun()


def show_ticket(conn: sqlite3.Connection, db_path: Path, project_id: int | None) -> None:
    """A ticket's events, oldest first, with router decisions set apart.

    A ticket with a job queued or running shows it (and a Cancel while it is queued) instead of
    the spec chat; a failed or interrupted last run is explained, and a ticket whose accepted spec
    waits at the plan gate offers "Continue to Build". Every saved chat can be read and downloaded.
    """
    cards = [c for column in ui_data.board(conn, project_id).values() for c in column]
    if not cards:
        st.info("No tickets in this database.")
        return
    by_id = {c["id"]: c for c in sorted(cards, key=lambda c: c["id"])}
    if st.session_state.get("ticket") not in by_id:
        st.session_state["ticket"] = next(iter(by_id))
    ticket_id = st.selectbox(
        "Ticket", list(by_id), format_func=lambda i: f"#{i} {by_id[i]['title']}", key="ticket"
    )
    ticket = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    st.markdown(f"Status: {status_label(ticket['status'])}")
    active = ui_data.active_job(conn, ticket_id)
    _show_job_state(conn, db_path, ticket_id, active)
    if by_id[ticket_id]["pr_url"]:
        st.markdown(f"[PR #{by_id[ticket_id]['pr_number']}]({by_id[ticket_id]['pr_url']})")
    with st.expander("Ticket text"):
        st.text(ticket["body"] or "(empty)")
    if not active and (request := ui_data.design_request(conn, ticket_id)):
        show_spec_chat(conn, db_path, ticket, request)
    for chat in ui_data.chats(conn, ticket_id):
        count = len(chat["messages"])
        with st.expander(f"Chat log: {chat['stage']} ({count} messages)"):
            for message in chat["messages"]:
                st.chat_message(message["role"]).markdown(message["content"])
            st.download_button(
                "Download chat log",
                brainstorm.transcript_md(chat["messages"], f"Chat ({chat['stage']})"),
                file_name=f"chat-{chat['stage']}-ticket-{ticket_id}.md",
                mime="text/markdown",
                key=f"download-{chat['session']}",
            )
    key_only = st.toggle("Key events only", value=True, key="key_only")
    for row in ui_data.timeline(conn, ticket_id, key_only=key_only):
        line = f"`{row['at']}` **{row['kind']}** · {row['actor']} — {row['summary']}"
        if row["is_decision"]:
            with st.container(border=True):
                st.markdown(line)
                for item in row["detail"]:
                    st.caption(item)
        else:
            st.markdown(line)
            if row["detail"]:
                with st.expander(f"{len(row['detail'])} details"):
                    for item in row["detail"]:
                        st.caption(item)


def _answer(db_path: Path, item: dict[str, Any], answer: str, chat: str | None = None) -> None:
    """Queue the answer to an open request; the ticket resumes on the background worker.

    ``chat`` is the spec chat that led to the answer; it is written beside the ticket's artifacts
    and logged on its timeline before the answer is queued.
    """
    ticket_id = item["ticket_id"]
    try:
        if chat:
            demo.archive_chat_in_database(
                db_path, ticket_id, chat, "spec", f"chat-spec-{item['id']}.md"
            )
        payload = {"request_id": item["id"], "answer": answer, "live": not _missing()}
        jobs.submit(db_path, "answer", payload, ticket_id=ticket_id)
    except db.JobConflict:
        st.warning(f"Ticket #{ticket_id} is already running.")
        return
    except Exception as exc:  # shown to the person; the request stays open
        st.error(f"Could not send the answer for ticket #{ticket_id}: {exc}")
        return
    st.session_state["notice"] = f"Ticket #{ticket_id}: your answer was sent."
    st.rerun()


def show_inbox(conn: sqlite3.Connection, db_path: Path) -> None:
    """Open human requests, answered here. Build and merge ones need the build tools."""
    items = ui_data.inbox(conn)
    if not items:
        st.info("Nothing is waiting for you.")
        return
    missing = _missing()
    for item in items:
        with st.container(border=True):
            st.markdown(
                f"**Request {item['id']}** · ticket #{item['ticket_id']} {item['ticket_title']}"
            )
            st.markdown(item["question"])
            if item["rules"]:
                st.caption("Rules fired: " + ", ".join(item["rules"]))
            if ui_data.active_job(conn, item["ticket_id"]):
                st.caption("Running…")
                continue
            if item["needs_live_tools"] and missing:
                st.info(
                    "This ticket is past the Design stage. Answering continues into Build or "
                    "the merge on GitHub, which needs tools this machine lacks. Answer it from "
                    "a terminal:"
                )
                for tool in missing:
                    st.caption(tool)
                st.code(
                    f"uv run python scripts/live_run.py --db {db_path} "
                    f"--answer {item['id']} approve",
                    language="bash",
                )
                continue
            note = ""
            if item["takes_note"]:
                note = st.text_input(
                    "Note for the implementer (optional)", key=f"note-{item['id']}"
                ).strip()
            for action in item["actions"]:
                if st.button(action, key=f"answer-{item['id']}-{action}"):
                    _answer(
                        db_path, item, f"retry: {note}" if action == "retry" and note else action
                    )
            if item["stage"] == "merge" or "retry" in item["options"]:
                continue  # a merge or a stuck request takes no free-text reply
            reply = st.text_input("Reply in your own words", key=f"reply-{item['id']}")
            if st.button("Send reply", key=f"send-{item['id']}") and reply.strip():
                _answer(db_path, item, reply.strip())


def _save_option(db_path: Path, key: str) -> None:
    """Widget callback: save the option's new value for this database."""
    settings.save(db_path, key, st.session_state[f"opt-{key}"])


def _reset_options(db_path: Path) -> None:
    """Button callback: every run option goes back to the value in config.yaml."""
    for key in settings.OPTIONS:
        settings.clear(db_path, key)
        st.session_state.pop(f"opt-{key}", None)


def _run_checks(db_path: Path) -> None:
    """Button callback: run the setup checks and refresh the sidebar's build-tools line."""
    st.session_state["checks"] = preflight.run_checks(ops.effective_config(db_path))
    st.session_state.pop("live_missing", None)


def show_settings(conn: sqlite3.Connection, db_path: Path) -> None:
    """Setup checks, run options and the read-only configuration; writes only in callbacks."""
    cfg = ops.effective_config(db_path)
    st.markdown("**Setup checks**")
    st.button("Run checks", key="checks_run", on_click=_run_checks, args=(db_path,))
    for check in st.session_state.get("checks", []):
        badge = ":green-badge[ok]" if check.ok else ":red-badge[missing]"
        st.markdown(f"{badge} **{check.name}** — {check.detail}")
        if check.needed_for == "build":
            st.caption("needed for Build")
    st.markdown("**Run options**")
    st.caption(
        "These apply to this database only and override config.yaml. "
        "Risk floor rules apply in every mode."
    )
    for key, allowed in settings.OPTIONS.items():
        section, field = key.split(".")
        current = getattr(getattr(cfg, section), field)
        widget_key = f"opt-{key}"
        if isinstance(allowed[0], bool):
            st.toggle(
                key, value=current, key=widget_key, on_change=_save_option, args=(db_path, key)
            )
        else:
            st.selectbox(
                key,
                list(allowed),
                index=list(allowed).index(current),
                key=widget_key,
                on_change=_save_option,
                args=(db_path, key),
            )
    st.button("Use config.yaml values", key="opt-reset", on_click=_reset_options, args=(db_path,))
    st.markdown("**Configuration (read-only)**")
    st.json(cfg.model_dump(), expanded=False)
    st.markdown("**New database**")
    name = st.text_input("Name", key="db_name", help="Lowercase letters, digits and hyphens.")
    if st.button("Create database", key="db_create"):
        try:
            path = ops.create_database(name)
        except (ValueError, FileExistsError) as exc:
            st.error(str(exc))
        else:
            st.session_state["db_pending"] = path
            st.session_state["notice"] = f"Created {path.name}."
            st.rerun()


def main() -> None:
    """Lay out the sidebar and one tab per view; only the open tab's content is built."""
    st.set_page_config(page_title="Sifu", layout="wide")
    choices = database_choices()
    if not choices:
        st.title("Sifu")
        st.info(f"No database in {DATA}. Run the demo notebook or scripts/live_run.py first.")
        return
    # Starting a ticket asks for the Ticket tab; widget state can only be set before the widgets.
    if goto := st.session_state.pop("goto", None):
        st.session_state["view"], st.session_state["ticket"] = goto
    with st.sidebar:
        st.title("Sifu")
        if (pending := st.session_state.pop("db_pending", None)) is not None:
            st.session_state["db"] = pending  # a new database is selected on the next run
        db_path = st.selectbox("Database", choices, format_func=lambda p: p.name, key="db")
        jobs.recover(db_path)  # jobs a dead process left behind are marked interrupted
        conn = open_recorded(db_path)
        projects = conn.execute("SELECT id, name FROM projects ORDER BY id").fetchall()
        names = {p["id"]: p["name"] for p in projects}
        project_id = st.selectbox(
            "Project", [None, *names], format_func=lambda i: names.get(i, "All projects")
        )
        waiting = len(ui_data.inbox(conn))
        st.caption(f"{waiting} request{'' if waiting == 1 else 's'} waiting for you")
        missing = _missing()
        if missing:
            st.markdown("**Design only. Missing:**")
            for tool in missing:
                st.caption(tool)
            st.button("Check again", key="live_recheck", on_click=_recheck)
        else:
            st.markdown("**Build and merge: ready**")
        _watch_jobs(db_path)
    try:
        if st.session_state.get("view") not in VIEWS:
            st.session_state["view"] = VIEWS[0]
        # on_change="rerun" makes the tabs track state: session_state["view"] holds the open
        # tab (the board's Details button sets it) and .open is true for that tab alone.
        if notice := st.session_state.pop("notice", None):
            st.success(notice)
        board, ticket, inbox, setup = st.tabs(VIEWS, key="view", on_change="rerun")
        if board.open:
            with board:
                show_board(conn, db_path, project_id)
        if ticket.open:
            with ticket:
                show_ticket(conn, db_path, project_id)
        if inbox.open:
            with inbox:
                show_inbox(conn, db_path)
        if setup.open:
            with setup:
                show_settings(conn, db_path)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
