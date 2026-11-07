"""Run the critics once on a worktree's diff with the real model and print what they say.

    uv run python scripts/critic_spike.py --worktree ~/Development/AgenticAI/worktrees/<name> \
        --spec data/artifacts/<db>/ticket-1/spec.md

Nothing is changed. It shows each kept finding, each dropped one with the reason, and the token
use, so you can judge the prompts on real code before turning ``critic.enabled`` on.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sifu.config import load_config
from sifu.critic import (
    added_index,
    merge_findings,
    run_critic,
    to_implementer_findings,
)
from sifu.llm import OllamaLLM
from sifu.verify import added_lines

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--spec", required=True, help="path to the ticket's spec.md")
    args = parser.parse_args()
    cfg = load_config(ROOT / "config.yaml")
    llm = OllamaLLM(cfg.ollama.base_url, cfg.ollama.num_ctx)
    index = added_index(added_lines(Path(args.worktree).expanduser()))
    spec = Path(args.spec).expanduser().read_text(encoding="utf-8")
    all_kept = []
    for name in cfg.critic.names:
        report, outcome = run_critic(
            name,
            spec_text=spec,
            index=index,
            checks="diff, build, swiftlint, test",
            llm=llm,
            model=cfg.models.code_reviewer,
            max_diff_chars=cfg.critic.max_diff_chars,
            min_scenario_words=cfg.critic.min_scenario_words,
        )
        print(
            f"\n== {name}: kept {len(report.kept)}, dropped {len(report.dropped)}, "
            f"degraded={report.degraded}, attempts={outcome.attempts}, "
            f"tokens={outcome.prompt_tokens}+{outcome.completion_tokens}"
        )
        all_kept.extend(report.kept)
        for f in report.kept:
            print("  KEEP", json.dumps(f))
        for f in report.dropped:
            print("  DROP", f["why"], "|", f["file"], f["line"], f["message"][:80])

    merged = merge_findings(all_kept)
    print(f"\n== after merging: {len(all_kept)} findings became {len(merged)}")
    for f in merged:
        print(f"  {f['severity']:7} {f['critic']} {f['file']}:{f['line']} {f['message'][:90]}")
    print("\n== sent back to the implementer (blockers only):")
    for f in to_implementer_findings(merged):
        print("  ", json.dumps(f))
    print("== posted as a PR comment (major and minor):")
    for f in merged:
        if f["severity"] != "blocker":
            print(f"   {f['severity']} {f['critic']} {f['file']}:{f['line']}")


if __name__ == "__main__":
    main()
