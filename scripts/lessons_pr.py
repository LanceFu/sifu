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
from pathlib import Path

from sifu.config import load_config
from sifu.lessons_pr import load_lessons, run

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "lessons.db"


def main() -> None:
    """Parse the command line and run the script."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lessons", required=True, help="YAML list of {lesson, evidence}")
    parser.add_argument(
        "--push", action="store_true", help="push and open the PR (default: dry run)"
    )
    args = parser.parse_args()
    cfg = load_config(ROOT / "config.yaml")
    out = run(cfg, load_lessons(args.lessons), push=args.push, db_path=DB)
    for lesson in out["skipped"]:
        print("ALREADY IN conventions.md:", lesson)
    print(out["diff"])
    print(f"Router: tier {out['tier']}, rules {out['rules']}")
    if not args.push:
        print("\nDry run: nothing pushed. Pass --push to open the PR.")
        return
    print(f"\nOpened {out['pr_url']}; waiting for a human to merge it.")


if __name__ == "__main__":
    main()
