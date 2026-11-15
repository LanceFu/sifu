"""The notebook's results tables come from the results files alone."""

import json

from sifu.report import critic_table, design_table, latest, wrong_tickets


def summary(**over):
    base = {
        "runs": 3,
        "detection_rate": 1.0,
        "false_positive_rate": 0.5,
        "false_auto_rate": 0.0,
        "over_escalation_rate": 0.0,
        "outcome_accuracy": 0.667,
        "stable_outcome_rate": 1.0,
        "tokens_per_ticket": 1234.4,
        "seconds_per_ticket": 9.6,
    }
    return {**base, **over}


def score(key, ok, fp=False):
    return {
        "key": key,
        "outcome_ok": ok,
        "false_positive": fp,
        "over_escalation": False,
        "false_auto": False,
    }


RUN = {
    "summaries": {"swarm": summary(), "baseline": summary(detection_rate=0.5)},
    "scores": {
        "swarm": [score("a", True), score("b", False, fp=True), score("b", True)],
        "baseline": [score("a", True), score("b", True)],
    },
    "provenance": {"sifu": {"commit": "abcdef123", "dirty": False, "changed_during_run": True}},
}


def test_design_table_has_a_column_per_run_and_system_and_flags_a_changed_tree():
    table = design_table({"v1": {**RUN, "provenance": None}, "v2": RUN})
    assert table.splitlines()[0] == "| Metric | v1 swarm | v1 baseline | v2 swarm | v2 baseline |"
    assert "| Seeded defects caught | 100% | 50% | 100% | 50% |" in table
    assert "| Tokens per ticket | 1,234 | 1,234 | 1,234 | 1,234 |" in table
    assert "not stamped" in table
    only = design_table({"v2": RUN}, systems=["swarm"])
    assert (
        only.splitlines()[0] == "| Metric | v2 swarm |"
        and "`abcdef1` (changed during run)" in table
    )


def test_wrong_tickets_counts_wrong_repeats_per_system():
    table = wrong_tickets(RUN)
    assert "| swarm | b | 1 / 2 | false positive |" in table
    assert "baseline" not in table
    assert wrong_tickets({"scores": {"swarm": [score("a", True)]}}).startswith("No ticket")


def critics(caught=None, beyond=None):
    return {
        "summary": {
            "flaws": 2,
            "detected": 2,
            "detected_exact": 1,
            "false_positives": 0,
            "clean_cases": 1,
            "clean_first_pass_rate": 1.0,
            "caught_by_checks": caught,
            "found_beyond_checks": beyond,
        },
        "cases": [
            {"score": {"name": "x", "detected": {"f1": True, "f2": True}}, "tokens": 10},
            {"score": {"name": "y", "detected": {}}, "tokens": 5},
        ],
    }


def test_critic_table_combines_a_separate_checks_only_run():
    checks = {
        "summary": {"caught_by_checks": 1},
        "cases": [{"score": {"name": "x", "caught_by_checks": {"f1": True, "f2": False}}}],
    }
    table = critic_table({"main": critics(0, 2), "held": critics()}, {"held": checks})
    assert "| Caught by build, SwiftLint, tests | 0/2 | 1/2 |" in table
    assert "| Found only by the critics | 2/2 | 1/2 |" in table
    assert "| Seeded flaws found (exact line) | 1/2 | 1/2 |" in table
    assert "| Tokens, whole set | 15 | 15 |" in table


def test_latest_picks_the_newest_file_for_a_label(tmp_path):
    (tmp_path / "eval").mkdir()
    for name in [
        "design-v2-20261114-090000",
        "design-v2-20261114-100000",
        "design-v20-x",
        "design-v2-b-20261115-000000",
    ]:
        (tmp_path / "eval" / f"{name}.json").write_text(json.dumps({}))
    assert latest("eval", "v2", tmp_path).name == "design-v2-20261114-100000.json"
