"""SQLite persistence for projects, tickets, the event log, decisions, requests, chats and jobs.

LangGraph keeps its own checkpoints (keyed by ticket id as thread id). These tables are the
application's audit trail and are what the UI and the evaluation harness read.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sifu.models import (
    DecisionResult,
    DecisionType,
    RiskAssessment,
    RiskTier,
    TicketStatus,
)
from sifu.state import assert_transition

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    repo_path TEXT NOT NULL,
    github_repo TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'intent',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    kind TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    decision_type TEXT NOT NULL,
    floor_tier INTEGER NOT NULL,
    final_tier INTEGER NOT NULL,
    rules_fired TEXT NOT NULL DEFAULT '[]',
    assessments TEXT NOT NULL DEFAULT '[]',
    resolution TEXT NOT NULL,
    decided_by TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS human_requests (
    id INTEGER PRIMARY KEY,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    decision_id INTEGER REFERENCES decisions(id),
    question TEXT NOT NULL,
    options TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'open',
    answer TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    answered_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    ticket_id INTEGER REFERENCES tickets(id),
    agent TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    latency_ms INTEGER,
    ok INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY,
    session TEXT NOT NULL,                    -- one brainstorm conversation
    ticket_id INTEGER REFERENCES tickets(id), -- NULL until the intent chat becomes a ticket
    stage TEXT NOT NULL,                      -- intent | spec
    role TEXT NOT NULL,                       -- user | assistant
    content TEXT NOT NULL,
    title TEXT,                               -- the draft after an assistant message
    draft TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS chat_messages_session ON chat_messages (session, id);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    ticket_id INTEGER REFERENCES tickets(id),
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued',
    result TEXT,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_active_per_ticket
    ON jobs (ticket_id) WHERE status IN ('queued', 'running');
"""

_RESOLUTION_BY_TIER = {
    RiskTier.AUTO: "auto",
    RiskTier.AUTO_WITH_VETO: "auto_with_veto",
    RiskTier.HUMAN: "pending_human",
}


class JobConflict(ValueError):
    """A ticket already has a job queued or running."""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection with foreign keys on, creating the parent directory if needed."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables if they do not exist."""
    with conn:
        conn.executescript(SCHEMA)


def create_project(
    conn: sqlite3.Connection, name: str, repo_path: str, github_repo: str | None = None
) -> int:
    """Insert a project and return its id."""
    with conn:
        cur = conn.execute(
            "INSERT INTO projects (name, repo_path, github_repo) VALUES (?, ?, ?)",
            (name, repo_path, github_repo),
        )
    return int(cur.lastrowid)


def get_or_create_project(
    conn: sqlite3.Connection, name: str, repo_path: str, github_repo: str | None = None
) -> int:
    """Return the id of the project with this name, creating it if needed."""
    row = conn.execute("SELECT id FROM projects WHERE name = ?", (name,)).fetchone()
    if row:
        return int(row["id"])
    return create_project(conn, name, repo_path, github_repo)


def create_ticket(conn: sqlite3.Connection, project_id: int, title: str, body: str = "") -> int:
    """Insert a ticket, log its ticket_created event and return its id."""
    with conn:
        cur = conn.execute(
            "INSERT INTO tickets (project_id, title, body) VALUES (?, ?, ?)",
            (project_id, title, body),
        )
    ticket_id = int(cur.lastrowid)
    append_event(conn, ticket_id, "ticket_created", "user", {"title": title})
    return ticket_id


def append_to_ticket_body(conn: sqlite3.Connection, ticket_id: int, text: str) -> None:
    """Add a clarification to the end of a ticket's body (a human's answer to a question)."""
    with conn:
        conn.execute(
            "UPDATE tickets SET body = body || ?, updated_at = datetime('now') WHERE id = ?",
            (f"\n\n{text}", ticket_id),
        )
    append_event(conn, ticket_id, "ticket_clarified", "human", {"text": text[:500]})


def get_ticket(conn: sqlite3.Connection, ticket_id: int) -> sqlite3.Row:
    """Return one ticket row; raise KeyError when the id is unknown."""
    row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if row is None:
        raise KeyError(f"ticket {ticket_id} not found")
    return row


def append_event(
    conn: sqlite3.Connection,
    ticket_id: int,
    kind: str,
    actor: str,
    payload: Mapping[str, Any] | None = None,
) -> int:
    """Append one row to the events log and return its id."""
    with conn:
        cur = conn.execute(
            "INSERT INTO events (ticket_id, kind, actor, payload) VALUES (?, ?, ?, ?)",
            (ticket_id, kind, actor, json.dumps(payload or {})),
        )
    return int(cur.lastrowid)


