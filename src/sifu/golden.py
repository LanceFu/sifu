"""The golden set for the critics: seeded flaws and clean twins, with a scorer.

Each case in ``golden/code/<name>/`` has a ``case.yaml``, the ticket's ``spec.md`` and a
``change.patch`` that applies to the product repo at ``base``. A flaw counts as detected when a
kept finding sits on a line of the same file that contains one of the flaw's quotes. Any kept
finding that matches no flaw is a false positive; on a clean case every finding is one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

GOLDEN_DIR = Path(__file__).resolve().parents[2] / "golden" / "code"


@dataclass(frozen=True)
class Flaw:
    id: str
    file: str
    quotes: list[str]
    critic: str


@dataclass(frozen=True)
class Case:
    name: str
    base: str
    spec_path: Path
    patch_path: Path
    expected: list[Flaw]
    notes: str = ""

    @property
    def clean(self) -> bool:
        return not self.expected


@dataclass
class CaseScore:
    name: str
    clean: bool
    detected: dict[str, bool] = field(default_factory=dict)
    by_expected_critic: dict[str, bool] = field(default_factory=dict)
    false_positives: list[dict[str, Any]] = field(default_factory=list)

    @property
    def blocker_false_positives(self) -> int:
        return sum(1 for f in self.false_positives if f["severity"] == "blocker")


def load_cases(root: Path = GOLDEN_DIR, only: list[str] | None = None) -> list[Case]:
    cases = []
    for folder in sorted(p for p in root.iterdir() if (p / "case.yaml").is_file()):
        if only and folder.name not in only:
            continue
        raw = yaml.safe_load((folder / "case.yaml").read_text(encoding="utf-8"))
        cases.append(
            Case(
                name=folder.name,
                base=str(raw["base"]),
                spec_path=folder / "spec.md",
                patch_path=folder / "change.patch",
                expected=[Flaw(**{**e, "quotes": list(e["quotes"])}) for e in raw["expected"]],
                notes=raw.get("notes", ""),
            )
        )
    return cases


def _squash(text: str) -> str:
    return " ".join(text.split())


def score_case(
    case: Case, findings: list[dict[str, Any]], index: dict[str, dict[int, str]]
) -> CaseScore:
    """Score merged findings against the case's seeded flaws."""
    score = CaseScore(case.name, case.clean)
    matched: set[int] = set()
    for flaw in case.expected:
        hits = []
        for i, f in enumerate(findings):
            text = _squash(index.get(f["file"], {}).get(f["line"], ""))
            if f["file"] == flaw.file and any(_squash(q) in text for q in flaw.quotes):
                hits.append(i)
        matched.update(hits)
        score.detected[flaw.id] = bool(hits)
        score.by_expected_critic[flaw.id] = any(
            flaw.critic in findings[i]["critic"].split("+") for i in hits
        )
    score.false_positives = [
        {k: f[k] for k in ("file", "line", "severity", "critic", "message")}
        for i, f in enumerate(findings)
        if i not in matched
    ]
    return score


def summarize(scores: list[CaseScore]) -> dict[str, Any]:
    flaws = [d for s in scores for d in s.detected.values()]
    clean = [s for s in scores if s.clean]
    return {
        "cases": len(scores),
        "flaws": len(flaws),
        "detected": sum(flaws),
        "detection_rate": round(sum(flaws) / len(flaws), 3) if flaws else None,
        "false_positives": sum(len(s.false_positives) for s in scores),
        "blocker_false_positives": sum(s.blocker_false_positives for s in scores),
        "clean_cases": len(clean),
        "clean_cases_with_findings": sum(1 for s in clean if s.false_positives),
    }
