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
from sifu.graph import Deps, run_ticket
from sifu.llm import LLM, OllamaLLM

ROOT = Path(__file__).resolve().parents[2]


def load_seed_tickets(path: Path = ROOT / "seed" / "tickets.yaml") -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)["tickets"]


def run_design_demo(
    keys: list[str] | None = None,
    cfg: Config | None = None,
    llm: LLM | None = None,
    db_path: str = ":memory:",
    show: bool = True,
) -> list[dict[str, Any]]:
    """Run seeded tickets through the Design stage and return one result row per ticket."""
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
    )
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