def list_events(conn: sqlite3.Connection, ticket_id: int) -> list[sqlite3.Row]:
    """All events for a ticket, oldest first."""
    return conn.execute(
        "SELECT * FROM events WHERE ticket_id = ? ORDER BY id", (ticket_id,)
    ).fetchall()


def set_status(
    conn: sqlite3.Connection,
    ticket_id: int,
    new_status: TicketStatus,
    facts: Mapping[str, bool] | None = None,
    actor: str = "supervisor",
) -> None:
    """Move a ticket to a new status if the state machine and its gate allow it."""
    current = TicketStatus(get_ticket(conn, ticket_id)["status"])
    assert_transition(current, new_status, facts)
    with conn:
        conn.execute(
            "UPDATE tickets SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (new_status.value, ticket_id),
        )
    append_event(
        conn, ticket_id, "status_changed", actor, {"from": current.value, "to": new_status.value}
    )


def record_decision(
    conn: sqlite3.Connection,
    ticket_id: int,
    decision_type: DecisionType,
    result: DecisionResult,
    assessments: list[RiskAssessment],
) -> int:
    """Store a router decision with the rules that fired and every assessor's opinion."""
    with conn:
        cur = conn.execute(
            "INSERT INTO decisions (ticket_id, decision_type, floor_tier, final_tier, rules_fired,"
            " assessments, resolution) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ticket_id,
                decision_type.value,
                int(result.floor_tier),
                int(result.final_tier),
                json.dumps(result.rules_fired),
                json.dumps([a.model_dump(mode="json") for a in assessments]),
                _RESOLUTION_BY_TIER[result.final_tier],
            ),
        )
    decision_id = int(cur.lastrowid)
    append_event(
        conn,
        ticket_id,
        "decision_recorded",
        "router",
        {"decision_id": decision_id, "tier": int(result.final_tier), "rules": result.rules_fired},
    )
    return decision_id


def resolve_decision(
    conn: sqlite3.Connection, decision_id: int, resolution: str, decided_by: str
) -> None:
    """Record the final outcome, for example human_approved, human_rejected or vetoed."""
    with conn:
        conn.execute(
            "UPDATE decisions SET resolution = ?, decided_by = ?, resolved_at = datetime('now')"
            " WHERE id = ?",
            (resolution, decided_by, decision_id),
        )


def open_human_request(
    conn: sqlite3.Connection,
    ticket_id: int,
    question: str,
    options: list[str] | None = None,
    decision_id: int | None = None,
) -> int:
    """Open a request for a human answer, log it and return its id."""
    with conn:
        cur = conn.execute(
            "INSERT INTO human_requests (ticket_id, decision_id, question, options)"
            " VALUES (?, ?, ?, ?)",
            (ticket_id, decision_id, question, json.dumps(options or [])),
        )
    request_id = int(cur.lastrowid)
    append_event(conn, ticket_id, "human_request_opened", "supervisor", {"request_id": request_id})
    return request_id


def answer_human_request(conn: sqlite3.Connection, request_id: int, answer: str) -> None:
    """Record a human's answer; raise if the request is unknown or already answered."""
    row = conn.execute("SELECT * FROM human_requests WHERE id = ?", (request_id,)).fetchone()
    if row is None:
        raise KeyError(f"human request {request_id} not found")
    if row["status"] != "open":
        raise ValueError(f"human request {request_id} is already {row['status']}")
    with conn:
        conn.execute(
            "UPDATE human_requests SET status = 'answered', answer = ?,"
            " answered_at = datetime('now') WHERE id = ?",
            (answer, request_id),
        )
    append_event(
        conn, row["ticket_id"], "human_request_answered", "human", {"request_id": request_id}
    )


