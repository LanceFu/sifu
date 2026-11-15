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

from sifu import db
from sifu.demo import answer_request, load_seed_tickets, show_decisions
from sifu.graph import is_paused, run_ticket
from sifu.live import DEFAULT_DB, open_live

DB = str(DEFAULT_DB)


def main() -> None:
    """Parse the command line and run the script."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key", default="clean", help="seed ticket key (default: clean)")
    parser.add_argument("--title", help="use this ticket title instead of the seed ticket's")
    parser.add_argument("--body", help="use this ticket body instead of the seed ticket's")
    parser.add_argument("--merge-mode", default="manual", choices=["manual", "risk_based", "auto"])
    parser.add_argument(
        "--planner", action="store_true", help="write and gate on plan.md (planner.enabled)"
    )
    parser.add_argument("--db", default=DB, help="database to use (default: data/live.db)")
    parser.add_argument("--answer", nargs=2, metavar=("REQUEST_ID", "ANSWER"))
    args = parser.parse_args()
    deps, project_id = open_live(args.db, merge_mode=args.merge_mode, planner=args.planner)
    if args.answer:
        answer_request(deps, int(args.answer[0]), args.answer[1])
        return
    seed = next(s for s in load_seed_tickets() if s["key"] == args.key)
    title, body = args.title or seed["title"], args.body or seed["body"]
    ticket_id = db.create_ticket(deps.conn, project_id, title, body)
    # without the planner there is no plan to accept, so the gate is opened up front
    facts = {} if args.planner else {"plan_accepted": True}
    state = run_ticket(deps, ticket_id, facts=facts)
    print(f"ticket {ticket_id}: {'PAUSED' if is_paused(state) else state.get('status')}")
    print("PR:", state.get("pr_url"), "| outcome:", state.get("outcome"))
    show_decisions(deps)
    for event in db.list_events(deps.conn, ticket_id)[-8:]:
        print(event["kind"], event["actor"])


if __name__ == "__main__":
    main()
