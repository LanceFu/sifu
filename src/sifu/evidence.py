"""Build and Test evidence for the notebook, read from a recorded database.

The live runs against GitHub (``scripts/live_run.py`` and ``scripts/live_probe.py``) left their
audit trail in SQLite files under ``data/``. These functions turn that trail into Markdown tables:
the verify rounds, the router's decisions and the pull request events. The database is opened
read-only and nothing here calls a model or GitHub, so the notebook reruns without side effects.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from sifu.models import RiskTier

DATA = Path(__file__).resolve().parents[2] / "data"
PR_KINDS = ("pr_opened", "draft_pr_opened", "merge_blocked", "pr_merged")


def open_recorded(path: str | Path) -> sqlite3.Connection:
    """Open a recorded database read-only; raise FileNotFoundError when it does not exist."""
    file = Path(path)
    if not file.is_file():
        raise FileNotFoundError(f"{file} not found")
    conn = sqlite3.connect(f"{file.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _cell(text: Any) -> str:
    """Make a value safe inside one Markdown table cell."""
    return str(text).replace("|", "\\|").replace("\n", " ")


def _table(header: list[str], rows: list[list[str]], empty: str) -> str:
    if not rows:
        return f"_{empty}_"
    lines = ["| " + " | ".join(header) + " |", "|" + " --- |" * len(header)]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def _events(conn: sqlite3.Connection, kinds: tuple[str, ...]) -> list[sqlite3.Row]:
    marks = ", ".join("?" * len(kinds))
    return conn.execute(
        f"SELECT * FROM events WHERE kind IN ({marks}) ORDER BY id", kinds
    ).fetchall()


def _finding(f: dict[str, Any]) -> str:
    where = f.get("file") or ""
    if where and f.get("line") is not None:
        where += f":{f['line']}"
    rule = f"`{f['rule']}` " if f.get("rule") else ""
    return _cell(f"{f.get('source', '')} {rule}{where}: {f.get('message', '')}".strip())


def verify_table(conn: sqlite3.Connection) -> str:
    """One row per verify round: the ticket, the round, each step's result and the findings."""
    rounds: dict[int, int] = {}
    rows = []
    for event in _events(conn, ("verify_result",)):
        payload = json.loads(event["payload"])
        tid = event["ticket_id"]
        rounds[tid] = rounds.get(tid, 0) + 1
        steps = ", ".join(
            f"{s.get('name')} {'ok' if s.get('ok') else 'FAILED'}" for s in payload.get("steps", [])
        )
        findings = "<br>".join(_finding(f) for f in payload.get("findings", [])) or "none"
        result = "passed" if payload.get("ok") else "failed"
        rows.append([str(tid), str(rounds[tid]), result, _cell(steps or "no steps"), findings])
    return _table(
        ["Ticket", "Round", "Result", "Steps", "Findings sent back"], rows, "no verify rounds"
    )


def _tier(value: Any) -> str:
    return RiskTier(int(value)).name


def decisions_table(conn: sqlite3.Connection) -> str:
    """One row per router decision: floor and final tier, rules, assessors and the resolution."""
    rows = []
    for d in conn.execute("SELECT * FROM decisions ORDER BY id").fetchall():
        rules = ", ".join(f"`{r}`" for r in json.loads(d["rules_fired"])) or "none"
        assessors = "<br>".join(
            _cell(f"{a['assessor']}: {_tier(a['tier'])}. {a['rationale']}")
            for a in json.loads(d["assessments"])
        )
        rows.append(
            [
                str(d["ticket_id"]),
                d["decision_type"],
                _tier(d["floor_tier"]),
                _tier(d["final_tier"]),
                rules,
                assessors or "not asked",
                d["resolution"],
            ]
        )
    return _table(
        ["Ticket", "Decision", "Floor", "Final", "Rules fired", "Assessors", "Resolution"],
        rows,
        "no decisions",
    )


def pr_table(conn: sqlite3.Connection) -> str:
    """One row per pull request event, each linked to the PR on GitHub."""
    urls: dict[int, str] = {}
    last: dict[int, int] = {}  # ticket -> its PR number, for events that do not carry one
    rows = []
    for event in _events(conn, PR_KINDS):
        payload = json.loads(event["payload"])
        tid = event["ticket_id"]
        number = payload.get("number", last.get(tid))
        if number is not None:
            last[tid] = number
            if payload.get("url"):
                urls[number] = payload["url"]
        link = "–" if number is None else f"#{number}"
        if number in urls:
            link = f"[#{number}]({urls[number]})"
        sha = f"`{payload['sha'][:7]}`" if payload.get("sha") else ""
        note = _cell(payload.get("reason", ""))
        rows.append([str(tid), event["kind"], link, sha, note, event["created_at"]])
    return _table(
        ["Ticket", "Event", "PR", "Commit", "Note", "Recorded (UTC)"], rows, "no pull requests"
    )


def build_test_evidence(path: str | Path) -> str:
    """All three tables for one recorded database, or a clear note when the file is missing."""
    name = Path(path).name
    try:
        conn = open_recorded(path)
    except FileNotFoundError:
        return f"**`{name}` not found.** Restore it to `data/` to show this run."
    try:
        tickets = "\n".join(
            f"- Ticket {t['id']}: {t['title']} (status: {t['status']})"
            for t in conn.execute("SELECT * FROM tickets ORDER BY id").fetchall()
        )
        return (
            f"#### `{name}`\n\n{tickets}\n\n**Verify rounds**\n\n{verify_table(conn)}\n\n"
            f"**Decisions**\n\n{decisions_table(conn)}\n\n**Pull requests**\n\n{pr_table(conn)}"
        )
    finally:
        conn.close()
