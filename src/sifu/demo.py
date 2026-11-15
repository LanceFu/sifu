"""One-call demo of the Design stage on the seeded tickets.

From a notebook cell: ``from sifu.demo import run_design_demo; run_design_demo()``.
From a shell: ``uv run python -m sifu.demo --ticket clean``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from sifu import db
from sifu.brainstorm import transcript_md
from sifu.config import Config, load_config
from sifu.context import ProductContext
from sifu.graph import Deps, is_paused, resume_ticket, run_ticket, sqlite_checkpointer
from sifu.llm import LLM, OllamaLLM
from sifu.synthesizer import ticket_code, write_artifact

ROOT = Path(__file__).resolve().parents[2]


def load_seed_tickets(path: Path = ROOT / "seed" / "tickets.yaml") -> list[dict[str, Any]]:
    """Load the seeded demo tickets from seed/tickets.yaml."""
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)["tickets"]


def _db_name(db_path: str) -> str:
    """Artifacts go in a folder per database, because ticket ids restart in every database."""
    return "memory" if db_path == ":memory:" else Path(db_path).stem


def artifacts_dir(db_path: str | Path, cfg: Config | None = None) -> Path:
    """The folder that holds a database's ticket artifacts (intent.md, spec.md, chat logs)."""
    cfg = cfg or load_config(ROOT / "config.yaml")
    return ROOT / cfg.workspace.artifacts_dir / _db_name(str(db_path))


def open_demo(
    cfg: Config | None = None, llm: LLM | None = None, db_path: str = ":memory:"
) -> tuple[Deps, int]:
    """Build the dependencies and the TripPlanner project. A file path keeps pauses on disk."""
    cfg = cfg or load_config(ROOT / "config.yaml")
    llm = llm or OllamaLLM.from_config(cfg.ollama)
    repo = Path(cfg.workspace.product_repo_path).expanduser()
    conn = db.connect(db_path)
    db.init_db(conn)
    project_id = db.get_or_create_project(conn, "TripPlanner", str(repo), cfg.github.repo)
    deps = Deps(
        conn=conn,
        llm=llm,
        cfg=cfg,
        ctx=ProductContext.load(repo),
        artifacts_dir=artifacts_dir(db_path, cfg),
        checkpointer=sqlite_checkpointer(db_path),
    )
    return deps, project_id


def run_design_demo(
    keys: list[str] | None = None,
    cfg: Config | None = None,
    llm: LLM | None = None,
    db_path: str = ":memory:",
    show: bool = True,
) -> list[dict[str, Any]]:
    """Run seeded tickets through the Design stage and return one result row per ticket."""
    deps, project_id = open_demo(cfg, llm, db_path)
    conn = deps.conn
    rows = []
    for seed in load_seed_tickets():
        if keys and seed["key"] not in keys:
            continue
        ticket_id = db.create_ticket(conn, project_id, seed["title"], seed["body"])
        state = run_ticket(deps, ticket_id)
        events = db.list_events(conn, ticket_id)
        rows.append(
            {
                "key": seed["key"],
                "ticket_id": ticket_id,
                "expected": seed["expect"],
                "outcome": state.get("outcome"),
                "status": db.get_ticket(conn, ticket_id)["status"],
                "events": len(events),
                "artifact": state.get("spec_path") or state.get("questions_path"),
            }
        )
        if show:
            _print_row(rows[-1], events)
    return rows


def run_router_demo(
    deps: Deps, project_id: int, keys: tuple[str, ...] = ("clean", "protected_path")
) -> dict[str, int]:
    """Run a low-risk ticket (auto-approved) and a protected-file ticket (paused for a human)."""
    seeds = {s["key"]: s for s in load_seed_tickets()}
    ids: dict[str, int] = {}
    for key in keys:
        ids[key] = db.create_ticket(deps.conn, project_id, seeds[key]["title"], seeds[key]["body"])
        state = run_ticket(deps, ids[key])
        verdict = "PAUSED for a human" if is_paused(state) else "continued"
        print(f"ticket {ids[key]} ({key}): {verdict}")
    show_decisions(deps)
    return ids


def show_decisions(deps: Deps) -> None:
    """Print every router decision with the rules that fired, and any open human requests."""
    for row in deps.conn.execute("SELECT * FROM decisions ORDER BY id"):
        print(
            f"decision {row['id']} ticket {row['ticket_id']} {row['decision_type']}: "
            f"floor={row['floor_tier']} final={row['final_tier']} rules={row['rules_fired']} "
            f"-> {row['resolution']}"
        )
    for request in db.open_requests(deps.conn):
        print(f"OPEN request {request['id']} (ticket {request['ticket_id']}): {request['options']}")


