"""Test doubles."""

import json

from sifu.llm import LLMResult

CLEAN = json.dumps({"findings": [], "questions": []})
SPEC = json.dumps(
    {
        "title": "Reject blank Trip names",
        "summary": "Saving a Trip with a blank name fails.",
        "acceptance_criteria": ["Given a blank name, When saving, Then nameBlank is thrown"],
        "requirement_refs": ["REQ-1"],
        "out_of_scope": [],
    }
)


class FakeLLM:
    """Replies by reviewer persona. A value may be a string or a list used one reply at a time."""

    def __init__(self, replies: dict[str, str | list[str]] | None = None) -> None:
        self.replies = {"Ambiguity": CLEAN, "Scope": CLEAN, "Policy": CLEAN, "Spec Writer": SPEC}
        self.replies.update(replies or {})
        self.calls: list[dict] = []

    def complete(self, *, model, system, user, schema=None):
        key = next(k for k in self.replies if k in system)
        self.calls.append({"key": key, "model": model, "user": user, "schema": schema})
        reply = self.replies[key]
        if isinstance(reply, list):
            reply = reply.pop(0) if len(reply) > 1 else reply[0]
        return LLMResult(reply, 10, 5)
