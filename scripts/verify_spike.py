"""Run the deterministic review stage once against a worktree and print the findings.

    uv run python scripts/verify_spike.py \
        --worktree ~/Development/AgenticAI/worktrees/ticket-9001-cloud --affected Sources/

It runs the diff checks and then the steps from ``verify`` in config.yaml (swift build, SwiftLint,
swift test) exactly as the graph does, so it is the way to check the parsers against real tool
output on your Mac. Nothing is changed in the worktree.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sifu.config import load_config
from sifu.verify import run_verify

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", required=True, help="a ticket worktree to check")
    parser.add_argument(
        "--affected",
        action="append",
        default=[],
        help="a path the spec lists (repeat); with none, the out-of-spec check is skipped",
    )
    args = parser.parse_args()
    cfg = load_config(ROOT / "config.yaml")
    result = run_verify(cfg, Path(args.worktree).expanduser(), args.affected)
    print(json.dumps(result.to_payload(cfg.verify.max_findings), indent=2))
    raise SystemExit(0 if result.ok else 1)


if __name__ == "__main__":
    main()
