"""Setup checks: what this machine still needs for the Design stage and for Build.

Every check is wrapped, so ``run_checks`` never raises: a check that blows up is a failed check
with the exception text as its detail. No detail ever holds the GitHub token.
"""

from __future__ import annotations

import json
import os
import shutil
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from sifu.config import Config, GithubConfig
from sifu.credentials import SecretNotFound, get_github_token
from sifu.implementer import git_output

NOT_CHECKED = "not checked"


@dataclass(frozen=True)
class Check:
    """The result of one setup check."""

    name: str
    ok: bool
    detail: str
    needed_for: Literal["design", "build"]


def _fetch(url: str) -> bytes:
    """GET a URL and return the body; gives up after three seconds."""
    with urllib.request.urlopen(url, timeout=3) as response:  # noqa: S310 - the configured Ollama
        return response.read()


def _guard(name: str, needed_for: Literal["design", "build"], fn: Callable[[], Check]) -> Check:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - a broken check is a failed check
        return Check(name, False, f"{type(exc).__name__}: {exc}", needed_for)


def run_checks(
    cfg: Config,
    *,
    fetch: Callable[[str], bytes] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    token: Callable[[GithubConfig], Any] = get_github_token,
    git: Callable[..., str] = git_output,
) -> list[Check]:
    """Run every setup check, in a fixed order; the first three are for Design, the rest Build."""
    fetch = fetch or _fetch
    repo = Path(cfg.workspace.product_repo_path).expanduser()
    state: dict[str, Any] = {}

    def ollama() -> Check:
        try:
            body = fetch(f"{cfg.ollama.base_url}/api/tags")
            state["tags"] = {m["name"] for m in json.loads(body).get("models", [])}
        except Exception:  # noqa: BLE001 - any failure means it is not reachable
            return Check("Ollama", False, f"not reachable at {cfg.ollama.base_url}", "design")
        return Check("Ollama", True, "reachable", "design")

    def models() -> Check:
        if "tags" not in state:
            return Check("Models", False, NOT_CHECKED, "design")
        wanted = {
            cfg.models.author,
            cfg.models.spec_reviewer,
            cfg.models.code_reviewer,
            *cfg.models.risk_assessors,
        }
        if cfg.implementer.backend == "ollama":
            wanted.add(cfg.implementer.ollama_model)
        missing = wanted - state["tags"]
        if missing:
            return Check("Models", False, "missing: " + ", ".join(sorted(missing)), "design")
        return Check("Models", True, f"{len(wanted)} models found", "design")

    def product_repo() -> Check:
        needed = [".git", "docs/glossary.yaml", "docs/PRD.md"]
        missing = [n for n in needed if not (repo / n).exists()]
        state["repo_ok"] = not missing
        if missing:
            return Check("Product repo", False, f"{repo}: missing " + ", ".join(missing), "design")
        return Check("Product repo", True, str(repo), "design")

    def claude() -> Check:
        if which("claude") is None:
            return Check("Claude Code", False, "no `claude` on PATH", "build")
        return Check("Claude Code", True, "found", "build")

    def github_token() -> Check:
        try:
            token(cfg.github)
        except SecretNotFound as exc:
            return Check("GitHub token", False, str(exc), "build")
        return Check("GitHub token", True, "found", "build")  # never the value

    def clean_clone() -> Check:
        if not state.get("repo_ok"):
            return Check("Clean clone", False, NOT_CHECKED, "build")
        if git(repo, "status", "--porcelain").strip():
            return Check("Clean clone", False, "uncommitted changes in the product repo", "build")
        return Check("Clean clone", True, "clean", "build")

    def verify_tools() -> Check:
        missing = [s.command[0] for s in cfg.verify.steps if which(s.command[0]) is None]
        if missing:
            return Check("Verify tools", False, "not on PATH: " + ", ".join(missing), "build")
        return Check("Verify tools", True, "found", "build")

    def worktrees() -> Check:
        path = Path(cfg.workspace.worktrees_dir).expanduser()
        probe = path
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        if not os.access(probe, os.W_OK):
            return Check("Worktrees folder", False, f"cannot write to {path}", "build")
        return Check("Worktrees folder", True, str(path), "build")

    plan: list[tuple[str, Literal["design", "build"], Callable[[], Check]]] = [
        ("Ollama", "design", ollama),
        ("Models", "design", models),
        ("Product repo", "design", product_repo),
        ("Claude Code", "build", claude),
        ("GitHub token", "build", github_token),
        ("Clean clone", "build", clean_clone),
        ("Verify tools", "build", verify_tools),
        ("Worktrees folder", "build", worktrees),
    ]
    return [_guard(name, needed, fn) for name, needed, fn in plan]
