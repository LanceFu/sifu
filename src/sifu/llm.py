"""Model access: a small interface, an Ollama implementation and structured output with retry.

Agents call ``structured_call``. It asks for JSON, validates it against a Pydantic model and
retries with the validation error. If every attempt fails it returns the caller's deterministic
fallback, so one bad model reply never crashes a ticket.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Generic, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


@dataclass
class LLMResult:
    """One model reply and its token counts, when the backend reports them."""

    text: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class LLM(Protocol):
    """Anything that can answer a system plus user prompt, optionally constrained to a schema."""

    def complete(
        self, *, model: str, system: str, user: str, schema: dict[str, Any] | None = None
    ) -> LLMResult: ...


class OllamaLLM:
    """Chat with a local Ollama model through langchain-ollama (imported lazily)."""

    def __init__(self, base_url: str, num_ctx: int, temperature: float = 0.0) -> None:
        self.base_url = base_url
        self.num_ctx = num_ctx
        self.temperature = temperature

    def complete(
        self, *, model: str, system: str, user: str, schema: dict[str, Any] | None = None
    ) -> LLMResult:
        from langchain_ollama import ChatOllama

        chat = ChatOllama(
            model=model,
            base_url=self.base_url,
            num_ctx=self.num_ctx,
            temperature=self.temperature,
            format=schema,
        )
        reply = chat.invoke([("system", system), ("user", user)])
        usage = getattr(reply, "usage_metadata", None) or {}
        text = reply.content if isinstance(reply.content, str) else str(reply.content)
        return LLMResult(text, usage.get("input_tokens"), usage.get("output_tokens"))


@dataclass
class StructuredOutcome(Generic[T]):
    """The validated value, or the fallback value with ``ok`` False, plus call statistics."""

    value: T
    ok: bool
    attempts: int
    errors: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    """Parse JSON from a reply, tolerating code fences and prose around the object."""
    candidate = text.strip()
    fenced = _FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(candidate[start : end + 1])


def structured_call(
    llm: LLM,
    *,
    model: str,
    system: str,
    user: str,
    schema_model: type[T],
    fallback: Callable[[], T],
    max_attempts: int = 3,
) -> StructuredOutcome[T]:
    """Call the model until it returns valid JSON for ``schema_model``, else use the fallback."""
    schema = schema_model.model_json_schema()
    errors: list[str] = []
    prompt_tokens = completion_tokens = 0
    started = time.monotonic()
    prompt = user
    for attempt in range(1, max_attempts + 1):
        try:
            reply = llm.complete(model=model, system=system, user=prompt, schema=schema)
            prompt_tokens += reply.prompt_tokens or 0
            completion_tokens += reply.completion_tokens or 0
            value = schema_model.model_validate(extract_json(reply.text))
        except (ValidationError, ValueError) as exc:  # JSONDecodeError is a ValueError
            errors.append(f"attempt {attempt}: {type(exc).__name__}: {str(exc)[:300]}")
            prompt = (
                f"{user}\n\nYour previous reply was not valid ({errors[-1]}). "
                "Reply again with only a JSON object that matches the schema."
            )
        except Exception as exc:  # noqa: BLE001 - backend down or timed out: stop retrying
            errors.append(f"attempt {attempt}: backend error {type(exc).__name__}: {exc}")
            break
        else:
            return StructuredOutcome(
                value,
                True,
                attempt,
                errors,
                prompt_tokens,
                completion_tokens,
                int((time.monotonic() - started) * 1000),
            )
    return StructuredOutcome(
        fallback(),
        False,
        len(errors),
        errors,
        prompt_tokens,
        completion_tokens,
        int((time.monotonic() - started) * 1000),
    )
