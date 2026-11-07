"""The Plan step: read-only repo context, plan checks and plan.md."""

import subprocess

from sifu.config import PlannerConfig
from sifu.models import Plan, Spec
from sifu.planner import fallback_plan, outside_spec, render_plan_md, repo_context
from tests.fakes import FakeLLM


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "Sources").mkdir(parents=True)
    (repo / "Sources" / "Trip.swift").write_text("struct Trip { let name: String }\n")
    (repo / "Sources" / "Other.swift").write_text("struct Other {}\n")
    (repo / "secret.txt").write_text("do not show\n")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    return repo


def test_context_lists_files_and_excerpts_only_the_specs_paths(tmp_path):
    repo = make_repo(tmp_path)
    listing, excerpts = repo_context(repo, ["Sources/Trip.swift"], PlannerConfig())
    assert "secret.txt" in listing  # names only
    assert "struct Trip" in excerpts
    assert "struct Other" not in excerpts and "do not show" not in excerpts


def test_context_respects_the_caps(tmp_path):
    repo = make_repo(tmp_path)
    listing, excerpts = repo_context(
        repo, ["Sources/"], PlannerConfig(listing_cap=1, excerpt_chars=5, total_chars=100)
    )
    assert "more files" in listing
    assert all(len(block.split("\n", 1)[1]) <= 5 for block in excerpts.split("\n\n") if block)


def test_context_of_a_missing_repo_is_empty_not_an_error(tmp_path):
    assert repo_context(tmp_path / "nope", ["Sources/"], PlannerConfig()) == ("", "")


def test_outside_spec_compares_with_affected_paths_and_extra_allowed(cfg):
    spec = Spec(title="t", summary="s", affected_paths=["Sources/Trip.swift"])
    plan = Plan(
        summary="s",
        files_to_change=["Sources/Trip.swift", "Tests/TripTests.swift", "Sources/Other.swift"],
    )
    assert outside_spec(plan, spec, cfg) == ["Sources/Other.swift"]
    # With no affected paths in the spec there is nothing to compare against.
    assert outside_spec(plan, Spec(title="t", summary="s"), cfg) == []


def test_fallback_plan_is_marked_degraded_and_empty():
    plan = fallback_plan(Spec(title="t", summary="the summary"))
    assert plan.degraded and plan.files_to_change == [] and plan.summary == "the summary"


def test_plan_md_lists_every_section():
    text = render_plan_md(7, Plan(summary="Do it", steps=["a", "b"], files_to_change=["A.swift"]))
    assert "ticket: 7" in text and "1. a" in text and "2. b" in text
    assert "- A.swift" in text and "## Risks\n- none" in text


def test_fake_planner_reply_is_a_valid_plan():
    plan = Plan.model_validate_json(FakeLLM().replies["Planner"])
    assert plan.files_to_change and not plan.degraded
