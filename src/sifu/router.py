"""Risk router. Code-enforced floor rules set the minimum tier; LLM assessors can only raise it."""

from __future__ import annotations

import posixpath

from sifu.config import RouterConfig
from sifu.models import (
    DecisionContext,
    DecisionResult,
    DecisionType,
    RiskAssessment,
    RiskTier,
)


def is_protected(path: str, protected_paths: list[str]) -> bool:
    """True if path is protected or escapes the repo.

    A trailing slash in a protected entry means a whole directory. Paths are normalized first,
    so ``./a/../.github/x`` is treated as ``.github/x``.
    """
    normalized = posixpath.normpath(path)
    if normalized.startswith("/") or normalized == ".." or normalized.startswith("../"):
        return True
    for entry in protected_paths:
        if entry.endswith("/"):
            if normalized.startswith(entry):
                return True
        elif normalized == entry:
            return True
    return False


def floor_tier(ctx: DecisionContext, cfg: RouterConfig) -> tuple[RiskTier, list[str]]:
    """Return the minimum tier for a decision and the names of the rules that set it."""
    rules: list[str] = []
    if any(is_protected(p, cfg.protected_paths) for p in ctx.touched_paths):
        rules.append("touches_protected_path")
    if ctx.iteration >= cfg.max_build_iterations:
        rules.append("loop_cap_reached")
    if ctx.conflicts_with_requirements:
        rules.append("conflicts_with_requirement")
    if ctx.decision_type == DecisionType.MERGE:
        if ctx.ci_green is not True:
            rules.append("ci_not_green")
        if cfg.merge_mode == "manual":
            rules.append("merge_mode_manual")
    return (RiskTier.HUMAN if rules else RiskTier.AUTO), rules


def decide(
    ctx: DecisionContext, assessments: list[RiskAssessment], cfg: RouterConfig
) -> DecisionResult:
    """Combine the floor with the assessors. The result is never below the floor."""
    floor, rules = floor_tier(ctx, cfg)
    tier = floor
    assessor_tiers = [a.tier for a in assessments]

    skip_assessors = ctx.decision_type == DecisionType.MERGE and cfg.merge_mode == "auto"
    if not skip_assessors:
        if not assessments:
            rules.append("missing_assessment")
            tier = max(tier, RiskTier.HUMAN)
        else:
            tier = max(tier, *assessor_tiers)
            if len(set(assessor_tiers)) > 1:
                rules.append("assessor_disagreement")
                tier = max(tier, RiskTier.AUTO_WITH_VETO)

    return DecisionResult(
        final_tier=tier,
        floor_tier=floor,
        rules_fired=rules,
        assessor_tiers=[] if skip_assessors else assessor_tiers,
    )
