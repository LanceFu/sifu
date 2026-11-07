"""The golden-set loader and scorer, and the critic pass on a real git tree with a fake model."""

import json
import subprocess

import pytest

from sifu.critic import review_worktree
from sifu.golden import GOLDEN_DIR, load_cases, score_case, summarize
from sifu.verify import added_lines
from tests.fakes import FakeLLM

FILE = "Sources/TripPlannerCore/DayPlan.swift"
INDEX = {FILE: {22: "        if stops.count > 10 {", 25: '        print("Adding \\(x)")'}}


def finding(line, severity="blocker", critic="edge_cases"):
    return {"file": FILE, "line": line, "severity": severity, "critic": critic, "message": "m"}


def case(name):
    return next(c for c in load_cases(only=[name]))


def test_the_shipped_cases_load_and_their_patches_exist():
    cases = load_cases()
    assert {c.name for c in cases} >= {"demo1_day_plan_limit", "clean_stop_duration"}
    for c in cases:
        assert c.spec_path.is_file() and c.patch_path.is_file() and c.base
    assert [c.clean for c in cases if c.name.startswith("clean_")] == [True, True]
    assert GOLDEN_DIR.is_dir()


def test_a_flaw_is_detected_by_a_finding_on_a_line_holding_its_quote():
    score = score_case(
        case("demo1_day_plan_limit"), [finding(22), finding(25, "major", "security")], INDEX
    )
    assert score.detected == {"off_by_one": True, "coordinates_in_log": True, "extra_api": False}
    assert score.by_expected_critic["off_by_one"] and score.by_expected_critic["coordinates_in_log"]
    assert score.false_positives == []


def test_a_finding_that_matches_no_flaw_is_a_false_positive_and_blockers_are_counted():
    index = {FILE: {**INDEX[FILE], 40: "let a = 1"}}
    score = score_case(
        case("demo1_day_plan_limit"), [finding(22), finding(40), finding(40, "major")], index
    )
    assert len(score.false_positives) == 2 and score.blocker_false_positives == 1


def test_found_by_a_different_critic_is_noted_and_merged_critics_count():
    merged = finding(22, critic="contract+security")
    score = score_case(case("demo1_day_plan_limit"), [merged], INDEX)
    assert score.detected["off_by_one"] and not score.by_expected_critic["off_by_one"]
    merged = finding(22, critic="contract+edge_cases")
    assert score_case(case("demo1_day_plan_limit"), [merged], INDEX).by_expected_critic[
        "off_by_one"
    ]


def test_any_finding_on_a_clean_case_is_a_false_positive_and_the_summary_adds_up():
    clean = score_case(case("clean_day_plan_limit"), [finding(22)], INDEX)
    seeded = score_case(case("demo1_day_plan_limit"), [finding(22)], INDEX)
    total = summarize([clean, seeded])
    assert total["flaws"] == 3 and total["detected"] == 1 and total["detection_rate"] == 0.333
    assert total["false_positives"] == 1 and total["clean_cases_with_findings"] == 1


@pytest.mark.parametrize("name", ["demo1_day_plan_limit", "demo2_stop_duration"])
def test_every_seeded_flaw_quote_is_in_its_patch(name):
    c = case(name)
    patch = c.patch_path.read_text(encoding="utf-8")
    for flaw in c.expected:
        assert any(q in patch for q in flaw.quotes), flaw.id


def test_the_review_pipeline_runs_on_a_patched_tree(tmp_path):
    from sifu.config import CriticConfig

    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    (tmp_path / "A.swift").write_text("one\ntwo\n")
    git("add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
    (tmp_path / "A.swift").write_text("one\nif count > 10 {\nthree\n")
    reply = json.dumps(
        {
            "findings": [
                {
                    "severity": "blocker",
                    "category": "off_by_one",
                    "file": "A.swift",
                    "line": 1,  # one line off: the quote is on line 2
                    "quote": "count > 10",
                    "message": "off by one",
                    "scenario": "With 10 items the check still lets an eleventh one in.",
                }
            ]
        }
    )
    result = review_worktree(
        tmp_path,
        "spec",
        llm=FakeLLM({"Edge Case Critic": reply}),
        critic_cfg=CriticConfig(),
        critic_model="m",
        checker_model="c",
        added=added_lines(tmp_path),
    )
    assert [(f["file"], f["line"], f["severity"]) for f in result.merged] == [
        ("A.swift", 2, "blocker")
    ]
    assert result.degraded == [] and result.index["A.swift"][2] == "if count > 10 {"
