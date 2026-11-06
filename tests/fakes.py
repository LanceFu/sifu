"""Test doubles."""

import json

from sifu.llm import LLMResult

CLEAN = json.dumps({"findings": [], "questions": []})
RISK_AUTO = json.dumps({"tier": 0, "confidence": 0.9, "rationale": "small and clear"})
SPEC = json.dumps(
    {
        "title": "Reject blank Trip names",
        "summary": "Saving a Trip with a blank name fails.",
        "acceptance_criteria": ["Given a blank name, When saving, Then nameBlank is thrown"],
        "requirement_refs": ["REQ-1"],
        "out_of_scope": [],
    }
)


CRITIC_CLEAN = json.dumps({"findings": []})

PLAN = json.dumps(
    {
        "summary": "Reject blank names when a Trip is saved.",
        "steps": ["Trim the name", "Throw nameBlank when empty", "Add tests"],
        "files_to_change": ["Sources/Trip.swift", "Tests/TripTests.swift"],
        "tests_to_add": ["testBlankNameIsRejected"],
        "risks": [],
    }
)


class FakeLLM:
    """Replies by reviewer persona. A value may be a string or a list used one reply at a time."""

    def __init__(self, replies: dict[str, str | list[str]] | None = None) -> None:
        # the critic personas are checked first: their examples mention TripPlannerCore
        self.replies = {
            "Edge Case Critic": CRITIC_CLEAN,
            "Contract Critic": CRITIC_CLEAN,
            "Security Critic": CRITIC_CLEAN,
            "Ambiguity": CLEAN,
            "Scope": CLEAN,
            "Policy": CLEAN,
            "Spec Writer": SPEC,
            "Risk Assessor": RISK_AUTO,
            "Planner": PLAN,
        }
        self.replies.update(replies or {})
        self.calls: list[dict] = []

    def complete(self, *, model, system, user, schema=None):
        key = next(k for k in self.replies if k in system)
        self.calls.append({"key": key, "model": model, "user": user, "schema": schema})
        reply = self.replies[key]
        if isinstance(reply, dict):  # a different reply per model tag
            reply = reply[model]
        if isinstance(reply, list):
            reply = reply.pop(0) if len(reply) > 1 else reply[0]
        return LLMResult(reply, 10, 5)
