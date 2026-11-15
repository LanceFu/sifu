"""Queries behind the Streamlit UI: the board, a ticket's timeline, the inbox and the chats.

Everything here is a read over the audit tables in ``db.py`` and returns plain dicts and lists, so
it is tested without Streamlit. ``ui.py`` only lays the results out.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from sifu import db
from sifu.models import DecisionType, RiskTier, TicketStatus

# Once one of these is logged the ticket is past Design: resuming it needs the real implementer,
# verifier and GitHub client, which only scripts/live_run.py wires up.
BUILD_KINDS = ("implementer_result", "pr_opened", "draft_pr_opened")
LIVE_DECISIONS = (DecisionType.ACCEPT_PLAN.value, DecisionType.MERGE.value)
# The graph reads an answer that starts with one of these as a command; anything else is feedback.
COMMANDS = ("approve", "close")
# What "key events only" keeps: who decided what, what the tools found and what reached GitHub.
KEY_KINDS = frozenset(
    {
        "ticket_created",
        "decision_recorded",
        "implementer_result",
        "verify_result",
        "critic_result",
        "verify_cap",
        "verify_no_progress",
        "critic_cap",
        "critic_no_progress",
        "design_round_cap",
        "build_requested",
        "pr_opened",
        "draft_pr_opened",
        "merge_blocked",
        "pr_merged",
        "human_request_opened",
        "human_request_answered",
        "lesson_proposed",
        "chat_saved",
        "ticket_closed",
        "error",
    }
)


def list_databases(data_dir: str | Path) -> list[Path]:
    """The ``.db`` files in a folder, by name; SQLite's -wal and -shm sidecars are skipped."""
    return sorted(p for p in Path(data_dir).glob("*.db") if p.is_file())


def _payload(event: sqlite3.Row) -> dict[str, Any]:
    return json.loads(event["payload"] or "{}")


def board(conn: sqlite3.Connection, project_id: int | None = None) -> dict[str, list[dict]]:
    """Tickets grouped by status, one column per status in pipeline order (empty ones included).

    Each card has the ticket's id, title and last update, its latest pull request if it has one,
    ``waiting``: whether a human request is open on it, and ``running``: whether it has a job
    queued or running.
    """
    where, args = ("WHERE project_id = ?", (project_id,)) if project_id is not None else ("", ())
    tickets = conn.execute(f"SELECT * FROM tickets {where} ORDER BY id", args).fetchall()
    waiting = {
        r["ticket_id"]
        for r in conn.execute("SELECT ticket_id FROM human_requests WHERE status = 'open'")
    }
    running = {j["ticket_id"] for j in jobs(conn, active_only=True)}
    prs: dict[int, dict[str, Any]] = {}
    for event in conn.execute(
        "SELECT * FROM events WHERE kind IN ('pr_opened', 'draft_pr_opened') ORDER BY id"
    ):
        prs[event["ticket_id"]] = _payload(event)
    columns: dict[str, list[dict]] = {status.value: [] for status in TicketStatus}
    for t in tickets:
        pr = prs.get(t["id"], {})
        columns.setdefault(t["status"], []).append(
            {
                "id": t["id"],
                "title": t["title"],
                "updated_at": t["updated_at"],
                "pr_number": pr.get("number"),
                "pr_url": pr.get("url"),
                "waiting": t["id"] in waiting,
                "running": t["id"] in running,
            }
        )
    return columns


def jobs(conn: sqlite3.Connection, active_only: bool = False) -> list[dict[str, Any]]:
    """Background jobs, newest first; empty when the database was recorded before jobs existed."""
    try:
        return [dict(row) for row in db.list_jobs(conn, active_only)]
    except sqlite3.OperationalError:
        return []


def active_job(conn: sqlite3.Connection, ticket_id: int) -> dict[str, Any] | None:
    """The ticket's queued or running job, if it has one."""
    return next((j for j in jobs(conn, active_only=True) if j["ticket_id"] == ticket_id), None)


def last_job(conn: sqlite3.Connection, ticket_id: int) -> dict[str, Any] | None:
    """The ticket's newest job of any status."""
    return next((j for j in jobs(conn) if j["ticket_id"] == ticket_id), None)


