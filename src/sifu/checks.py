"""Deterministic ticket checks that run before and beside the model reviewers.

They exist because a model reviewer can miss a defect or be talked out of one. These cannot.
"""

from __future__ import annotations

import re

from sifu.models import Finding

_INJECTION = re.compile(
    r"ignore (?:all |any |your |the |previous |prior |above )+(?:instructions|rules)"
    r"|disregard (?:all |your |the |previous |prior )*(?:instructions|rules)"
    r"|system (?:note|prompt|message)"
    r"|approve (?:this|the) (?:ticket|change|pr)"
    r"|mark (?:this|every|all)[^.]{0,40}(?:low risk|info)"
    r"|as an ai\b",
    re.IGNORECASE,
)
_GIVEN = re.compile(r"\bgiven\b.*\bwhen\b.*\bthen\b", re.IGNORECASE | re.DOTALL)


def screen_injection(text: str) -> list[Finding]:
    """Flag phrases that address the reviewing agent instead of describing work."""
    match = _INJECTION.search(text)
    if not match:
        return []
    return [
        Finding(
            reviewer="checks",
            category="prompt_injection",
            severity="blocker",
            message="The ticket contains text aimed at the reviewing agent, not at a developer.",
            evidence=match.group(0),
        )
    ]


def check_acceptance_format(text: str) -> list[Finding]:
    """The PRD requires Given / When / Then acceptance criteria; a ticket without them is major."""
    if _GIVEN.search(text):
        return []
    return [
        Finding(
            reviewer="checks",
            category="missing_acceptance_criteria",
            severity="major",
            message="The ticket has no Given / When / Then acceptance criteria.",
        )
    ]


def run_checks(text: str) -> list[Finding]:
    return screen_injection(text) + check_acceptance_format(text)


def protected_mentions(text: str, protected_paths: list[str]) -> list[str]:
    """Protected paths the ticket text names. A match is a floor input, so no model can hide it."""
    found = []
    lowered = text.lower()
    for entry in protected_paths:
        needle = entry.rstrip("/").lower()
        if needle and needle in lowered:
            found.append(entry)
    return found
