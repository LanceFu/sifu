"""LLM risk assessors: two model families rate each decision, and the router only goes up."""

from __future__ import annotations

import json
from pathlib import Path

from sifu.context import ProductContext
from sifu.llm import LLM, StructuredOutcome, structured_call
from sifu.models import AssessorOutput, DecisionType, RiskAssessment, RiskTier
from sifu.reviewers import PROMPTS_DIR, UNTRUSTED_NOTE


def build_system_prompt(ctx: ProductContext, prompts_dir: Path = PROMPTS_DIR) -> str:
    folder = prompts_dir / "risk"
    fewshot = json.loads((folder / "fewshot.json").read_text(encoding="utf-8"))
    examples = "\n\n".join(
        f"Example {i}\nDecision input:\n{ex['ticket']}\nAnswer:\n{json.dumps(ex['output'])}"
        for i, ex in enumerate(fewshot, start=1)
    )
    parts = [
        (folder / "persona.md").read_text(encoding="utf-8").strip(),
        (folder / "rubric.md").read_text(encoding="utf-8").strip(),
        "## Glossary\n" + ctx.glossary_text(),
        "## Product requirements (PRD)\n" + ctx.prd_text,
    ]
    if ctx.conventions_text:
        parts.append("## Conventions\n" + ctx.conventions_text)
    parts.append("## Examples\n" + examples)
    return "\n\n".join(parts)


def unavailable(model: str, reason: str) -> RiskAssessment:
    """Deterministic fallback: an assessor that fails counts as a vote for a human."""
    return RiskAssessment(
        assessor=model,
        tier=RiskTier.HUMAN,
        confidence=0.0,
        rationale=f"Assessor unavailable, so a human decides. {reason}"[:300],
    )


def run_assessor(
    model: str,
    *,
    decision_type: DecisionType,
    title: str,
    spec_text: str,
    ctx: ProductContext,
    llm: LLM,
    prompts_dir: Path = PROMPTS_DIR,
) -> tuple[RiskAssessment, StructuredOutcome[AssessorOutput]]:
    """Ask one model for a tier. The ticket's spec is passed as untrusted data."""
    user = (
        f"{UNTRUSTED_NOTE}\n\nDecision: {decision_type.value}\n"
        f"<ticket>\nTitle: {title}\n{spec_text}\n</ticket>"
    )
    outcome = structured_call(
        llm,
        model=model,
        system=build_system_prompt(ctx, prompts_dir),
        user=user,
        schema_model=AssessorOutput,
        fallback=lambda: AssessorOutput(tier=RiskTier.HUMAN, confidence=0.0, rationale="fallback"),
    )
    if not outcome.ok:
        return unavailable(model, "; ".join(outcome.errors)), outcome
    out = outcome.value
    return (
        RiskAssessment(
            assessor=model, tier=out.tier, confidence=out.confidence, rationale=out.rationale
        ),
        outcome,
    )
