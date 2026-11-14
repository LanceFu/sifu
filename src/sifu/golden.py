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
    # the same, counting only findings whose cited line holds the quote (no LINE_SLACK)
    detected_exact: dict[str, bool] = field(default_factory=dict)
    by_expected_critic: dict[str, bool] = field(default_factory=dict)
    false_positives: list[dict[str, Any]] = field(default_factory=list)
    # Sifu's own verify steps on the patched tree; None when the case ran without them
    checks_ok: bool | None = None
    caught_by_checks: dict[str, bool] = field(default_factory=dict)
    unmapped_check_findings: list[dict[str, Any]] = field(default_factory=list)

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


# A finding counts for a flaw when the flaw's quote is on the cited line or this many lines either
# side: models often cite the signature or body line next to the one a label quotes.
LINE_SLACK = 2


def is_heldout(name: str) -> bool:
    """Held-out cases were written after the first critic run and never used to tune a prompt."""
    return "heldout" in name


def _squash(text: str) -> str:
    return " ".join(text.split())


def _quoted(flaw: Flaw, f: dict[str, Any], slack: int, index: dict[str, dict[int, str]]) -> bool:
    """Whether the finding's file is the flaw's and a quote is within ``slack`` lines of it."""
    if f.get("file") != flaw.file or f.get("line") is None:
        return False
    lines = index.get(f["file"], {})
    near = range(f["line"] - slack, f["line"] + slack + 1)
    text = " | ".join(_squash(lines.get(n, "")) for n in near)
    return any(_squash(q) in text for q in flaw.quotes)


def score_checks(
    score: CaseScore,
    case: Case,
    check_findings: list[dict[str, Any]],
    index: dict[str, dict[int, str]],
) -> CaseScore:
    """Add what Sifu's deterministic verify steps caught, so the critics' share beyond them shows.

    A flaw counts as caught when a check finding cites a line within LINE_SLACK of its quote. A
    check finding that matches no flaw (a test failure in a test file, a build error elsewhere) is
    kept as unmapped: on a clean case it means the patch itself is broken.
    """
    score.checks_ok = not check_findings
    # same rule as score_case: a finding on one flaw's exact line does not claim its neighbours
    exact = {
        i
        for i, f in enumerate(check_findings)
        for flaw in case.expected
        if _quoted(flaw, f, 0, index)
    }
    mapped: set[int] = set()
    for flaw in case.expected:
        hits = {
            i
            for i, f in enumerate(check_findings)
            if _quoted(flaw, f, 0, index)
            or (i not in exact and _quoted(flaw, f, LINE_SLACK, index))
        }
        mapped |= hits
        score.caught_by_checks[flaw.id] = bool(hits)
    score.unmapped_check_findings = [f for i, f in enumerate(check_findings) if i not in mapped]
    return score


def score_case(
    case: Case, findings: list[dict[str, Any]], index: dict[str, dict[int, str]]
) -> CaseScore:
    """Score merged findings against the case's seeded flaws."""
    score = CaseScore(case.name, case.clean)

    def quoted(flaw: Flaw, f: dict[str, Any], slack: int) -> bool:
        return _quoted(flaw, f, slack, index)

    # a finding whose own line holds a flaw's quote counts for that flaw only; the slack applies
    # to findings that hit no flaw exactly, so one finding in dense code cannot claim its neighbours
    exact = {i for i, f in enumerate(findings) for fl in case.expected if quoted(fl, f, 0)}
    matched: set[int] = set()
    for flaw in case.expected:
        hits = [
            i
            for i, f in enumerate(findings)
            if quoted(flaw, f, 0) or (i not in exact and quoted(flaw, f, LINE_SLACK))
        ]
        matched.update(hits)
        score.detected[flaw.id] = bool(hits)
        score.detected_exact[flaw.id] = any(quoted(flaw, f, 0) for f in findings)
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
    exact = [d for s in scores for d in s.detected_exact.values()]
    clean = [s for s in scores if s.clean]
    verified = [s for s in scores if s.checks_ok is not None]
    caught = beyond = None
    if verified:
        caught = sum(sum(s.caught_by_checks.values()) for s in verified)
        beyond = sum(
            1 for s in verified for k, ok in s.detected.items() if ok and not s.caught_by_checks[k]
        )
    return {
        "cases": len(scores),
        "flaws": len(flaws),
        "detected": sum(flaws),
        "detection_rate": round(sum(flaws) / len(flaws), 3) if flaws else None,
        "detected_exact": sum(exact),
        "detection_rate_exact": round(sum(exact) / len(exact), 3) if exact else None,
        # flaws Sifu's build, SwiftLint and test steps catch, and those only a critic found
        "caught_by_checks": caught,
        "found_beyond_checks": beyond,
        "cases_failing_checks": sum(1 for s in verified if not s.checks_ok) if verified else None,
        "false_positives": sum(len(s.false_positives) for s in scores),
        "blocker_false_positives": sum(s.blocker_false_positives for s in scores),
        "clean_cases": len(clean),
        "clean_cases_with_findings": sum(1 for s in clean if s.false_positives),
        # Share of clean changes the critics let through without a blocker, i.e. no round trip.
        "clean_first_pass_rate": (
            round(sum(1 for s in clean if not s.blocker_false_positives) / len(clean), 3)
            if clean
            else None
        ),
    }
