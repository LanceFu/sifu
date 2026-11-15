"""Brainstorm chats: the model's turn, the stored log and the hand-off into a ticket."""

import json

import pytest

from sifu import brainstorm, db
from sifu.llm import LLMResult

BODY = (
    "Saving a Trip with a blank name succeeds today.\n"
    "Given a Traveller saves a Trip, When the name is blank, Then nameBlank is thrown.\nREQ-1"
)


def turn(reply="Which name counts as blank?", title="Reject blank Trip names", draft=BODY):
    return json.dumps({"reply": reply, "title": title, "draft": draft})


class Scripted:
    """Replies in order and remembers every prompt."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, *, model, system, user, schema=None):
        self.calls.append({"model": model, "system": system, "user": user})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return LLMResult(reply, 7, 3)


def said(*texts):
    return [{"role": "user", "content": text} for text in texts]


def test_a_turn_returns_the_reply_and_the_new_draft(ctx):
    llm = Scripted(turn())
    result, outcome = brainstorm.run_turn(llm, "m", ctx, "intent", said("Blank names save."))
    assert outcome.ok and result.reply == "Which name counts as blank?"
    assert (result.title, result.draft) == ("Reject blank Trip names", BODY)


def test_the_prompt_carries_the_persona_glossary_conversation_and_current_draft(ctx):
    llm = Scripted(turn())
    brainstorm.run_turn(
        llm, "m", ctx, "intent", said("Blank names save."), title="Old title", draft="Old text"
    )
    [call] = llm.calls
    assert "Intent Coach" in call["system"] and "Traveller" in call["system"]
    assert "USER: Blank names save." in call["user"]
    assert "Title: Old title\nOld text" in call["user"]


def test_the_spec_chat_uses_its_own_persona_and_sees_the_document(ctx):
    llm = Scripted(turn(draft=""))
    result, _ = brainstorm.run_turn(
        llm, "m", ctx, "spec", said("Why did it stop?"), draft="note", document="SPEC TEXT"
    )
    [call] = llm.calls
    assert "Spec Coach" in call["system"] and "<document>\nSPEC TEXT" in call["user"]
    assert result.draft == ""  # an empty draft is an answer here: nothing to change


def test_an_intent_turn_that_drops_the_draft_keeps_the_one_already_written(ctx):
    llm = Scripted(turn(title="", draft=" "))
    result, _ = brainstorm.run_turn(
        llm, "m", ctx, "intent", said("ok"), title="Kept title", draft="Kept text"
    )
    assert (result.title, result.draft) == ("Kept title", "Kept text")


def test_a_failed_model_call_keeps_the_draft_and_says_so(ctx):
    llm = Scripted(ConnectionError("ollama is down"))
    result, outcome = brainstorm.run_turn(
        llm, "m", ctx, "intent", said("hello"), title="T", draft="D"
    )
    assert not outcome.ok and "ollama is down" in result.reply
    assert (result.title, result.draft) == ("T", "D")


def test_an_unknown_stage_is_rejected(ctx):
    with pytest.raises(ValueError):
        brainstorm.build_system_prompt("merge", ctx)


def test_reply_in_database_stores_both_messages_the_draft_and_the_run(tmp_path, cfg, ctx):
    path = tmp_path / "chat.db"
    first = brainstorm.reply_in_database(
        path, "s1", "intent", "Blank names save.", cfg=cfg, llm=Scripted(turn()), ctx=ctx
    )
    llm = Scripted(turn(reply="Added."))
    brainstorm.reply_in_database(
        path, "s1", "intent", "Whitespace counts.", title=first.title, draft=first.draft,
        cfg=cfg, llm=llm, ctx=ctx,
    )  # fmt: skip
    conn = db.connect(path)
    rows = db.list_chat(conn, "s1")
    assert [(r["role"], r["content"]) for r in rows] == [
        ("user", "Blank names save."),
        ("assistant", "Which name counts as blank?"),
        ("user", "Whitespace counts."),
        ("assistant", "Added."),
    ]
    assert rows[1]["title"] == "Reject blank Trip names" and rows[1]["draft"] == BODY
    assert all(r["ticket_id"] is None and r["stage"] == "intent" for r in rows)
    # the second call was sent the whole conversation, and the model came from the config
    assert "USER: Blank names save." in llm.calls[0]["user"]
    assert "ASSISTANT: Which name counts as blank?" in llm.calls[0]["user"]
    assert llm.calls[0]["model"] == cfg.models.author
    runs = conn.execute("SELECT agent, model, ok, ticket_id FROM runs").fetchall()
    assert [tuple(r) for r in runs] == [("brainstorm:intent", cfg.models.author, 1, None)] * 2


def test_the_persons_message_is_kept_when_the_model_is_down(tmp_path, cfg, ctx):
    path = tmp_path / "chat.db"
    llm = Scripted(ConnectionError("down"))
    result = brainstorm.reply_in_database(
        path, "s1", "intent", "Blank names save.", cfg=cfg, llm=llm, ctx=ctx
    )
    rows = db.list_chat(db.connect(path), "s1")
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[0]["content"] == "Blank names save." and rows[1]["content"] == result.reply


def test_draft_gaps_lists_what_the_template_still_needs():
    assert brainstorm.draft_gaps("", "") == ["No title yet.", "No description yet."]
    assert brainstorm.draft_gaps("T", "Make it better.\n- OPEN: which screen?") == [
        "No Given / When / Then acceptance criterion.",
        "No requirement id (REQ-n).",
        "The draft still has OPEN: lines.",
    ]
    assert brainstorm.draft_gaps("Reject blank Trip names", BODY) == []


def test_transcript_names_each_speaker_and_marks_the_log_untrusted():
    text = brainstorm.transcript_md(
        [
            {"role": "user", "content": "Blank names save.", "created_at": "2026-11-14 10:00:00"},
            {"role": "assistant", "content": "Which name counts as blank?"},
        ],
        "Chat (intent): TP-0001",
    )
    assert text.startswith("# Chat (intent): TP-0001\n")
    assert "untrusted input" in text
    assert "## Person · 2026-11-14 10:00:00\n\nBlank names save." in text
    assert "## Sifu\n\nWhich name counts as blank?" in text
