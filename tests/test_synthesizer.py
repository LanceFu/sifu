from sifu.models import Finding, ReviewerReport, Spec
from sifu.synthesizer import (
    build_questions,
    decide,
    fallback_spec,
    render_spec_md,
    write_artifact,
)


def finding(reviewer, severity, message="m"):
    return Finding(reviewer=reviewer, category="c", severity=severity, message=message)


def test_minor_findings_do_not_block():
    out = decide([ReviewerReport(reviewer="scope", findings=[finding("scope", "minor")])], [])
    assert out.kind == "spec" and len(out.notes) == 1


def test_major_or_blocker_forces_questions():
    for severity in ("major", "blocker"):
        report = ReviewerReport(reviewer="policy", findings=[finding("policy", severity)])
        assert decide([report], []).kind == "questions"


def test_glossary_out_of_scope_blocks():
    assert decide([], [finding("glossary", "major")]).kind == "questions"


def test_reviewer_questions_are_used_then_findings_fill_the_gaps():
    reports = [
        ReviewerReport(
            reviewer="ambiguity", findings=[finding("ambiguity", "major")], questions=["Q1", "Q1"]
        ),
        ReviewerReport(reviewer="scope", findings=[finding("scope", "major", "too big")]),
    ]
    outcome = decide(reports, [finding("glossary", "major", "Budget is out of scope")])
    texts = [q.question for q in build_questions(reports, outcome)]
    assert texts == ["Q1", "too big", "Budget is out of scope"]


def test_fallback_spec_takes_given_lines_and_requirement_ids():
    body = "Intro\n- Given a Trip, When saved, Then ok (REQ-1)\nGiven x, When y, Then z"
    spec = fallback_spec("T", body)
    assert len(spec.acceptance_criteria) == 2 and spec.requirement_refs == ["REQ-1"]
    assert fallback_spec("T", "no criteria").acceptance_criteria == []


def test_spec_markdown_lists_criteria_and_notes(tmp_path):
    spec = Spec(
        title="T",
        summary="S",
        acceptance_criteria=["Given a", "Given b"],
        requirement_refs=["REQ-2"],
    )
    text = render_spec_md(7, spec, [finding("glossary", "minor", "Use Traveller")])
    assert (
        "ticket: 7" in text and "1. Given a" in text and "[minor] glossary: Use Traveller" in text
    )
    assert write_artifact(tmp_path, 7, "spec.md", text).read_text() == text
