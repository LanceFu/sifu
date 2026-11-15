"""Results tables for the notebook, built only from the JSON files the eval scripts write.

Nothing here calls a model or GitHub: the notebook can show the evaluation on any machine that has
``data/artifacts``. Each function returns Markdown for ``IPython.display.Markdown``.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

ARTIFACTS = Path(__file__).resolve().parents[2] / "data" / "artifacts"

# (summary key, row label, kind): kind "pct" shows a percentage, "num" a whole number
DESIGN_ROWS = [
    ("detection_rate", "Seeded defects caught", "pct"),
    ("false_positive_rate", "False positives (good ticket blocked)", "pct"),
    ("false_auto_rate", "False-auto (needed a human, did not get one)", "pct"),
    ("over_escalation_rate", "Over-escalation (could be auto, paused)", "pct"),
    ("outcome_accuracy", "Right outcome", "pct"),
    ("stable_outcome_rate", "Same outcome on every repeat", "pct"),
    ("tokens_per_ticket", "Tokens per ticket", "num"),
    ("seconds_per_ticket", "Seconds per ticket", "num"),
]


def load(path: str | Path) -> dict[str, Any]:
    """Read one JSON results file."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def latest(kind: str, label: str, root: Path = ARTIFACTS) -> Path:
    """The newest results file for a label: ``latest("eval", "v2")`` -> eval/design-v2-*.json."""
    # the digit keeps "alt" from matching alt-v2 files
    pattern = f"design-{label}-[0-9]*.json" if kind == "eval" else f"{label}-[0-9]*.json"
    found = sorted((root / kind).glob(pattern))
    if not found:
        raise FileNotFoundError(f"no {pattern} in {root / kind}")
    return found[-1]


def _cell(value: Any, kind: str) -> str:
    if value is None:
        return "–"
    return f"{value:.0%}" if kind == "pct" else f"{value:,.0f}"


def _stamp(run: dict[str, Any]) -> str:
    prov = run.get("provenance")
    if not prov:
        return "not stamped"
    sifu = prov["sifu"]
    flag = " (changed during run)" if sifu.get("changed_during_run") else ""
    dirty = " dirty" if sifu.get("dirty") else ""
    return f"`{sifu['commit'][:7]}`{dirty}{flag}"


def design_table(runs: dict[str, dict[str, Any]], systems: list[str] | None = None) -> str:
    """One column per (run, system): ``runs`` maps a column-group name to a loaded eval file."""
    cols = [
        (name, system)
        for name, run in runs.items()
        for system in run["summaries"]
        if systems is None or system in systems
    ]
    head = "| Metric | " + " | ".join(f"{n} {s}" for n, s in cols) + " |"
    rule = "| :--- | " + " | ".join("---:" for _ in cols) + " |"
    lines = [head, rule]
    for key, label, kind in DESIGN_ROWS:
        cells = [_cell(runs[n]["summaries"][s].get(key), kind) for n, s in cols]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines.append(
        "| Runs | " + " | ".join(str(runs[n]["summaries"][s]["runs"]) for n, s in cols) + " |"
    )
    lines.append("| Sifu commit | " + " | ".join(_stamp(runs[n]) for n, _ in cols) + " |")
    return "\n".join(lines)


def wrong_tickets(run: dict[str, Any]) -> str:
    """Tickets with a wrong outcome on any repeat, per system, with how many repeats were wrong."""
    lines = ["| System | Ticket | Wrong / repeats | Why |", "| :--- | :--- | ---: | :--- |"]
    for system, scores in run["scores"].items():
        tally: dict[str, list[bool]] = defaultdict(list)
        for s in scores:
            tally[s["key"]].append(s["outcome_ok"])
        for key, oks in tally.items():
            if all(oks):
                continue
            why = next(
                "false positive"
                if s["false_positive"]
                else "over-escalation"
                if s["over_escalation"]
                else "false-auto"
                if s["false_auto"]
                else "missed defect"
                for s in scores
                if s["key"] == key and not s["outcome_ok"]
            )
            lines.append(f"| {system} | {key} | {oks.count(False)} / {len(oks)} | {why} |")
    return "\n".join(lines) if len(lines) > 2 else "No ticket was wrong on any repeat."


def critic_table(
    sets: dict[str, dict[str, Any]], checks: dict[str, dict[str, Any]] | None = None
) -> str:
    """Critic golden-set results; ``checks`` adds a checks-only run for a set run without verify."""
    checks = checks or {}
    rows = [
        ("Seeded flaws found (within 2 lines)", lambda s: f"{s['detected']}/{s['flaws']}"),
        ("Seeded flaws found (exact line)", lambda s: f"{s['detected_exact']}/{s['flaws']}"),
        ("Caught by build, SwiftLint, tests", None),
        ("Found only by the critics", None),
        ("False positives (all findings)", lambda s: str(s["false_positives"])),
        (
            "Clean diffs passed with no blocker",
            lambda s: (
                f"{round(s['clean_first_pass_rate'] * s['clean_cases'])}/{s['clean_cases']}"
                if s["clean_cases"]
                else "–"
            ),
        ),
    ]
    names = list(sets)
    lines = [
        "| | " + " | ".join(names) + " |",
        "| :--- | " + " | ".join("---:" for _ in names) + " |",
    ]
    for label, fn in rows:
        cells = []
        for n in names:
            s = sets[n]["summary"]
            if fn is not None:
                cells.append(fn(s))
                continue
            caught = s.get("caught_by_checks")
            if caught is None and n in checks:  # verify ran separately: combine with the critic run
                c = checks[n]["summary"]["caught_by_checks"]
                caught = c
            if caught is None:
                cells.append("–")
            elif label.startswith("Caught"):
                cells.append(f"{caught}/{s['flaws']}")
            else:
                beyond = s.get("found_beyond_checks")
                if beyond is None:
                    beyond = _beyond(sets[n], checks[n])
                cells.append(f"{beyond}/{s['flaws']}")
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines.append(
        "| Tokens, whole set | "
        + " | ".join(f"{sum(c.get('tokens', 0) for c in sets[n]['cases']):,}" for n in names)
        + " |"
    )
    lines.append("| Sifu commit | " + " | ".join(_stamp(sets[n]) for n in names) + " |")
    return "\n".join(lines)


def _beyond(critics: dict[str, Any], checks: dict[str, Any]) -> int:
    caught = {c["score"]["name"]: c["score"]["caught_by_checks"] for c in checks["cases"]}
    return sum(
        1
        for c in critics["cases"]
        for flaw, ok in c["score"]["detected"].items()
        if ok and not caught.get(c["score"]["name"], {}).get(flaw, False)
    )


def tokens_by_case(run: dict[str, Any]) -> str:
    """Tokens the critics used per case, to spot a reply that ran to the output cap."""
    lines = ["| Case | Tokens | Seeded flaws | False positives |", "| :--- | ---: | ---: | ---: |"]
    for c in run["cases"]:
        s = c["score"]
        flaws = f"{sum(s['detected'].values())}/{len(s['detected'])}" if s["detected"] else "clean"
        lines.append(
            f"| {s['name']} | {c.get('tokens', 0):,} | {flaws} | {len(s['false_positives'])} |"
        )
    return "\n".join(lines)