def open_requests(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every human request still waiting for an answer, oldest first."""
    return conn.execute("SELECT * FROM human_requests WHERE status = 'open' ORDER BY id").fetchall()


def record_run(
    conn: sqlite3.Connection,
    agent: str,
    model: str,
    ticket_id: int | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    latency_ms: int | None = None,
    ok: bool = True,
) -> int:
    """Record one model call (agent, model, tokens, latency) and return its id."""
    with conn:
        cur = conn.execute(
            "INSERT INTO runs (ticket_id, agent, model, prompt_tokens, completion_tokens,"
            " latency_ms, ok) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ticket_id, agent, model, prompt_tokens, completion_tokens, latency_ms, int(ok)),
        )
    return int(cur.lastrowid)


def tokens_used(conn: sqlite3.Connection, ticket_id: int) -> int:
    """Tokens the ticket's model calls have used since it was last reopened (or ever)."""
    event = conn.execute(
        "SELECT payload FROM events WHERE ticket_id = ? AND kind = 'ticket_reopened'"
        " ORDER BY id DESC LIMIT 1",
        (ticket_id,),
    ).fetchone()
    baseline = int(json.loads(event["payload"]).get("runs_before", 0)) if event else 0
    return int(
        conn.execute(
            "SELECT COALESCE(SUM(COALESCE(prompt_tokens, 0) + COALESCE(completion_tokens, 0)), 0)"
            " FROM runs WHERE ticket_id = ? AND id > ?",
            (ticket_id, baseline),
        ).fetchone()[0]
    )


def add_chat_message(
    conn: sqlite3.Connection,
    session: str,
    stage: str,
    role: str,
    content: str,
    *,
    ticket_id: int | None = None,
    title: str | None = None,
    draft: str | None = None,
) -> int:
    """Append one brainstorm message to a chat session and return its id.

    An assistant message also stores the draft (title and text) as it stood after that reply.
    """
    with conn:
        cur = conn.execute(
            "INSERT INTO chat_messages (session, ticket_id, stage, role, content, title, draft)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session, ticket_id, stage, role, content, title, draft),
        )
    return int(cur.lastrowid)


def list_chat(conn: sqlite3.Connection, session: str) -> list[sqlite3.Row]:
    """Every message of one chat session, oldest first."""
    return conn.execute(
        "SELECT * FROM chat_messages WHERE session = ? ORDER BY id", (session,)
    ).fetchall()


def attach_chat(conn: sqlite3.Connection, session: str, ticket_id: int) -> int:
    """Link a chat session to the ticket it produced; return how many messages it holds."""
    with conn:
        cur = conn.execute(
            "UPDATE chat_messages SET ticket_id = ? WHERE session = ?", (ticket_id, session)
        )
    return int(cur.rowcount)


def create_job(
    conn: sqlite3.Connection,
    kind: str,
    payload: Mapping[str, Any],
    ticket_id: int | None = None,
) -> int:
    """Queue a job; raise JobConflict if the ticket already has one queued or running."""
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO jobs (ticket_id, kind, payload) VALUES (?, ?, ?)",
                (ticket_id, kind, json.dumps(payload)),
            )
    except sqlite3.IntegrityError as exc:
        raise JobConflict(f"ticket {ticket_id} already has a job queued or running") from exc
    return int(cur.lastrowid)


def start_job(conn: sqlite3.Connection, job_id: int) -> bool:
    """Mark a queued job running; False when it is no longer queued (for example cancelled)."""
    with conn:
        cur = conn.execute(
            "UPDATE jobs SET status = 'running', started_at = datetime('now')"
            " WHERE id = ? AND status = 'queued'",
            (job_id,),
        )
    return cur.rowcount == 1


def finish_job(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    result: str | None = None,
    error: str | None = None,
) -> None:
    """Record how a job ended: failed when there is an error, otherwise done."""
    status = "failed" if error is not None else "done"
    with conn:
        conn.execute(
            "UPDATE jobs SET status = ?, result = ?, error = ?, finished_at = datetime('now')"
            " WHERE id = ?",
            (status, result, error, job_id),
        )


def cancel_job(conn: sqlite3.Connection, job_id: int) -> bool:
    """Cancel a job that has not started; False when it already started or ended."""
    with conn:
        cur = conn.execute(
            "UPDATE jobs SET status = 'cancelled', finished_at = datetime('now')"
            " WHERE id = ? AND status = 'queued'",
            (job_id,),
        )
    return cur.rowcount == 1


def interrupt_stale_jobs(conn: sqlite3.Connection) -> int:
    """Mark every queued or running job interrupted (its process is gone); return the count."""
    with conn:
        cur = conn.execute(
            "UPDATE jobs SET status = 'interrupted', finished_at = datetime('now')"
            " WHERE status IN ('queued', 'running')"
        )
    return int(cur.rowcount)


def list_jobs(conn: sqlite3.Connection, active_only: bool = False) -> list[sqlite3.Row]:
    """Jobs newest first; with active_only, only those queued or running."""
    where = " WHERE status IN ('queued', 'running')" if active_only else ""
    return conn.execute(f"SELECT * FROM jobs{where} ORDER BY id DESC").fetchall()
