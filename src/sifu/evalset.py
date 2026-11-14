"""The golden tickets for the Design stage and the scorer that compares systems on them.

``golden/tickets.yaml`` lists tickets with a seeded defect (or none). A *system* (the swarm or the
single-prompt baseline) runs each ticket and produces one record per run; ``score_record`` marks
the record against the ticket's labels and ``summarize`` turns the scores into the numbers in the
results table:

* detection rate by defect type: a seeded defect counts as caught when the ticket is blocked
  (``questions``); a minor glossary slip is caught when the finding is raised; a protected path is
  caught when the ticket goes to a human.
* false-positive rate: tickets that should get a spec but were blocked.
* false-auto rate: tickets that need a human but went on without one (the costly error).
* over-escalation rate: tickets that could be automatic but paused for a human.
* schema-valid rate and first-try rate of the model calls, tokens and latency per ticket.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

GOLDEN_TICKETS = Path(__file__).resolve().parents[2] / "golden" / "tickets.yaml"

DEFECTS = (
    "clean",
    "ambiguity",
    "scope",
    "glossary",
    "out_of_scope",
    "contradiction",
    "injection",
    "protected_path",
)
OUTCOMES = ("spec", "questions")
ROUTES = ("auto", "human")
CATCHERS = ("checks", "models", "floor")


@dataclass(frozen=True)
class GoldenTicket:
    key: str
    defect: str
    title: str
    body: str
    expect: str  # spec | questions
    route: str  # auto | human
    flag: str | None = None  # a finding category that must appear even when a spec is written
    by: str = ""  # checks | models | floor: who can catch it (reporting only)

    @property
    def text(self) -> str:
        """The text the deterministic checks read, as the graph builds it."""
        return f"{self.title}\n{self.body}"


def load_tickets(path: Path = GOLDEN_TICKETS, only: list[str] | None = None) -> list[GoldenTicket]:
    """Read and validate the golden tickets. ``only`` keeps the listed keys (and checks them)."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))["tickets"]
    tickets, seen = [], set()
    for item in raw:
        ticket = GoldenTicket(**item)
        if ticket.key in seen:
            raise ValueError(f"duplicate golden ticket key {ticket.key!r}")
        seen.add(ticket.key)
        if ticket.defect not in DEFECTS:
            raise ValueError(f"{ticket.key}: defect {ticket.defect!r} is not one of {DEFECTS}")
        if ticket.expect not in OUTCOMES:
            raise ValueError(f"{ticket.key}: expect must be one of {OUTCOMES}")
        if ticket.route not in ROUTES:
            raise ValueError(f"{ticket.key}: route must be one of {ROUTES}")
        if ticket.by and ticket.by not in CATCHERS:
            raise ValueError(f"{ticket.key}: by must be one of {CATCHERS}")
        if ticket.defect != "clean" and not ticket.by:
            raise ValueError(f"{ticket.key}: a seeded defect needs a 'by' label")
        tickets.append(ticket)
    if only:
        missing = set(only) - seen
        if missing:
            raise ValueError(f"unknown golden ticket keys: {sorted(missing)}")
        tickets = [t for t in tickets if t.key in only]
    return tickets


@dataclass
class TicketScore:
    key: str
    defect: str
    by: str
    detected: bool | None  # None for clean tickets: nothing was seeded
    outcome_ok: bool
    false_positive: bool
    false_auto: bool
    over_escalation: bool
    errored: bool


def score_record(ticket: GoldenTicket, record: dict[str, Any]) -> TicketScore:
    """Mark one run against the ticket's labels. A crashed run counts as a pause, not a pass."""
    errored = record["outcome"] == "error"
    outcome = "questions" if errored else record["outcome"]
    route = "human" if errored else record["route"]
    flags = set(record.get("flags", []))

    detected: bool | None
    if ticket.defect == "clean":
        detected = None
    elif ticket.defect == "protected_path":
        detected = route == "human"
    elif ticket.flag:
        detected = ticket.flag in flags
    else:
        detected = outcome == "questions"

    return TicketScore(
        key=ticket.key,
        defect=ticket.defect,
        by=ticket.by,
        detected=detected,
        outcome_ok=outcome == ticket.expect,
        false_positive=ticket.expect == "spec" and outcome == "questions",
        false_auto=ticket.route == "human" and route == "auto",
        over_escalation=ticket.route == "auto" and route == "human",
        errored=errored,
    )


def _rate(hit: int, total: int) -> float | None:
    return round(hit / total, 3) if total else None


def _group(scores: list[TicketScore], field: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[bool]] = defaultdict(list)
    for s in scores:
        if s.detected is not None:
            groups[getattr(s, field)].append(s.detected)
    return {
        name: {"caught": sum(v), "total": len(v), "rate": _rate(sum(v), len(v))}
        for name, v in sorted(groups.items())
    }


