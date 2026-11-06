"""Check two verify-loop paths on real GitHub by planting a SwiftLint violation.

    uv run python scripts/live_probe.py once      # violation planted in round 1 only
    uv run python scripts/live_probe.py always    # violation planted in every round

``once``: the real implementer is resumed with the SwiftLint finding and must fix it, so verify
fails in round 1, passes in round 2, and a PR opens (the plan's "sent back and fixed" check).
Answer the merge request with ``close`` to leave the PR open without merging.

``always``: the violation comes back after every fix, so the loop gives up (verify_no_progress)
and Sifu opens a *draft* PR with the findings as a comment. The ticket stays in Needs Human.

Each run uses its own fresh database (data/probe-<scenario>.db) and a unique branch name, and
ends with a PASS/FAIL checklist. The planted code is a force cast, which SwiftLint reports as
``force_cast``. Needs the same setup as scripts/live_run.py.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from live_run import ROOT, open_live

from sifu import db
from sifu.demo import show_decisions
from sifu.graph import is_paused, run_ticket
from sifu.implementer import Implementer
from sifu.verify import Verifier

# A ticket still open in TripPlanner (REQ-3 is "Planned"), so the implementer has work to do.
TITLE = "Limit a Day Plan to 10 Stops"
BODY = (
    "Given a Day Plan already has 10 Stops, When a Traveller adds another Stop, Then adding fails "
    "with a tooManyStops error and the Day Plan is unchanged (REQ-3)."
)
PROBE = "\nfunc sifuLintProbe(_ value: Any) -> Int { value as! Int }\n"


class Probe:
    """Wraps the real implementer and plants a force cast in a changed Swift file afterwards."""

    def __init__(self, inner: Implementer, scenario: str) -> None:
        self.inner, self.scenario, self.calls = inner, scenario, 0

    def __call__(self, ticket_id, spec_text, conventions, feedback=None, session_id=None):
        result = self.inner(ticket_id, spec_text, conventions, feedback, session_id)
        self.calls += 1
        if result.ok and (self.scenario == "always" or self.calls == 1):
            target = next(
                (
                    f
                    for f in result.changed_files
                    if f.startswith("Sources/") and f.endswith(".swift")
                ),
                None,
            )
            if target is None:
                raise SystemExit(
                    f"no Sources/*.swift file among the changes {result.changed_files}; cannot "
                    "plant. If the ticket is already implemented on main, change TITLE and BODY."
                )
            with open(Path(result.worktree) / target, "a", encoding="utf-8") as handle:
                handle.write(PROBE)
            print(f"[probe] round {self.calls}: planted a force cast in {target}")
        return result


def kinds(deps, ticket_id) -> list[str]:
    return [e["kind"] for e in db.list_events(deps.conn, ticket_id)]


def verify_oks(deps, ticket_id) -> list[bool]:
    rows = [e for e in db.list_events(deps.conn, ticket_id) if e["kind"] == "verify_result"]
    return [json.loads(e["payload"])["ok"] for e in rows]


def report(checks: list[tuple[str, bool]]) -> None:
    print("\nChecklist")
    for label, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=["once", "always"])
    args = parser.parse_args()

    db_path = ROOT / "data" / f"probe-{args.scenario}.db"
    for leftover in db_path.parent.glob(db_path.name + "*"):
        leftover.unlink()
    deps, project_id = open_live("manual", db_path=str(db_path))
    tag = f"probe-{args.scenario}-{time.strftime('%m%d%H%M')}"
    deps.implementer = Probe(Implementer(deps.cfg, tag=tag), args.scenario)
    deps.verifier = Verifier(deps.cfg, tag=tag)

    ticket_id = db.create_ticket(deps.conn, project_id, TITLE, BODY)
    state = run_ticket(deps, ticket_id, facts={"plan_accepted": True})
    status = db.get_ticket(deps.conn, ticket_id)["status"]
    seen, oks = kinds(deps, ticket_id), verify_oks(deps, ticket_id)
    print(f"\nticket {ticket_id}: status={status} outcome={state.get('outcome')} verify={oks}")
    print("PR:", state.get("pr_url"))
    show_decisions(deps)

    if args.scenario == "once":
        report(
            [
                ("verify failed in round 1 and passed in round 2", oks[:2] == [False, True]),
                (
                    "the implementer ran twice (the second time with the findings)",
                    seen.count("implementer_result") == 2,
                ),
                (
                    "a normal (not draft) PR was opened",
                    "pr_opened" in seen and "draft_pr_opened" not in seen,
                ),
                ("the merge decision is waiting for you", is_paused(state)),
            ]
        )
        print(
            "\nOn GitHub: the PR is not a draft, has Sifu's decision comment, and its diff has no "
            "sifuLintProbe. Then close the ticket and leave the PR open for you to close:\n"
            "  uv run python scripts/live_run.py --db data/probe-once.db "
            "--answer <request id> close"
        )
    else:
        report(
            [
                ("verify never passed", not any(oks)),
                (
                    "the loop stopped on its own",
                    state.get("outcome") in ("verify_no_progress", "verify_cap"),
                ),
                ("a draft PR was opened", "draft_pr_opened" in seen and "pr_opened" not in seen),
                ("the ticket is waiting in needs_human", status == "needs_human"),
            ]
        )
        print(
            "\nOn GitHub: the PR is marked Draft, titled '[Draft] ...', and has a comment naming "
            "swiftlint force_cast and why Sifu stopped. Close the PR and delete the branch."
        )


if __name__ == "__main__":
    main()
