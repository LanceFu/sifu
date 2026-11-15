"""The generated intent.md and spec.md keep the shape of docs/templates.

The renderers in ``sifu.synthesizer`` do not read the template files, so this test is what stops
the two from drifting apart: front-matter keys, headings, table headers and the fixed lines.
"""

import re
from pathlib import Path

from sifu.models import Finding, Spec
from sifu.synthesizer import render_intent_md, render_spec_md

TEMPLATES = Path(__file__).resolve().parents[1] / "docs" / "templates"


def _template(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def _front_matter_keys(text: str) -> list[str]:
    return re.findall(r"^(\w+):", text.split("---")[1], re.MULTILINE)


def _headings(text: str) -> list[str]:
    """Headings with the template's ``[placeholder]`` text blanked, so titles do not matter."""
    return [re.sub(r"\[.*?\]", "", h).strip() for h in re.findall(r"^#{1,3} .*$", text, re.M)]


def _table_headers(text: str) -> list[str]:
    """The header row of each table: the line just above its ``| :---- |`` separator."""
    lines = text.splitlines()
    return [prev for prev, line in zip(lines, lines[1:], strict=False) if line.startswith("| :-")]


def _spec_text() -> str:
    spec = Spec(
        title="Reject blank Trip names",
        summary="Saving a Trip with a blank name fails.",
        acceptance_criteria=["Given a blank name, When saving, Then nameBlank is thrown"],
        requirement_refs=["REQ-1"],
        affected_paths=["Sources/Trip.swift"],
        design=["Trim the name first."],
        assumptions=["Whitespace counts as blank."],
        out_of_scope=["Renaming existing Trips"],
    )
    note = Finding(reviewer="scope", category="c", severity="minor", message="Looks fine.")
    return render_spec_md(
        1, spec, [note], repo="tripplanner-ios", intent_text="x", today="2026-11-15"
    )


def test_intent_md_has_the_templates_front_matter_and_sections():
    template = _template("intent.md")
    text = render_intent_md(
        1, "Reject blank Trip names", "Given a, Then b (REQ-1)", repo="r", created="2026-11-15"
    )
    assert _front_matter_keys(text) == _front_matter_keys(template)
    assert _headings(text)[1:] == _headings(template)[1:]  # after the title line
    for fixed in re.findall(r"^<!--.*-->$", template, re.MULTILINE):
        assert fixed in text


def test_spec_md_has_the_templates_front_matter_sections_tables_and_fixed_lines():
    template, text = _template("spec.md"), _spec_text()
    assert _front_matter_keys(text) == _front_matter_keys(template)
    assert _headings(text)[1:] == _headings(template)[1:]
    assert _table_headers(text) == _table_headers(template)
    for fixed in (
        "Every row needs an automated test. Build, SwiftLint and tests must pass.",
        "Implements the intent in TP-0001; no behaviour is added beyond it.",
    ):
        assert fixed.replace("TP-0001", "TP-0000") in template and fixed in text
