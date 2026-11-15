"""intent.md and spec.md are rendered from docs/templates: the template is the structure.

Editing a template changes the generated files with no code change; the engine only fills
``{{placeholders}}`` and drops ``{# comments #}``.
"""

import re
import shutil
from pathlib import Path

import pytest

from sifu import templates
from sifu.models import Finding, Spec
from sifu.synthesizer import render_intent_md, render_spec_md

SHIPPED = Path(__file__).resolve().parents[1] / "docs" / "templates"


@pytest.fixture()
def folder(tmp_path):
    (tmp_path / "t.md").write_text("# {{title}}\n\n{# a note for authors #}\n{{body}}\n")
    return tmp_path


def test_a_template_is_filled_and_its_comments_are_dropped(folder):
    text = templates.render("t.md", {"title": "Hi", "body": "Text"}, folder)
    assert text == "# Hi\n\nText\n"


def test_a_placeholder_the_code_does_not_supply_is_an_error(folder):
    with pytest.raises(templates.TemplateError, match="body"):
        templates.render("t.md", {"title": "Hi"}, folder)


def test_values_are_not_expanded_again(folder):
    body = "{{title}} and {# not a comment #} stay as typed"
    assert body in templates.render("t.md", {"title": "Hi", "body": body}, folder)


def test_a_line_holding_only_an_empty_placeholder_is_dropped_and_trailing_space_is_trimmed(
    tmp_path,
):
    (tmp_path / "t.md").write_text("a: {{x}}\n{{rows}}\nz\n")
    assert templates.render("t.md", {"x": "", "rows": ""}, tmp_path) == "a:\nz\n"


def _spec_text(templates_dir=None):
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
        1,
        spec,
        [note],
        repo="tripplanner-ios",
        intent_text="x",
        today="2026-11-15",
        templates_dir=templates_dir,
    )


def test_editing_the_spec_template_changes_the_generated_spec_without_a_code_change(tmp_path):
    for name in ("intent.md", "spec.md"):
        shutil.copy(SHIPPED / name, tmp_path / name)
    edited = (tmp_path / "spec.md").read_text().replace("## 8. Out of scope", "## 8. Not doing")
    (tmp_path / "spec.md").write_text(edited)
    text = _spec_text(tmp_path)
    assert "## 8. Not doing" in text and "## 8. Out of scope" not in text
    assert "## 8. Out of scope" in _spec_text()  # the shipped template is unchanged


def test_editing_the_intent_template_changes_the_generated_intent(tmp_path):
    shutil.copy(SHIPPED / "intent.md", tmp_path / "intent.md")
    edited = (tmp_path / "intent.md").read_text().replace("## Ticket text", "## What is wanted")
    (tmp_path / "intent.md").write_text(edited)
    text = render_intent_md(1, "T", "Given a.", repo="r", templates_dir=tmp_path)
    assert "## What is wanted" in text


@pytest.mark.parametrize("name", ["intent.md", "spec.md"])
def test_the_shipped_templates_render_with_nothing_left_over(name):
    if name == "spec.md":
        text = _spec_text()
    else:
        text = render_intent_md(1, "T", "Given a, Then b (REQ-1)", repo="r", created="2026-11-15")
    assert "{{" not in text and "{#" not in text
    template = (SHIPPED / name).read_text()
    wanted = [
        re.sub(r"\{\{.*?\}\}", "", h).strip() for h in re.findall(r"^#{2,3} .*$", template, re.M)
    ]
    assert re.findall(r"^#{2,3} .*$", text, re.M) == wanted  # every section the template names
    assert text.startswith("---\n") and text.endswith("\n") and not text.endswith("\n\n")


def test_a_ticket_body_that_looks_like_a_placeholder_is_kept_verbatim():
    body = "Given {{id}} and {# x #}, Then fine."
    assert body in render_intent_md(5, "T", body, repo="r")


def test_the_fixed_text_of_the_shipped_templates_names_no_protected_path(cfg):
    """The router reads spec.md for protected paths, so fixed template text must not name one."""
    from sifu import checks

    for text in (_spec_text(), render_intent_md(1, "T", "Given a.", repo="r")):
        assert checks.protected_mentions(text, cfg.router.protected_paths) == []
