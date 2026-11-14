"""Propose lessons for the product's conventions.md as a PR that the router sends to a human.

    uv run python scripts/lessons_pr.py --lessons lessons/2026-11-14.yaml          # dry run
    uv run python scripts/lessons_pr.py --lessons lessons/2026-11-14.yaml --push   # open the PR

The lessons file is a YAML list of ``{lesson, evidence}``; quote any evidence that contains ``#``
(``"PR #3"``), since YAML reads ``#`` as a comment. A dry run adds the lessons in a temporary
worktree, prints the diff and the router's decision, and removes the worktree. ``--push`` pushes
the branch, opens the PR, comments the decision and records it in data/lessons.db. Sifu never
merges it: conventions.md is protected, so a human reviews and merges.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from sifu import db
from sifu.config import load_config
from sifu.credentials import get_github_token
from sifu.github import GitHubClient
from sifu.lessons_pr import discard, load_lessons, prepare, publish
from sifu.models import DecisionType

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "lessons.db"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lessons", required=True, help="YAML list of {lesson, evidence}")
    parser.add_argument(
        "--push", action="store_true", help="push and open the PR (default: dry run)"
    )
    args = parser.parse_args()
    cfg = load_config(ROOT / "config.yaml")
    repo = Path(cfg.workspace.product_repo_path).expanduser()
    lessons = load_lessons(args.lessons)
    name = f"lessons-{time.strftime('%Y%m%d-%H%M%S')}"

    change = prepare(
        repo, cfg.workspace.worktrees_dir, lessons, cfg.router, name, base=cfg.github.base_branch
    )
    for item in change.skipped:
        print("ALREADY IN conventions.md:", item.lesson)
    print(change.diff)
    d = change.decision
    print(f"Router: tier {d.final_tier.name} (floor {d.floor_tier.name}), rules {d.rules_fired}")

    if not args.push:
        discard(repo, change.worktree, change.branch)
        print("\nDry run: nothing pushed. Pass --push to open the PR.")
        return

    token = get_github_token(cfg.github)  # a SecretStr; never printed
    out = publish(change, cfg.github, GitHubClient.from_config(cfg.github), token)
    conn = db.connect(DB)
    db.init_db(conn)
    project = db.get_or_create_project(conn, cfg.github.repo, str(repo), cfg.github.repo)
    ticket = db.create_ticket(
        conn, project, f"Lessons for conventions.md ({len(change.added)})", change.diff
    )
    db.record_decision(conn, ticket, DecisionType.MERGE, d, [])
    db.append_event(conn, ticket, "pr_opened", "sifu", out)
    print(f"\nOpened {out['pr_url']} on {out['branch']}; waiting for a human to merge it.")


if __name__ == "__main__":
    main()