def answer_request(deps: Deps, request_id: int, answer: str) -> dict[str, Any]:
    """Answer an open request and resume its ticket from the saved checkpoint."""
    request = deps.conn.execute(
        "SELECT ticket_id, status FROM human_requests WHERE id = ?", (request_id,)
    ).fetchone()
    if request is None or request["status"] != "open":
        raise ValueError(f"request {request_id} is not open; see show_decisions(deps)")
    state = resume_ticket(deps, request["ticket_id"], answer)
    status = db.get_ticket(deps.conn, request["ticket_id"])["status"]
    print(
        f"ticket {request['ticket_id']}: {'paused again' if is_paused(state) else 'resumed'}, "
        f"status={status}"
    )
    show_decisions(deps)
    return state


def answer_in_database(
    db_path: str | Path,
    request_id: int,
    answer: str,
    cfg: Config | None = None,
    llm: LLM | None = None,
) -> str:
    """Answer an open request in a database file and return the ticket's new status.

    This is what the UI's inbox calls. It has no implementer, verifier or GitHub client, so the
    planner is switched off for the call: an approved spec stops at the plan gate instead of
    running the stub Build. Requests past the Design stage go through ``scripts/live_run.py``.
    """
    cfg = (cfg or load_config(ROOT / "config.yaml")).model_copy(deep=True)
    cfg.planner.enabled = False
    deps, _ = open_demo(cfg, llm, str(db_path))
    try:
        ticket_id = deps.conn.execute(
            "SELECT ticket_id FROM human_requests WHERE id = ?", (request_id,)
        ).fetchone()
        answer_request(deps, request_id, answer)
        return str(db.get_ticket(deps.conn, ticket_id["ticket_id"])["status"])
    finally:
        deps.conn.close()


def archive_chat(
    conn: Any, artifacts: Path, ticket_id: int, session: str, stage: str, name: str | None = None
) -> Path | None:
    """Link a chat to its ticket, write it beside the ticket's artifacts and log ``chat_saved``.

    Returns the file's path, or None when the session has no messages.
    """
    count = db.attach_chat(conn, session, ticket_id)
    if not count:
        return None
    heading = f"Chat ({stage}): {ticket_code(ticket_id)}"
    path = write_artifact(
        artifacts,
        ticket_id,
        name or f"chat-{stage}.md",
        transcript_md(db.list_chat(conn, session), heading),
    )
    db.append_event(
        conn,
        ticket_id,
        "chat_saved",
        "human",
        {"session": session, "stage": stage, "messages": count, "path": str(path)},
    )
    return path


def archive_chat_in_database(
    db_path: str | Path, ticket_id: int, session: str, stage: str, name: str | None = None
) -> Path | None:
    """``archive_chat`` on a database file; what the UI calls before it sends a spec answer."""
    conn = db.connect(db_path)
    try:
        db.init_db(conn)
        return archive_chat(conn, artifacts_dir(db_path), ticket_id, session, stage, name)
    finally:
        conn.close()


def start_in_database(
    db_path: str | Path,
    title: str,
    body: str,
    session: str | None = None,
    cfg: Config | None = None,
    llm: LLM | None = None,
) -> tuple[int, str]:
    """Create a ticket from a reviewed draft, run its Design stage and return (id, status).

    This is the Intent tab's "Start the workflow". The Design stage writes ``intent.md`` from
    the title and body, then a spec or questions. ``session`` is the brainstorm chat that led to
    the draft: it is linked to the ticket and written to ``chat-intent.md``. As with
    ``answer_in_database`` the planner is off, so an accepted spec waits at the plan gate.
    """
    if not title.strip() or not body.strip():
        raise ValueError("a ticket needs a title and a description")
    cfg = (cfg or load_config(ROOT / "config.yaml")).model_copy(deep=True)
    cfg.planner.enabled = False
    deps, project_id = open_demo(cfg, llm, str(db_path))
    try:
        ticket_id = db.create_ticket(deps.conn, project_id, title.strip(), body.strip())
        if session:
            archive_chat(deps.conn, deps.artifacts_dir, ticket_id, session, "intent")
        run_ticket(deps, ticket_id)
        return ticket_id, str(db.get_ticket(deps.conn, ticket_id)["status"])
    finally:
        deps.conn.close()


def _print_row(row: dict[str, Any], events: list[Any]) -> None:
    mark = "ok " if row["outcome"] == row["expected"] else "!! "
    print(
        f"{mark}{row['key']:<13} expected={row['expected']:<9} got={row['outcome']:<9} "
        f"status={row['status']:<12} events={row['events']}"
    )
    if row["artifact"]:
        print(f"   {row['artifact']}")
    for event in events:
        if event["kind"] == "agent_output" and event["actor"].startswith("reviewer"):
            report = json.loads(event["payload"])["report"]
            print(
                f"   {event['actor']:<20} {len(report['findings'])} findings"
                f"{' (degraded)' if report['degraded'] else ''}"
            )


def main() -> None:
    """Parse the command line and run the script."""
    parser = argparse.ArgumentParser(description="Run the Design stage on seeded tickets.")
    parser.add_argument("--ticket", action="append", help="seed key; repeat for several")
    parser.add_argument("--db", default=":memory:")
    args = parser.parse_args()
    run_design_demo(args.ticket, db_path=args.db)


if __name__ == "__main__":
    main()
