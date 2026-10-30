"""Run the headless Claude Code implementer once against a product repo (the Nov 4-6 spike).

    uv run python scripts/implementer_spike.py --repo ~/Development/AgenticAI/tripplanner-ios

It creates a worktree on branch sifu/ticket-<id>, runs ``claude -p`` with the allow-list and
turn cap from config.yaml, and prints what happened. Nothing is pushed. Remove the worktree
afterwards with
``git -C <repo> worktree remove --force <path>`` and ``git -C <repo> branch -D sifu/ticket-<id>``.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from sifu.config import load_config
from sifu.implementer import Implementer

ROOT = Path(__file__).resolve().parents[1]
SPEC = """# Reject blank Trip names

Given a Traveller saves a Trip, When the name is empty or only spaces,
Then saving fails with a nameBlank error and the Trip is not created (REQ-1).
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="product repo (default: workspace.product_repo_path)")
    parser.add_argument("--worktrees", help="worktrees dir (default: workspace.worktrees_dir)")
    parser.add_argument("--ticket", type=int, default=9001)
    parser.add_argument(
        "--spec-file", help="markdown spec to implement (default: blank Trip names)"
    )
    args = parser.parse_args()

    cfg = load_config(ROOT / "config.yaml")
    if args.repo:
        cfg.workspace.product_repo_path = args.repo
    if args.worktrees:
        cfg.workspace.worktrees_dir = args.worktrees
    spec = Path(args.spec_file).read_text(encoding="utf-8") if args.spec_file else SPEC
    repo = Path(cfg.workspace.product_repo_path).expanduser()
    conventions = (repo / "conventions.md").read_text(encoding="utf-8")

    result = Implementer(cfg)(args.ticket, spec, conventions)
    print(json.dumps(asdict(result), indent=2))


if __name__ == "__main__":
    main()
