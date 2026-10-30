"""SQLite persistence for projects, tickets, the event log, decisions and human requests.

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
"""

_RESOLUTION_BY_TIER = {
    RiskTier.AUTO: "auto",
    RiskTier.AUTO_WITH_VETO: "auto_with_veto",
    RiskTier.HUMAN: "pending_human",
}


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection with foreign keys on, creating the parent directory if needed."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
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
    with conn:
        cur = conn.execute(
            "INSERT INTO projects (name, repo_path, github_repo) VALUES (?, ?, ?)",
            (name, repo_path, github_repo),
        )
    return int(cur.lastrowid)


def get_or_create_project(
    conn: sqlite3.Connection, name: str, repo_path: str, github_repo: str | None = None
) -> int:
    row = conn.execute("SELECT id FROM projects WHERE name = ?", (name,)).fetchone()
    if row:
        return int(row["id"])
    return create_project(conn, name, repo_path, github_repo)


def create_ticket(conn: sqlite3.Connection, project_id: int, title: str, body: str = "") -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO tickets (project_id, title, body) VALUES (?, ?, ?)",
            (project_id, title, body),
        )
    ticket_id = int(cur.lastrowid)
    append_event(conn, ticket_id, "ticket_created", "user", {"title": title})
    return ticket_id


def get_ticket(conn: sqlite3.Connection, ticket_id: int) -> sqlite3.Row:
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
    with conn:
        cur = conn.execute(
            "INSERT INTO events (ticket_id, kind, actor, payload) VALUES (?, ?, ?, ?)",
            (ticket_id, kind, actor, json.dumps(payload or {})),
        )
    return int(cur.lastrowid)


def list_events(conn: sqlite3.Connection, ticket_id: int) -> list[sqlite3.Row]:
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
    with conn:
        cur = conn.execute(
            "INSERT INTO runs (ticket_id, agent, model, prompt_tokens, completion_tokens,"
            " latency_ms, ok) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ticket_id, agent, model, prompt_tokens, completion_tokens, latency_ms, int(ok)),
        )
    return int(cur.lastrowid)
