"""Deterministic glossary checker. No model is involved, so the result is repeatable."""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from sifu.models import Finding


class GlossaryTerm(BaseModel):
    term: str
    definition: str
    avoid: list[str] = Field(default_factory=list)


class Glossary(BaseModel):
    terms: list[GlossaryTerm] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)


def load_glossary(path: str | Path) -> Glossary:
    """Read a product glossary YAML file."""
    with open(path, encoding="utf-8") as handle:
        return Glossary.model_validate(yaml.safe_load(handle) or {})


def _pattern(word: str, *, inflect: bool) -> re.Pattern[str]:
    suffix = r"(?:s|es)?" if inflect else ""
    return re.compile(rf"\b{re.escape(word)}{suffix}\b", re.IGNORECASE)


def check_text(glossary: Glossary, text: str) -> list[Finding]:
    """Flag avoided words (minor) and out-of-scope features (major) found in the text."""
    findings: list[Finding] = []
    for term in glossary.terms:
        for word in term.avoid:
            match = _pattern(word, inflect=True).search(text)
            if match:
                findings.append(
                    Finding(
                        reviewer="glossary",
                        category="glossary_violation",
                        severity="minor",
                        message=f"Use '{term.term}' instead of '{match.group(0)}'.",
                        evidence=_snippet(text, match),
                    )
                )
    for word in glossary.out_of_scope:
        match = _pattern(word, inflect=True).search(text)
        if match:
            findings.append(
                Finding(
                    reviewer="glossary",
                    category="out_of_scope",
                    severity="major",
                    message=f"'{word}' is out of scope for version 1.",
                    evidence=_snippet(text, match),
                )
            )
    return findings


def _snippet(text: str, match: re.Match[str], radius: int = 40) -> str:
    start, end = max(0, match.start() - radius), min(len(text), match.end() + radius)
    return " ".join(text[start:end].split())
