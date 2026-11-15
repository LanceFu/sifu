"""Sifu's Streamlit front end: a ticket board, a ticket timeline and the human inbox.

Run it with ``uv run --extra agents --extra ui streamlit run src/sifu/ui.py``.

The app is a thin layer over ``ui_data``. It reads the chosen database read-only; the one thing
it can change is an open Design-stage request, which it answers through
``demo.answer_in_database``. Requests past the Design stage (plan and merge decisions) show the
``scripts/live_run.py`` command instead, because resuming them needs the real implementer and
GitHub client.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

import streamlit as st

from sifu import demo, ui_data
from sifu.evidence import DATA, open_recorded

DB_ENV = "SIFU_UI_DB"  # open this database first instead of the newest one in data/
VIEWS = ("Board", "Ticket", "Inbox")


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


def show_board(conn: sqlite3.Connection, project_id: int | None) -> None:
    """One column per status; each card links its pull request and can open the timeline."""
    columns = ui_data.board(conn, project_id)
    for slot, (status, cards) in zip(st.columns(len(columns)), columns.items(), strict=True):
        with slot:
            st.markdown(f"**{status} ({len(cards)})**")
            for card in cards:
                with st.container(border=True):
                    st.markdown(f"**#{card['id']}** {card['title']}")
                    if card["pr_url"]:
                        st.markdown(f"[PR #{card['pr_number']}]({card['pr_url']})")
                    if card["waiting"]:
                        st.caption("Waiting for you")
                    st.button(
                        "Timeline",
                        key=f"open-{card['id']}",
                        on_click=_open_timeline,
                        args=(card["id"],),
                    )


def show_ticket(conn: sqlite3.Connection, project_id: int | None) -> None:
    """A ticket's events, oldest first, with router decisions set apart."""
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
    st.markdown(f"Status: **{ticket['status']}**")
    if by_id[ticket_id]["pr_url"]:
        st.markdown(f"[PR #{by_id[ticket_id]['pr_number']}]({by_id[ticket_id]['pr_url']})")
    with st.expander("Ticket text"):
        st.text(ticket["body"] or "(empty)")
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


def _answer(db_path: Path, item: dict[str, Any], answer: str) -> None:
    """Answer a Design-stage request and report the ticket's new status."""
    try:
        with st.spinner("Resuming the ticket (this calls the local models)…"):
            status = demo.answer_in_database(db_path, item["id"], answer)
    except Exception as exc:  # shown to the person; the request stays open
        st.error(f"Could not resume ticket #{item['ticket_id']}: {exc}")
        return
    st.session_state["notice"] = f"Ticket #{item['ticket_id']} resumed; status is now {status}."
    st.rerun()


def show_inbox(conn: sqlite3.Connection, db_path: Path) -> None:
    """Open human requests. Design-stage ones are answered here; later ones by command line."""
    if notice := st.session_state.pop("notice", None):
        st.success(notice)
    items = ui_data.inbox(conn)
    if not items:
        st.info("Nothing is waiting for you.")
        return
    for item in items:
        with st.container(border=True):
            st.markdown(
                f"**Request {item['id']}** · ticket #{item['ticket_id']} {item['ticket_title']}"
            )
            st.markdown(item["question"])
            if item["rules"]:
                st.caption("Rules fired: " + ", ".join(item["rules"]))
            if item["needs_live_tools"]:
                st.info(
                    "This ticket is past the Design stage. Answering continues into Build or "
                    "the merge on GitHub, so answer it from a terminal:"
                )
                st.code(
                    f"uv run python scripts/live_run.py --db {db_path} "
                    f"--answer {item['id']} approve",
                    language="bash",
                )
                continue
            for action in item["actions"]:
                if st.button(action, key=f"answer-{item['id']}-{action}"):
                    _answer(db_path, item, action)
            reply = st.text_input("Reply in your own words", key=f"reply-{item['id']}")
            if st.button("Send reply", key=f"send-{item['id']}") and reply.strip():
                _answer(db_path, item, reply.strip())


def main() -> None:
    """Lay out the sidebar and one tab per view; only the open tab's content is built."""
    st.set_page_config(page_title="Sifu", layout="wide")
    choices = database_choices()
    if not choices:
        st.title("Sifu")
        st.info(f"No database in {DATA}. Run the demo notebook or scripts/live_run.py first.")
        return
    with st.sidebar:
        st.title("Sifu")
        db_path = st.selectbox("Database", choices, format_func=lambda p: p.name)
        conn = open_recorded(db_path)
        projects = conn.execute("SELECT id, name FROM projects ORDER BY id").fetchall()
        names = {p["id"]: p["name"] for p in projects}
        project_id = st.selectbox(
            "Project", [None, *names], format_func=lambda i: names.get(i, "All projects")
        )
        waiting = len(ui_data.inbox(conn))
        st.caption(f"{waiting} request{'' if waiting == 1 else 's'} waiting for you")
    try:
        if st.session_state.get("view") not in VIEWS:
            st.session_state["view"] = VIEWS[0]
        # on_change="rerun" makes the tabs track state: session_state["view"] holds the open
        # tab (the board's Timeline button sets it) and .open is true for that tab alone.
        board, ticket, inbox = st.tabs(VIEWS, key="view", on_change="rerun")
        if board.open:
            with board:
                show_board(conn, project_id)
        if ticket.open:
            with ticket:
                show_ticket(conn, project_id)
        if inbox.open:
            with inbox:
                show_inbox(conn, db_path)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
