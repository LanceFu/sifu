"""Evaluate the Design stage: the swarm against a single-prompt baseline on the golden tickets.

    uv run python scripts/run_eval.py --check              # labels vs deterministic code, no model
    uv run python scripts/run_eval.py                      # swarm and baseline, one run each
    uv run python scripts/run_eval.py --repeats 3          # repeat to see how stable outcomes are
    uv run python scripts/run_eval.py --system swarm --label alt-gemma \\
        --set models.spec_reviewer=gemma4:31b-mlx --set models.author=gemma4:31b-mlx
    uv run python scripts/run_eval.py --only injection_paraphrase --only clean_stop_order

Needs Ollama running with the models in config.yaml. Writes data/artifacts/eval/design-<label>-
<time>.json (not committed) and prints the results table as markdown for the notebook.
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from sifu.context import ProductContext
from sifu.evalrun import (
    check_labels,
    eval_config,
    load_config_with_overrides,
    run_baseline,
    run_swarm,
    write_results,
)
from sifu.evalset import load_tickets
from sifu.llm import OllamaLLM
from sifu.provenance import finish_stamp, git_stamp, print_provenance

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--system", choices=["swarm", "baseline", "both"], default="both")
    parser.add_argument("--repeats", type=int, default=1, help="runs per ticket")
    parser.add_argument("--only", action="append", help="a ticket key (repeatable)")
    parser.add_argument("--label", default="main", help="names the results file")
    parser.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="override a config value, e.g. models.spec_reviewer=gemma4:31b-mlx",
    )
    parser.add_argument("--repo", help="product repo path (default: workspace.product_repo_path)")
    parser.add_argument("--check", action="store_true", help="check labels against code and stop")
    args = parser.parse_args()

    cfg = load_config_with_overrides(ROOT / "config.yaml", args.overrides)
    repo = Path(args.repo or cfg.workspace.product_repo_path).expanduser()
    ctx = ProductContext.load(repo)
    tickets = load_tickets(only=args.only)

    if args.check:
        problems = check_labels(tickets, ctx, eval_config(cfg))
        print(f"{len(tickets)} tickets checked against {repo}")
        print("\n".join(problems) if problems else "labels agree with the deterministic code")
        raise SystemExit(1 if problems else 0)

    llm = OllamaLLM(cfg.ollama.base_url, cfg.ollama.num_ctx)
    start = {"sifu": git_stamp(ROOT), "product": git_stamp(repo)}
    per_system = {}
    if args.system in ("swarm", "both"):
        with tempfile.TemporaryDirectory() as tmp:
            per_system["swarm"] = run_swarm(
                tickets, cfg, llm, ctx, artifacts_dir=tmp, repeats=args.repeats
            )
    if args.system in ("baseline", "both"):
        per_system["baseline"] = run_baseline(tickets, cfg, llm, ctx, repeats=args.repeats)

    out_dir = ROOT / cfg.workspace.artifacts_dir / "eval"
    provenance = {
        name: finish_stamp(s, ROOT if name == "sifu" else repo) for name, s in start.items()
    }
    path, table = write_results(out_dir, args.label, tickets, per_system, cfg, provenance)
    print("\n" + table)
    print_provenance(provenance)
    print("\nWrote", path)


if __name__ == "__main__":
    main()