def awaiting_build(conn: sqlite3.Connection, ticket_id: int) -> bool:
    """True when the ticket's spec is accepted and it waits at the plan gate for Build.

    That is: status ``spec``, its newest ``awaiting_plan`` event is not followed by a build or
    a plan step, and no human request is open on it.
    """
    ticket = conn.execute("SELECT status FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if ticket is None or ticket["status"] != TicketStatus.SPEC.value:
        return False
    gate = conn.execute(
        "SELECT MAX(id) FROM events WHERE ticket_id = ? AND kind = 'awaiting_plan'", (ticket_id,)
    ).fetchone()[0]
    if gate is None:
        return False
    later = conn.execute(
        "SELECT 1 FROM events WHERE ticket_id = ? AND id > ?"
        " AND kind IN ('build_requested', 'implementer_result', 'stub_ran')",
        (ticket_id, gate),
    ).fetchone()
    opened = conn.execute(
        "SELECT 1 FROM human_requests WHERE ticket_id = ? AND status = 'open'", (ticket_id,)
    ).fetchone()
    return later is None and opened is None


def _tier(value: Any) -> str:
    return RiskTier(int(value)).name


def _finding(f: dict[str, Any]) -> str:
    where = f.get("file") or ""
    if where and f.get("line") is not None:
        where += f":{f['line']}"
    head = " ".join(part for part in (f.get("source"), f.get("rule"), where) if part)
    return f"{head}: {f.get('message', '')}"


def _decision_row(decision: sqlite3.Row) -> tuple[str, list[str]]:
    rules = ", ".join(json.loads(decision["rules_fired"])) or "none"
    summary = (
        f"{decision['decision_type']}: floor {_tier(decision['floor_tier'])}, "
        f"final {_tier(decision['final_tier'])}; rules: {rules}; {decision['resolution']}"
    )
    detail = [
        f"{a['assessor']}: {_tier(a['tier'])} (confidence {a['confidence']}). {a['rationale']}"
        for a in json.loads(decision["assessments"])
    ]
    return summary, detail


def _summarise(kind: str, p: dict[str, Any]) -> tuple[str, list[str]]:
    """A one-line summary and optional detail lines for an event of a known kind."""
    if kind == "ticket_created":
        return p.get("title", ""), []
    if kind == "status_changed":
        return f"{p.get('from')} → {p.get('to')}", []
    if kind == "verify_result":
        steps = ", ".join(
            f"{s.get('name')} {'ok' if s.get('ok') else 'FAILED'}" for s in p.get("steps", [])
        )
        findings = p.get("findings", [])
        count = p.get("finding_count", len(findings))
        noun = "finding" if count == 1 else "findings"
        result = "passed" if p.get("ok") else "failed"
        return f"{result}: {steps}; {count} {noun}", [_finding(f) for f in findings]
    if kind == "implementer_result":
        files = p.get("changed_files") or []
        result = "ok" if p.get("ok") else f"failed ({p.get('error')})"
        return f"{result}: {p.get('num_turns')} turns, {len(files)} files changed", list(files)
    if kind in ("pr_opened", "draft_pr_opened"):
        return f"#{p.get('number')} {p.get('url', '')}".strip(), []
    if kind == "pr_merged":
        return f"#{p.get('number')} merged", []
    if kind == "agent_output" and "report" in p:
        findings = p["report"].get("findings", [])
        return f"{len(findings)} findings", [
            f"{f.get('severity', '')} {f.get('category', '')}: {f.get('message', '')}".strip()
            for f in findings
        ]
    if kind == "agent_output" and "findings" in p:
        return f"{len(p['findings'])} findings", [_finding(f) for f in p["findings"]]
    if kind == "agent_output":
        return str(p.get("path", "")), []
    if kind == "chat_saved":
        return f"{p.get('stage')} chat, {p.get('messages')} messages", [str(p.get("path", ""))]
    if kind == "human_request_opened":
        return f"request {p.get('request_id')}", []
    return ", ".join(f"{k}={v}" for k, v in p.items() if not isinstance(v, (dict, list))), []


def timeline(
    conn: sqlite3.Connection, ticket_id: int, key_only: bool = False
) -> list[dict[str, Any]]:
    """The ticket's events, oldest first, each with a one-line summary.

    ``key_only`` keeps the decisions, tool results, pull request events and human steps and
    drops the rest (tool calls, status changes, intermediate agent output).

    A ``decision_recorded`` event is joined to its row in ``decisions``, so the line shows the
    floor and final tier, the rules that fired and how it was resolved, with one detail line per
    assessor.
    """
    decisions = {
        d["id"]: d
        for d in conn.execute("SELECT * FROM decisions WHERE ticket_id = ?", (ticket_id,))
    }
    rows = []
    for event in conn.execute(
        "SELECT * FROM events WHERE ticket_id = ? ORDER BY id", (ticket_id,)
    ).fetchall():
        if key_only and event["kind"] not in KEY_KINDS:
            continue
        payload = _payload(event)
        decision = decisions.get(payload.get("decision_id"))
        is_decision = event["kind"] == "decision_recorded" and decision is not None
        if is_decision:
            summary, detail = _decision_row(decision)
        else:
            summary, detail = _summarise(event["kind"], payload)
        rows.append(
            {
                "at": event["created_at"],
                "actor": event["actor"],
                "kind": event["kind"],
                "summary": summary,
                "detail": detail,
                "is_decision": is_decision,
            }
        )
    return rows


def _demote_headings(text: str) -> str:
    """Turn Markdown heading lines into bold lines so a question never renders as a page title."""
    lines = []
    for line in text.split("\n"):
        stripped = line.lstrip("#")
        if line.startswith("#") and stripped.startswith(" "):
            line = f"**{stripped.strip()}**"
        lines.append(line)
    return "\n".join(lines)


def inbox(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Open human requests, oldest first, with the ticket, the decision behind each and options.

    ``needs_live_tools`` is true when answering would continue into Build, Test or the merge.
    """
    past_design = {
        r["ticket_id"]
        for r in conn.execute(
            f"SELECT DISTINCT ticket_id FROM events WHERE kind IN ({', '.join('?' * 3)})",
            BUILD_KINDS,
        )
    }
    items = []
    for r in conn.execute(
        "SELECT h.*, t.title AS ticket_title, d.decision_type, d.rules_fired"
        " FROM human_requests h JOIN tickets t ON t.id = h.ticket_id"
        " LEFT JOIN decisions d ON d.id = h.decision_id"
        " WHERE h.status = 'open' ORDER BY h.id"
    ).fetchall():
        options = json.loads(r["options"] or "[]")
        items.append(
            {
                "id": r["id"],
                "ticket_id": r["ticket_id"],
                "ticket_title": r["ticket_title"],
                "question": _demote_headings(r["question"]),
                "options": options,
                "actions": [o for o in options if o in COMMANDS],
                "created_at": r["created_at"],
                "stage": r["decision_type"],
                "rules": json.loads(r["rules_fired"] or "[]"),
                "needs_live_tools": r["ticket_id"] in past_design
                or r["decision_type"] in LIVE_DECISIONS,
            }
        )
    return items


def design_request(conn: sqlite3.Connection, ticket_id: int) -> dict[str, Any] | None:
    """The ticket's open request if it can be answered in the UI (a Design-stage one), else None."""
    for item in inbox(conn):
        if item["ticket_id"] == ticket_id and not item["needs_live_tools"]:
            return item
    return None


def _chat_rows(conn: sqlite3.Connection, where: str, args: tuple[Any, ...]) -> list[sqlite3.Row]:
    try:
        return conn.execute(
            f"SELECT * FROM chat_messages WHERE {where} ORDER BY id", args
        ).fetchall()
    except sqlite3.OperationalError:  # a database recorded before chats existed
        return []


def chat_messages(conn: sqlite3.Connection, session: str) -> list[dict[str, Any]]:
    """One chat session's messages, oldest first; empty when the session or table is missing."""
    return [dict(row) for row in _chat_rows(conn, "session = ?", (session,))]


def chats(conn: sqlite3.Connection, ticket_id: int) -> list[dict[str, Any]]:
    """The ticket's chat sessions in the order they began, each with its stage and messages."""
    sessions: dict[str, dict[str, Any]] = {}
    for row in _chat_rows(conn, "ticket_id = ?", (ticket_id,)):
        entry = sessions.setdefault(
            row["session"], {"session": row["session"], "stage": row["stage"], "messages": []}
        )
        entry["messages"].append(dict(row))
    return list(sessions.values())
