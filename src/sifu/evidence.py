"""Build and Test evidence for the notebook, read from a recorded database.

The live runs against GitHub (``scripts/live_run.py`` and ``scripts/live_probe.py``) left their
audit trail in SQLite files under ``data/``. These functions turn that trail into Markdown tables:
the verify rounds, the router's decisions and the pull request events. The database is opened
read-only and nothing here calls a model or GitHub, so the notebook reruns without side effects.

One run's database (``data/live.db``, PR #2) was lost. For that run ``github_snapshot`` renders
what GitHub itself recorded, from a JSON file saved once under ``docs/recorded/``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from sifu.models import RiskTier

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
RECORDED = ROOT / "docs" / "recorded"  # GitHub snapshots, kept in git
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
    """One row per router decision, then each assessor's rationale as a list under the table.

    The rationales are full sentences, so they sit below the table instead of in a column:
    inside it they squeeze the other columns off a printed page.
    """
    rows, notes = [], []
    for d in conn.execute("SELECT * FROM decisions ORDER BY id").fetchall():
        rules = ", ".join(f"`{r}`" for r in json.loads(d["rules_fired"])) or "none"
        rows.append(
            [
                str(d["id"]),
                str(d["ticket_id"]),
                d["decision_type"],
                _tier(d["floor_tier"]),
                _tier(d["final_tier"]),
                rules,
                d["resolution"],
            ]
        )
        notes += [
            f"- Decision {d['id']}, {a['assessor']}: {_tier(a['tier'])}. "
            + str(a["rationale"]).replace("\n", " ")
            for a in json.loads(d["assessments"])
        ]
    table = _table(
        ["Decision", "Ticket", "Type", "Floor", "Final", "Rules fired", "Resolution"],
        rows,
        "no decisions",
    )
    return table + ("\n\n" + "\n".join(notes) if notes else "")


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


def github_snapshot(path: str | Path) -> str:
    """Render a saved GitHub record of one pull request: the PR, its check, files and comments.

    The file was read from GitHub once and saved; nothing here calls GitHub. Sifu wrote the
    comments itself from its decision record, so they carry the tier, rules and rationales.
    """
    pr = json.loads(Path(path).read_text(encoding="utf-8"))
    facts = [
        ["Pull request", f"[#{pr['number']}]({pr['url']}) {_cell(pr['title'])}"],
        ["Branch and commit", f"`{pr['head_branch']}` at `{pr['head_sha'][:7]}`"],
        ["Opened (UTC)", pr["created_at"]],
        ["State", pr["state"]],
    ]
    if pr.get("merged_at"):
        merge = f"{pr['merged_at']} by {pr['merged_by']}, commit `{pr['merge_commit'][:7]}`"
        facts.append(["Merged (UTC)", merge])
    for commit in pr.get("commits", []):
        facts.append(["Commit author", f"{commit['author']}: {_cell(commit['message'])}"])
    for check in pr.get("checks", []):
        result = f"{check['conclusion']} at {check['completed_at']}"
        facts.append([f"Required check `{check['name']}`", f"[{result}]({check['url']})"])
    files = [[f"`{f['path']}`", f"+{f['additions']} -{f['deletions']}"] for f in pr["files"]]
    comments = "\n\n".join(
        f"[Comment]({c['url']}) by {c['author']} at {c['created_at']}:\n\n"
        + "\n".join(f"> {line}" for line in c["body"].splitlines())
        for c in pr.get("comments", [])
    )
    return (
        f"{_table(['Field', 'Recorded on GitHub'], facts, 'no pull request')}\n\n"
        f"**Files changed**\n\n{_table(['File', 'Lines'], files, 'no files')}\n\n"
        f"**Comments posted on the pull request**\n\n{comments or '_none_'}"
    )


def build_test_evidence(path: str | Path, snapshot: str | Path | None = None) -> str:
    """All three tables for one recorded database, or a clear note when the file is missing.

    When the database is missing and ``snapshot`` names a saved GitHub record of the run's pull
    request, that record is shown in its place, with a note on what it cannot show.
    """
    name = Path(path).name
    try:
        conn = open_recorded(path)
    except FileNotFoundError:
        if snapshot is not None and Path(snapshot).is_file():
            captured = json.loads(Path(snapshot).read_text(encoding="utf-8"))["captured_at"]
            return (
                f"#### `{name}` (database lost; GitHub's record of the run)\n\n"
                f"`{name}` is no longer in `data/`, so the verify rounds and the decision rows "
                "of this run cannot be shown. What follows is GitHub's side of the same trail, "
                f"read once on {captured} and saved in `docs/recorded/{Path(snapshot).name}`. "
                "The comment is the one Sifu posted from its merge decision.\n\n"
                f"{github_snapshot(snapshot)}"
            )
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
