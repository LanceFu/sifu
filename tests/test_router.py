import itertools

import pytest

from sifu.config import RouterConfig
from sifu.models import DecisionContext, DecisionType, RiskAssessment, RiskTier
from sifu.router import decide, floor_tier, is_protected

PROTECTED = [".github/", "conventions.md", "Package.swift"]


def cfg(**kw) -> RouterConfig:
    return RouterConfig(protected_paths=PROTECTED, **kw)


def assessment(tier: RiskTier, name: str = "a") -> RiskAssessment:
    return RiskAssessment(assessor=name, tier=tier, confidence=0.9, rationale="test")


def ctx(**kw) -> DecisionContext:
    return DecisionContext(decision_type=kw.pop("decision_type", DecisionType.ACCEPT_SPEC), **kw)


def test_low_risk_decision_is_automatic():
    result = decide(ctx(), [assessment(RiskTier.AUTO), assessment(RiskTier.AUTO, "b")], cfg())
    assert result.final_tier == RiskTier.AUTO
    assert result.rules_fired == []


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        "conventions.md",
        "./Package.swift",
        "a/../.github/x",
        "/etc/passwd",
        "../x",
    ],
)
def test_protected_or_escaping_paths_force_human(path):
    assert is_protected(path, PROTECTED)
    tier, rules = floor_tier(ctx(touched_paths=[path]), cfg())
    assert tier == RiskTier.HUMAN
    assert rules == ["touches_protected_path"]


def test_ordinary_paths_are_not_protected():
    assert not is_protected("Sources/TripPlannerCore/Trip.swift", PROTECTED)
    assert not is_protected(".githubx/file", PROTECTED)


def test_loop_cap_and_requirement_conflict_force_human():
    assert floor_tier(ctx(iteration=3), cfg(max_build_iterations=3))[0] == RiskTier.HUMAN
    assert floor_tier(ctx(conflicts_with_requirements=True), cfg())[0] == RiskTier.HUMAN


def test_merge_needs_green_ci():
    merge = ctx(decision_type=DecisionType.MERGE, ci_green=False)
    assert floor_tier(merge, cfg())[1] == ["ci_not_green"]
    unknown = ctx(decision_type=DecisionType.MERGE, ci_green=None)
    assert floor_tier(unknown, cfg())[0] == RiskTier.HUMAN


def test_manual_merge_mode_always_needs_human():
    merge = ctx(decision_type=DecisionType.MERGE, ci_green=True)
    result = decide(merge, [assessment(RiskTier.AUTO)], cfg(merge_mode="manual"))
    assert result.final_tier == RiskTier.HUMAN
    assert "merge_mode_manual" in result.rules_fired


def test_auto_merge_mode_ignores_assessors_but_not_floors():
    clean = ctx(decision_type=DecisionType.MERGE, ci_green=True)
    assert (
        decide(clean, [assessment(RiskTier.HUMAN)], cfg(merge_mode="auto")).final_tier
        == RiskTier.AUTO
    )
    risky = ctx(decision_type=DecisionType.MERGE, ci_green=True, touched_paths=["Package.swift"])
    assert decide(risky, [], cfg(merge_mode="auto")).final_tier == RiskTier.HUMAN
    red = ctx(decision_type=DecisionType.MERGE, ci_green=False)
    assert decide(red, [], cfg(merge_mode="auto")).final_tier == RiskTier.HUMAN


def test_assessor_can_raise_the_tier():
    result = decide(ctx(), [assessment(RiskTier.HUMAN), assessment(RiskTier.AUTO, "b")], cfg())
    assert result.final_tier == RiskTier.HUMAN


def test_assessor_disagreement_adds_a_veto_window():
    result = decide(
        ctx(), [assessment(RiskTier.AUTO), assessment(RiskTier.AUTO_WITH_VETO, "b")], cfg()
    )
    assert result.final_tier == RiskTier.AUTO_WITH_VETO
    assert "assessor_disagreement" in result.rules_fired


def test_missing_assessment_fails_safe_to_human():
    result = decide(ctx(), [], cfg())
    assert result.final_tier == RiskTier.HUMAN
    assert "missing_assessment" in result.rules_fired


def test_llm_can_never_lower_the_floor():
    tiers = list(RiskTier)
    for protected, n_assessors in itertools.product([False, True], [1, 2]):
        for combo in itertools.product(tiers, repeat=n_assessors):
            context = ctx(touched_paths=["conventions.md"] if protected else [])
            result = decide(context, [assessment(t, str(i)) for i, t in enumerate(combo)], cfg())
            assert result.final_tier >= result.floor_tier
            if protected:
                assert result.final_tier == RiskTier.HUMAN
