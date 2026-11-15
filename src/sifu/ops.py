"""Every write the app can ask for, by database path.

``ui.py`` and ``jobs.py`` call these; each opens its own connection and closes it.
"""

from __future__ import annotations

from pathlib import Path

from sifu import db, demo, settings
from sifu import live as live_wiring
from sifu.config import Config, load_config
from sifu.graph import Deps, continue_ticket, reopen_ticket, resume_interrupted, run_ticket
from sifu.llm import LLM


def effective_config(db_path: str | Path, cfg: Config | None = None) -> Config:
    """The config with this database's saved run options applied (a copy)."""
    cfg = cfg or load_config(live_wiring.ROOT / "config.yaml")
    return settings.apply(cfg, settings.load(db_path))


def open_deps(
    db_path: str | Path, *, live: bool, cfg: Config | None = None, llm: LLM | None = None
) -> Deps:
    """Open the database with the real Build wiring (live) or the design-only wiring.

    Design mode copies the config and switches the planner off, so an accepted spec stops at the
    plan gate instead of running the stub Build.
    """
    cfg = effective_config(db_path, cfg)
    if live:
        return live_wiring.open_live(db_path, cfg=cfg, llm=llm)[0]
    cfg.planner.enabled = False
    return demo.open_demo(cfg, llm, str(db_path))[0]


def _status(deps: Deps, ticket_id: int) -> str:
    return str(db.get_ticket(deps.conn, ticket_id)["status"])


def create_ticket(
    db_path: str | Path,
    title: str,
    body: str,
    session: str | None = None,
    *,
    cfg: Config | None = None,
) -> int:
    """File a reviewed draft as a ticket and link its brainstorm chat; run nothing."""
    if not title.strip() or not body.strip():
        raise ValueError("a ticket needs a title and a description")
    cfg = cfg or load_config(demo.ROOT / "config.yaml")
    conn = db.connect(db_path)
    try:
        db.init_db(conn)
        repo = str(Path(cfg.workspace.product_repo_path).expanduser())
        project = db.get_or_create_project(conn, "TripPlanner", repo, cfg.github.repo)
        ticket_id = db.create_ticket(conn, project, title.strip(), body.strip())
        if session:
            demo.archive_chat(conn, demo.artifacts_dir(db_path, cfg), ticket_id, session, "intent")
        return ticket_id
    finally:
        conn.close()


def run(
    db_path: str | Path,
    ticket_id: int,
    *,
    live: bool,
    cfg: Config | None = None,
    llm: LLM | None = None,
) -> str:
    """Run a ticket from the start until it finishes or stops; return its status."""
    deps = open_deps(db_path, live=live, cfg=cfg, llm=llm)
    try:
        # without the planner there is no plan to accept, so the gate is opened up front
        facts = {"plan_accepted": True} if live and not deps.cfg.planner.enabled else {}
        run_ticket(deps, ticket_id, facts=facts)
        return _status(deps, ticket_id)
    finally:
        deps.conn.close()


def reopen(
    db_path: str | Path,
    ticket_id: int,
    *,
    live: bool,
    cfg: Config | None = None,
    llm: LLM | None = None,
) -> str:
    """Run a failed ticket again from the start; return its status."""
    deps = open_deps(db_path, live=live, cfg=cfg, llm=llm)
    try:
        facts = {"plan_accepted": True} if live and not deps.cfg.planner.enabled else {}
        reopen_ticket(deps, ticket_id, facts)
        return _status(deps, ticket_id)
    finally:
        deps.conn.close()


def resume(
    db_path: str | Path,
    ticket_id: int,
    *,
    live: bool,
    cfg: Config | None = None,
    llm: LLM | None = None,
) -> str:
    """Continue a run that was cut off from its last checkpoint; return its status."""
    deps = open_deps(db_path, live=live, cfg=cfg, llm=llm)
    try:
        resume_interrupted(deps, ticket_id)
        return _status(deps, ticket_id)
    finally:
        deps.conn.close()


def answer(
    db_path: str | Path,
    request_id: int,
    answer: str,
    *,
    live: bool,
    cfg: Config | None = None,
    llm: LLM | None = None,
) -> str:
    """Answer an open request, resume its ticket and return the ticket's status."""
    deps = open_deps(db_path, live=live, cfg=cfg, llm=llm)
    try:
        row = deps.conn.execute(
            "SELECT ticket_id FROM human_requests WHERE id = ?", (request_id,)
        ).fetchone()
        demo.answer_request(deps, request_id, answer)
        return _status(deps, row["ticket_id"])
    finally:
        deps.conn.close()


def continue_to_build(
    db_path: str | Path,
    ticket_id: int,
    *,
    cfg: Config | None = None,
    llm: LLM | None = None,
) -> str:
    """Continue a ticket waiting at the plan gate into Build; return its status."""
    deps = open_deps(db_path, live=True, cfg=cfg, llm=llm)
    try:
        continue_ticket(deps, ticket_id)
        return _status(deps, ticket_id)
    finally:
        deps.conn.close()
