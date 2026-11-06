"""Run one seeded ticket end to end against real GitHub (implement, verify, PR, merge decision).

    uv run python scripts/live_run.py                      # run the "clean" seed ticket
    uv run python scripts/live_run.py --answer 1 approve   # answer open request 1 and resume

State is kept in data/live.db, so a ticket that pauses for a human (the merge decision with
merge_mode manual) can be resumed in a second command. Needs: Ollama running (the design stage),
`claude` logged in, a GitHub token (SIFU_GITHUB_TOKEN or the Keychain item), and a clean clone of
the product repo at workspace.product_repo_path.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from sifu import db
from sifu.config import load_config
from sifu.credentials import get_github_token
from sifu.demo import answer_request, load_seed_tickets, open_demo, show_decisions
from sifu.github import GitHubClient
from sifu.graph import is_paused, run_ticket
from sifu.implementer import Implementer
from sifu.pr_flow import publish_branch
from sifu.verify import Verifier

ROOT = Path(__file__).resolve().parents[1]
DB = str(ROOT / "data" / "live.db")


def open_live(merge_mode: str):
    cfg = load_config(ROOT / "config.yaml")
    cfg.router.merge_mode = merge_mode
    deps, project_id = open_demo(cfg, db_path=DB)
    token = get_github_token(cfg.github)  # a SecretStr; never printed
    deps.implementer = Implementer(cfg)
    deps.verifier = Verifier(cfg)
    deps.github = GitHubClient.from_config(cfg.github)
    deps.publisher = lambda worktree, branch, message: publish_branch(
        cfg.github, worktree, branch, message, token
    )
    return deps, project_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key", default="clean", help="seed ticket key (default: clean)")
    parser.add_argument("--merge-mode", default="manual", choices=["manual", "risk_based", "auto"])
    parser.add_argument("--answer", nargs=2, metavar=("REQUEST_ID", "ANSWER"))
    args = parser.parse_args()
    deps, project_id = open_live(args.merge_mode)
    if args.answer:
        answer_request(deps, int(args.answer[0]), args.answer[1])
        return
    seed = next(s for s in load_seed_tickets() if s["key"] == args.key)
    ticket_id = db.create_ticket(deps.conn, project_id, seed["title"], seed["body"])
    state = run_ticket(deps, ticket_id)
    print(f"ticket {ticket_id}: {'PAUSED' if is_paused(state) else state.get('status')}")
    print("PR:", state.get("pr_url"), "| outcome:", state.get("outcome"))
    show_decisions(deps)
    for event in db.list_events(deps.conn, ticket_id)[-8:]:
        print(event["kind"], event["actor"])


if __name__ == "__main__":
    main()
