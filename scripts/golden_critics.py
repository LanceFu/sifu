"""Score the critics on the golden set with the real models.

    uv run python scripts/golden_critics.py                 # the tuning cases in golden/code
    uv run python scripts/golden_critics.py --heldout       # the held-out cases only
    uv run python scripts/golden_critics.py --only demo1_day_plan_limit
    uv run python scripts/golden_critics.py --checks-only   # Sifu's verify steps only, no model

Each case's patch is applied to a fresh clone of the product repo at the case's base commit. Sifu's
own verify steps (diff checks, swift build, SwiftLint, swift test) run first, as in the graph, then
the critic pass, and both are scored against the seeded flaws: the summary shows which flaws the
deterministic checks already catch and which only a critic found. Prints a table and writes a
JSON record to data/artifacts/golden/ (not committed).
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
from sifu.critic import added_index, review_worktree
from sifu.golden import is_heldout, load_cases, score_case, score_checks, summarize
from sifu.llm import OllamaLLM
from sifu.provenance import finish_stamp, git_stamp, print_provenance
from sifu.verify import added_lines, run_verify

ROOT = Path(__file__).resolve().parents[1]


def git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", action="append", help="a case name (repeatable)")
    parser.add_argument(
        "--heldout",
        action="store_true",
        help="run only the held-out cases; without it they are skipped so they stay unseen",
    )
    parser.add_argument("--no-verify", action="store_true", help="skip Sifu's verify steps")
    parser.add_argument(
        "--checks-only", action="store_true", help="run the verify steps only, no critic model"
    )
    args = parser.parse_args()
    cfg = load_config(ROOT / "config.yaml")
    llm = OllamaLLM(cfg.ollama.base_url, cfg.ollama.num_ctx)
    product = Path(cfg.workspace.product_repo_path).expanduser()
    checker = cfg.critic.confirm_model or cfg.models.author
    start = {"sifu": git_stamp(ROOT), "product": git_stamp(product)}

    scores, records = [], []
    cases = load_cases(only=args.only)
    if not args.only:  # naming a case runs it; otherwise tuning and held-out sets stay apart
        cases = [c for c in cases if is_heldout(c.name) == args.heldout]
    for case in cases:
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "repo"
            git("clone", "-q", "--no-hardlinks", str(product), str(tree))
            git("checkout", "-q", case.base, cwd=tree)
            git("apply", str(case.patch_path.resolve()), cwd=tree)
            added = added_lines(tree)
            checks = None
            if not args.no_verify:
                touched = sorted({name for name, _, _ in added})
                checks = run_verify(cfg, tree, touched)
            if args.checks_only:
                score = score_case(case, [], added_index(added))
                score_checks(score, case, [asdict(f) for f in checks.findings], added_index(added))
                scores.append(score)
                print_checks(case.name, score, checks)
                records.append({"score": asdict(score), "checks": checks.to_payload()})
                continue
            review = review_worktree(
                tree,
                case.spec_path.read_text(encoding="utf-8"),
                llm=llm,
                critic_cfg=cfg.critic,
                critic_model=cfg.models.code_reviewer,
                checker_model=checker,
                added=added,
            )
        score = score_case(case, review.merged, review.index)
        if checks is not None:
            score_checks(score, case, [asdict(f) for f in checks.findings], review.index)
            print_checks(case.name, score, checks)
        scores.append(score)
        found = sum(score.detected.values())
        print(
            f"\n{case.name}: {found}/{len(case.expected)} flaws found, "
            f"{len(score.false_positives)} false positives, {review.tokens} tokens, "
            f"{len(review.dropped)} dropped, degraded={review.degraded}"
        )
        for flaw_id, ok in score.detected.items():
            lane = "" if score.by_expected_critic[flaw_id] else "  (found by another critic)"
            near = "" if score.detected_exact[flaw_id] else "  (only within the line window)"
            print(f"  {'FOUND  ' if ok else 'MISSED '} {flaw_id}{lane + near if ok else ''}")
        for fp in score.false_positives:
            print(
                f"  FALSE+  {fp['severity']:7} {fp['critic']} {fp['file']}:{fp['line']} "
                f"{fp['message'][:80]}"
            )
        records.append(
            {
                "score": asdict(score),
                "dropped": review.dropped,
                "tokens": review.tokens,
                "checks": checks.to_payload() if checks is not None else None,
            }
        )

    summary = summarize(scores)
    print("\nSummary", json.dumps(summary))
    out = ROOT / "data" / "artifacts" / "golden"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"critics-{time.strftime('%Y%m%d-%H%M%S')}.json"
    provenance = {
        name: finish_stamp(s, ROOT if name == "sifu" else product) for name, s in start.items()
    }
    print_provenance(provenance)
    payload = {"provenance": provenance, "summary": summary, "cases": records}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print("Wrote", path)


def print_checks(name: str, score, checks) -> None:
    steps = " ".join(f"{s['name']}={'ok' if s.get('ok') else 'FAIL'}" for s in checks.steps)
    caught = [k for k, ok in score.caught_by_checks.items() if ok]
    print(f"\n{name} checks: {steps}; caught {caught or 'none'}")
    for f in score.unmapped_check_findings[:5]:
        print(f"  UNMAPPED {f['source']} {f['file']}:{f['line']} {f['message'][:90]}")


if __name__ == "__main__":
    main()
