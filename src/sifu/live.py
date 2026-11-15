"""The real Build wiring: implementer, verifier and GitHub client, shared by scripts and the app."""

from __future__ import annotations

from pathlib import Path

from sifu.config import Config, load_config
from sifu.credentials import get_github_token
from sifu.demo import open_demo
from sifu.github import GitHubClient
from sifu.graph import Deps
from sifu.implementer import Implementer
from sifu.llm import LLM
from sifu.pr_flow import publish_branch
from sifu.preflight import run_checks
from sifu.verify import Verifier

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "data" / "live.db"


def tag_for(db_path: str | Path) -> str | None:
    """Branch tag for a database: none for the default one, else the file's stem.

    Ticket ids restart in every database, so any other database tags its branches and worktrees.
    """
    if Path(db_path).resolve() == DEFAULT_DB.resolve():
        return None
    return Path(db_path).stem


def open_live(
    db_path: str | Path = DEFAULT_DB,
    *,
    cfg: Config | None = None,
    llm: LLM | None = None,
    merge_mode: str | None = None,
    planner: bool | None = None,
) -> tuple[Deps, int]:
    """Open the database and wire the real implementer, verifier and GitHub client.

    The caller's config is copied; ``merge_mode`` and ``planner`` override it only when given.
    """
    cfg = (cfg or load_config(ROOT / "config.yaml")).model_copy(deep=True)
    if merge_mode is not None:
        cfg.router.merge_mode = merge_mode
    if planner is not None:
        cfg.planner.enabled = planner
    deps, project_id = open_demo(cfg, llm, str(db_path))
    token = get_github_token(cfg.github)  # a SecretStr; never printed
    tag = tag_for(db_path)
    deps.implementer = Implementer(cfg, tag=tag)
    deps.verifier = Verifier(cfg, tag=tag)
    deps.github = GitHubClient.from_config(cfg.github)
    deps.publisher = lambda worktree, branch, message: publish_branch(
        cfg.github, worktree, branch, message, token
    )
    return deps, project_id


def missing_tools(cfg: Config | None = None) -> list[str]:
    """What Build and merge still need on this machine, as ``name: detail``; empty when ready."""
    cfg = cfg or load_config(ROOT / "config.yaml")
    return [
        f"{c.name}: {c.detail}" for c in run_checks(cfg) if c.needed_for == "build" and not c.ok
    ]
