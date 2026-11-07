"""Score the critics on the golden set with the real models.

    uv run python scripts/golden_critics.py                 # every case in golden/code
    uv run python scripts/golden_critics.py --only demo1_day_plan_limit

Each case's patch is applied to a fresh clone of the product repo at the case's base commit, the
critic pass runs exactly as in the graph, and the merged findings are scored against the seeded
flaws. Prints a table and writes a JSON record to data/artifacts/golden/ (not committed).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from sifu.config import load_config
from sifu.critic import review_worktree
from sifu.golden import load_cases, score_case, summarize
from sifu.llm import OllamaLLM
from sifu.verify import added_lines

ROOT = Path(__file__).resolve().parents[1]


def git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", action="append", help="a case name (repeatable)")
    args = parser.parse_args()
    cfg = load_config(ROOT / "config.yaml")
    llm = OllamaLLM(cfg.ollama.base_url, cfg.ollama.num_ctx)
    product = Path(cfg.workspace.product_repo_path).expanduser()
    checker = cfg.critic.confirm_model or cfg.models.author

    scores, records = [], []
    for case in load_cases(only=args.only):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "repo"
            git("clone", "-q", "--no-hardlinks", str(product), str(tree))
            git("checkout", "-q", case.base, cwd=tree)
            git("apply", str(case.patch_path.resolve()), cwd=tree)
            review = review_worktree(
                tree,
                case.spec_path.read_text(encoding="utf-8"),
                llm=llm,
                critic_cfg=cfg.critic,
                critic_model=cfg.models.code_reviewer,
                checker_model=checker,
                added=added_lines(tree),
            )
        score = score_case(case, review.merged, review.index)
        scores.append(score)
        found = sum(score.detected.values())
        print(
            f"\n{case.name}: {found}/{len(case.expected)} flaws found, "
            f"{len(score.false_positives)} false positives, {review.tokens} tokens, "
            f"{len(review.dropped)} dropped, degraded={review.degraded}"
        )
        for flaw_id, ok in score.detected.items():
            lane = "" if score.by_expected_critic[flaw_id] else "  (found by another critic)"
            print(f"  {'FOUND  ' if ok else 'MISSED '} {flaw_id}{lane if ok else ''}")
        for fp in score.false_positives:
            print(
                f"  FALSE+  {fp['severity']:7} {fp['critic']} {fp['file']}:{fp['line']} "
                f"{fp['message'][:80]}"
            )
        records.append({"score": asdict(score), "dropped": review.dropped, "tokens": review.tokens})

    summary = summarize(scores)
    print("\nSummary", json.dumps(summary))
    out = ROOT / "data" / "artifacts" / "golden"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"critics-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps({"summary": summary, "cases": records}, indent=2), encoding="utf-8")
    print("Wrote", path)


if __name__ == "__main__":
    main()
