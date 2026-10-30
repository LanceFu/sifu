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
from sifu.config import Config, load_config
from sifu.context import ProductContext
from sifu.graph import Deps, is_paused, resume_ticket, run_ticket, sqlite_checkpointer
from sifu.llm import LLM, OllamaLLM

ROOT = Path(__file__).resolve().parents[2]


def load_seed_tickets(path: Path = ROOT / "seed" / "tickets.yaml") -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)["tickets"]


def open_demo(
    cfg: Config | None = None, llm: LLM | None = None, db_path: str = ":memory:"
) -> tuple[Deps, int]:
    """Build the dependencies and the TripPlanner project. A file path keeps pauses on disk."""
    cfg = cfg or load_config(ROOT / "config.yaml")
    llm = llm or OllamaLLM(cfg.ollama.base_url, cfg.ollama.num_ctx)
    repo = Path(cfg.workspace.product_repo_path).expanduser()
    conn = db.connect(db_path)
    db.init_db(conn)
    project_id = db.get_or_create_project(conn, "TripPlanner", str(repo), cfg.github.repo)
    deps = Deps(
        conn=conn,
        llm=llm,
        cfg=cfg,
        ctx=ProductContext.load(repo),
        artifacts_dir=ROOT / cfg.workspace.artifacts_dir,
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
        "SELECT ticket_id FROM human_requests WHERE id = ?", (request_id,)
    ).fetchone()
    state = resume_ticket(deps, request["ticket_id"], answer)
    status = db.get_ticket(deps.conn, request["ticket_id"])["status"]
    print(
        f"ticket {request['ticket_id']}: {'paused again' if is_paused(state) else 'resumed'}, "
        f"status={status}"
    )
    show_decisions(deps)
    return state


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
    parser = argparse.ArgumentParser(description="Run the Design stage on seeded tickets.")
    parser.add_argument("--ticket", action="append", help="seed key; repeat for several")
    parser.add_argument("--db", default=":memory:")
    args = parser.parse_args()
    run_design_demo(args.ticket, db_path=args.db)


if __name__ == "__main__":
    main()
