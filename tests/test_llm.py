import pytest
from pydantic import BaseModel

from sifu.llm import LLMResult, extract_json, structured_call


class Out(BaseModel):
    n: int


class Scripted:
    def __init__(self, replies):
        self.replies = list(replies)
        self.users = []

    def complete(self, *, model, system, user, schema=None):
        self.users.append(user)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return LLMResult(reply, 3, 2)


def run(llm, **kw):
    return structured_call(
        llm, model="m", system="s", user="u", schema_model=Out, fallback=lambda: Out(n=-1), **kw
    )


def test_extract_json_handles_fences_and_prose():
    assert extract_json('```json\n{"n": 1}\n```') == {"n": 1}
    assert extract_json('Sure! {"n": 2} Hope that helps.') == {"n": 2}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_valid_first_reply_needs_one_attempt():
    outcome = run(Scripted(['{"n": 5}']))
    assert (outcome.ok, outcome.attempts, outcome.value.n) == (True, 1, 5)
    assert outcome.prompt_tokens == 3


def test_invalid_reply_is_retried_with_the_error():
    llm = Scripted(["not json", '{"n": "x"}', '{"n": 7}'])
    outcome = run(llm)
    assert outcome.ok and outcome.attempts == 3 and outcome.value.n == 7
    assert "not valid" in llm.users[1]


def test_fallback_after_all_attempts_fail():
    outcome = run(Scripted(["bad", "bad", "bad"]))
    assert not outcome.ok and outcome.value.n == -1 and len(outcome.errors) == 3


def test_backend_error_stops_retrying_and_falls_back():
    llm = Scripted([ConnectionError("ollama down"), '{"n": 1}'])
    outcome = run(llm)
    assert not outcome.ok and outcome.value.n == -1
    assert len(llm.users) == 1 and "ollama down" in outcome.errors[0]


def test_ollama_caps_each_reply_with_num_predict_from_config(monkeypatch):
    # a reasoning model can loop until the context fills; the cap bounds one reply's cost
    import sys
    import types

    from sifu.config import OllamaConfig
    from sifu.llm import OllamaLLM

    seen = {}

    class FakeChat:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def invoke(self, messages):
            return types.SimpleNamespace(
                content='{"a": 1}', usage_metadata={"input_tokens": 5, "output_tokens": 7}
            )

    monkeypatch.setitem(sys.modules, "langchain_ollama", types.SimpleNamespace(ChatOllama=FakeChat))
    llm = OllamaLLM.from_config(OllamaConfig(num_ctx=4096, num_predict=512))
    result = llm.complete(model="m", system="s", user="u")
    assert seen["num_predict"] == 512 and seen["num_ctx"] == 4096
    assert (result.text, result.prompt_tokens, result.completion_tokens) == ('{"a": 1}', 5, 7)
    assert OllamaConfig().num_predict == 8192
