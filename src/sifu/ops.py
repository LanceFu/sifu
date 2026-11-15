"""Every write the app can ask for, by database path.

``ui.py`` and ``jobs.py`` call these; each opens its own connection and closes it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from sifu import db, demo, lessons_pr, settings
from sifu import live as live_wiring
from sifu.config import Config, load_config
from sifu.evidence import DATA
from sifu.graph import Deps, continue_ticket, reopen_ticket, resume_interrupted, run_ticket
from sifu.implementer import git_output
from sifu.lessons_pr import Lesson
from sifu.llm import LLM

DATABASE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


def create_database(name: str, *, data_dir: Path | None = None, cfg: Config | None = None) -> Path:
    """Create ``<data_dir>/<name>.db`` with every table and the TripPlanner project.

    ``ValueError`` for a name that is not lowercase letters, digits and hyphens (at most 40);
    ``FileExistsError`` when the file exists. Nothing is created or overwritten on an error.
    """
    if not DATABASE_NAME.fullmatch(name):
        raise ValueError(
            "a database name is lowercase letters, digits and hyphens, 1 to 40 characters, "
            "starting with a letter or digit"
        )
    path = (data_dir or DATA) / f"{name}.db"
    if path.exists():
        raise FileExistsError(f"{path.name} already exists")
    cfg = cfg or load_config(live_wiring.ROOT / "config.yaml")
    conn = db.connect(path)
    try:
        db.init_db(conn)
        repo = str(Path(cfg.workspace.product_repo_path).expanduser())
        db.get_or_create_project(conn, "TripPlanner", repo, cfg.github.repo)
    finally:
        conn.close()
    return path


def safe_worktree(worktree: str | None, cfg: Config) -> Path | None:
    """The worktree as a resolved path, only when it is a folder strictly inside worktrees_dir."""
    if not worktree:
        return None
    root = Path(cfg.workspace.worktrees_dir).expanduser().resolve()
    path = Path(worktree).resolve()
    if path == root or not path.is_dir() or not path.is_relative_to(root):
        return None
    return path


def worktree_diff(worktree: Path, limit: int = 200_000) -> str:
    """The worktree's changes against HEAD and its untracked files, cut at ``limit`` characters."""
    text = git_output(worktree, "diff", "HEAD")
    untracked = git_output(worktree, "ls-files", "--others", "--exclude-standard").split()
    if untracked:
        text += "\nUntracked files:\n" + "\n".join(untracked) + "\n"
    if len(text) > limit:
        text = text[:limit] + f"\n(diff cut at {limit} characters)"
    return text


def _lessons(rows: list[dict[str, str]]) -> list[Lesson]:
    """Lesson objects from editor rows; blank rows are dropped, and an empty result is refused."""
    lessons = [Lesson(**row) for row in rows if str(row.get("lesson", "")).strip()]
    if not lessons:
        raise ValueError("no lessons to send")
    return lessons


def preview_lessons(
    db_path: str | Path, lessons: list[dict[str, str]], *, cfg: Config | None = None
) -> dict[str, Any]:
    """Dry run: the conventions.md diff and the router's decision; nothing is pushed."""
    return lessons_pr.run(
        effective_config(db_path, cfg), _lessons(lessons), push=False, project="TripPlanner"
    )


def send_lessons(
    db_path: str | Path, lessons: list[dict[str, str]], *, cfg: Config | None = None
) -> str:
    """Open the pull request that adds the lessons to conventions.md; return its URL."""
    out = lessons_pr.run(
        effective_config(db_path, cfg),
        _lessons(lessons),
        push=True,
        db_path=db_path,
        project="TripPlanner",
    )
    return str(out["pr_url"])


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
