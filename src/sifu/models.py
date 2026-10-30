"""Shared data models: ticket states, risk tiers and structured agent outputs."""

from __future__ import annotations

from enum import Enum, IntEnum
from typing import Literal

from pydantic import BaseModel, Field


class TicketStatus(str, Enum):
    """Pipeline stages plus the two side states."""

    INTENT = "intent"
    SPEC = "spec"
    BUILD = "build"
    TEST = "test"
    DONE = "done"
    NEEDS_HUMAN = "needs_human"
    FAILED = "failed"


class RiskTier(IntEnum):
    """Autonomy tiers. A higher value means more human involvement."""

    AUTO = 0
    AUTO_WITH_VETO = 1
    HUMAN = 2


class DecisionType(str, Enum):
    """Points in the pipeline where the risk router decides who approves."""

    ACCEPT_INTENT = "accept_intent"
    ACCEPT_SPEC = "accept_spec"
    ACCEPT_PLAN = "accept_plan"
    MERGE = "merge"


Severity = Literal["info", "minor", "major", "blocker"]


class Finding(BaseModel):
    """One structured finding from a reviewer agent."""

    reviewer: str
    category: str
    severity: Severity
    message: str
    evidence: str | None = None


class RiskAssessment(BaseModel):
    """An LLM assessor's opinion. It can raise the tier but never lower the floor."""

    assessor: str
    tier: RiskTier
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str


class DecisionContext(BaseModel):
    """Facts about a pending decision that the deterministic floor rules inspect."""

    decision_type: DecisionType
    touched_paths: list[str] = Field(default_factory=list)
    iteration: int = 0
    conflicts_with_requirements: bool = False
    ci_green: bool | None = None  # only meaningful for MERGE


class DecisionResult(BaseModel):
    """Outcome of the router: the final tier and why."""

    final_tier: RiskTier
    floor_tier: RiskTier
    rules_fired: list[str] = Field(default_factory=list)
    assessor_tiers: list[RiskTier] = Field(default_factory=list)


class LLMFinding(BaseModel):
    """A finding as a reviewer model writes it; the code adds the reviewer's name."""

    category: str
    severity: Severity
    message: str
    evidence: str | None = None


class ReviewerOutput(BaseModel):
    """The JSON a reviewer model must return."""

    findings: list[LLMFinding] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)


class ReviewerReport(BaseModel):
    """A reviewer's findings plus whether the deterministic fallback had to replace the model."""

    reviewer: str
    findings: list[Finding] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    degraded: bool = False


class Spec(BaseModel):
    """The accepted shape of spec.md."""

    title: str
    summary: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    requirement_refs: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)


class ClarifyingQuestion(BaseModel):
    """A question for the ticket author, raised when the Design stage cannot write a spec."""

    question: str
    source: str
    severity: Severity