def summarize(
    tickets: list[GoldenTicket], records: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[TicketScore]]:
    """Score every record and compute the results-table numbers. Rates pool all repeats."""
    by_key = {t.key: t for t in tickets}
    scores = [score_record(by_key[r["key"]], r) for r in records]
    defect_scores = [s for s in scores if s.detected is not None]
    expect_spec = [s for s in scores if by_key[s.key].expect == "spec"]
    need_human = [s for s in scores if by_key[s.key].route == "human"]
    may_auto = [s for s in scores if by_key[s.key].route == "auto"]

    calls = sum(r.get("calls", 0) for r in records)
    first_try_calls = sum(r.get("first_try_calls", 0) for r in records)
    first_try_total = sum(r.get("first_try_total", 0) for r in records)
    n = len(records)

    by_ticket: dict[str, set[str]] = defaultdict(set)
    repeats: dict[str, int] = defaultdict(int)
    for r in records:
        by_ticket[r["key"]].add(r["outcome"])
        repeats[r["key"]] += 1
    repeated = [k for k, c in repeats.items() if c > 1]

    summary = {
        "runs": n,
        "tickets": len(by_key),
        "detection_rate": _rate(sum(bool(s.detected) for s in defect_scores), len(defect_scores)),
        "detection_by_defect": _group(scores, "defect"),
        "detection_by_catcher": _group(scores, "by"),
        "false_positive_rate": _rate(sum(s.false_positive for s in expect_spec), len(expect_spec)),
        "false_auto_rate": _rate(sum(s.false_auto for s in need_human), len(need_human)),
        "over_escalation_rate": _rate(sum(s.over_escalation for s in may_auto), len(may_auto)),
        "outcome_accuracy": _rate(sum(s.outcome_ok for s in scores), n),
        "errors": sum(s.errored for s in scores),
        "schema_valid_rate": _rate(sum(r.get("calls_ok", 0) for r in records), calls),
        "first_try_valid_rate": _rate(first_try_calls, first_try_total),
        "tokens_per_ticket": round(sum(r.get("tokens", 0) for r in records) / n) if n else None,
        "seconds_per_ticket": (
            round(sum(r.get("latency_ms", 0) for r in records) / n / 1000, 1) if n else None
        ),
        "stable_outcome_rate": (
            _rate(sum(len(by_ticket[k]) == 1 for k in repeated), len(repeated))
            if repeated
            else None
        ),
    }
    return summary, scores


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def _cell(group: dict[str, Any] | None) -> str:
    if not group:
        return "n/a"
    return f"{group['caught']}/{group['total']} ({_pct(group['rate'])})"


def format_markdown(summaries: dict[str, dict[str, Any]]) -> str:
    """One results table: a column per system or configuration, a row per metric."""
    names = list(summaries)
    rows: list[tuple[str, list[str]]] = []

    def add(label: str, fn) -> None:
        rows.append((label, [fn(summaries[n]) for n in names]))

    add("Runs (tickets x repeats)", lambda s: f"{s['runs']} ({s['tickets']} tickets)")
    add("Seeded defects caught", lambda s: _pct(s["detection_rate"]))
    defects = [d for d in DEFECTS if d != "clean"]
    for defect in defects:
        add(f"  {defect}", lambda s, d=defect: _cell(s["detection_by_defect"].get(d)))
    add("  caught by code alone (checks, floor)", lambda s: _cell(_merge(s, ("checks", "floor"))))
    add("  needs a model", lambda s: _cell(s["detection_by_catcher"].get("models")))
    add("False positives (good ticket blocked)", lambda s: _pct(s["false_positive_rate"]))
    add("False-auto (needed a human, did not get one)", lambda s: _pct(s["false_auto_rate"]))
    add("Over-escalation (could be auto, paused)", lambda s: _pct(s["over_escalation_rate"]))
    add("Right outcome (spec or questions)", lambda s: _pct(s["outcome_accuracy"]))
    add("Model calls schema-valid", lambda s: _pct(s["schema_valid_rate"]))
    add("Reviewer calls valid first try", lambda s: _pct(s["first_try_valid_rate"]))
    add("Tokens per ticket", lambda s: str(s["tokens_per_ticket"]))
    add("Seconds per ticket", lambda s: str(s["seconds_per_ticket"]))
    add("Same outcome on every repeat", lambda s: _pct(s["stable_outcome_rate"]))

    header = "| Metric | " + " | ".join(names) + " |"
    rule = "| :---- | " + " | ".join("----:" for _ in names) + " |"
    body = ["| " + label + " | " + " | ".join(cells) + " |" for label, cells in rows]
    return "\n".join([header, rule, *body])


def _merge(summary: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any] | None:
    groups = [
        summary["detection_by_catcher"][n] for n in names if n in summary["detection_by_catcher"]
    ]
    if not groups:
        return None
    caught, total = sum(g["caught"] for g in groups), sum(g["total"] for g in groups)
    return {"caught": caught, "total": total, "rate": _rate(caught, total)}
