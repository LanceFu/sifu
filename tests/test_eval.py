"""The Design-stage evaluation: golden tickets, scorer, the two runners and config overrides."""

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("langgraph")

from sifu.baseline import baseline_system_prompt  # noqa: E402
from sifu.context import ProductContext  # noqa: E402
from sifu.evalrun import (  # noqa: E402
    _line,  # noqa: E402
    check_labels,
    eval_config,
    load_config_with_overrides,
    run_baseline,
    run_swarm,
    write_results,
)
from sifu.evalset import (  # noqa: E402
    DEFECTS,
    GoldenTicket,
    format_markdown,
    load_tickets,
    score_record,
    summarize,
)
from sifu.llm import LLMResult  # noqa: E402
from tests.fakes import FakeLLM  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GWT = "Given a Traveller saves a Trip, When the name is blank, Then nameBlank is thrown (REQ-1)."


def ticket(key="t", defect="clean", expect="spec", route="auto", flag=None, by=""):
    return GoldenTicket(key, defect, "Title", GWT, expect, route, flag, by)


def record(key="t", outcome="spec", route="auto", flags=(), **extra):
    base = {
        "key": key,
        "system": "swarm",
        "repeat": 1,
        "outcome": outcome,
        "route": route,
        "flags": list(flags),
        "calls": 4,
        "calls_ok": 4,
        "first_try_calls": 3,
        "first_try_total": 3,
        "tokens": 100,
        "latency_ms": 2000,
    }
    return {**base, **extra}


# ---- the shipped golden tickets --------------------------------------------------------------


def test_the_shipped_tickets_load_and_cover_every_defect_type():
    tickets = load_tickets()
    assert 20 <= len(tickets) <= 25
    assert {t.defect for t in tickets} == set(DEFECTS)
    assert sum(t.defect == "clean" for t in tickets) >= 4  # enough to measure false positives


def test_no_golden_ticket_reuses_a_few_shot_or_seed_example():
    seen = " ".join(
        p.read_text(encoding="utf-8")
        for p in [*(ROOT / "src/sifu/prompts").glob("*/fewshot.json"), ROOT / "seed/tickets.yaml"]
    ).lower()
    for t in load_tickets():
        assert t.title.lower() not in seen, f"{t.key} repeats a prompt example"


def test_the_labels_agree_with_the_deterministic_code(cfg):
    repo = Path(os.environ.get("SIFU_PRODUCT_REPO", cfg.workspace.product_repo_path)).expanduser()
    if not (repo / "docs" / "PRD.md").exists():
        pytest.skip("the product repo is not on this machine")
    assert check_labels(load_tickets(), ProductContext.load(repo), eval_config(cfg)) == []


def test_loading_rejects_a_bad_label(tmp_path):
    bad = tmp_path / "t.yaml"
    bad.write_text(
        "tickets:\n"
        "- {key: a, defect: ambiguity, title: t, body: b, expect: questions, route: human}\n"
    )
    with pytest.raises(ValueError, match="needs a 'by' label"):
        load_tickets(bad)
    with pytest.raises(ValueError, match="unknown golden ticket keys"):
        load_tickets(only=["nope"])


# ---- scoring --------------------------------------------------------------------------------


def test_a_blocked_defect_is_caught_and_a_spec_for_it_is_a_miss():
    t = ticket(defect="ambiguity", expect="questions", route="human", by="models")
    assert score_record(t, record(outcome="questions", route="human")).detected is True
    missed = score_record(t, record(outcome="spec", route="auto"))
    assert missed.detected is False and missed.false_auto is True


def test_a_missed_defect_that_the_router_still_pauses_is_not_false_auto():
    t = ticket(defect="scope", expect="questions", route="human", by="models")
    score = score_record(t, record(outcome="spec", route="human"))
    assert score.detected is False and score.false_auto is False


def test_a_blocked_clean_ticket_is_a_false_positive_and_a_pause_is_over_escalation():
    t = ticket()
    assert score_record(t, record(outcome="questions", route="human")).false_positive is True
    spec_but_human = score_record(t, record(outcome="spec", route="human"))
    assert spec_but_human.over_escalation is True and spec_but_human.false_positive is False


def test_a_minor_flag_counts_only_when_raised_and_must_not_block():
    t = ticket(defect="glossary", flag="glossary_violation", by="checks")
    assert score_record(t, record(flags=["glossary_violation"])).detected is True
    assert score_record(t, record(flags=[])).detected is False
    assert score_record(t, record(outcome="questions", route="human")).false_positive is True


def test_a_protected_path_is_caught_only_by_a_human_route():
    t = ticket(defect="protected_path", route="human", by="floor")
    assert score_record(t, record(route="human")).detected is True
    assert score_record(t, record(route="auto")).detected is False


def test_a_crashed_run_counts_as_a_pause_never_as_a_pass():
    t = ticket(defect="injection", expect="questions", route="human", by="models")
    score = score_record(t, record(outcome="error", route="auto"))
    assert score.errored and score.detected is True and score.false_auto is False


def test_summary_pools_runs_and_reports_rates_by_defect_and_catcher():
    tickets = [
        ticket("c"),
        ticket("a", "ambiguity", "questions", "human", by="models"),
        ticket("i", "injection", "questions", "human", by="checks"),
    ]
    records = [
        record("c"),
        record("c", outcome="questions", route="human"),  # a repeat that blocks a clean ticket
        record("a", outcome="questions", route="human"),
        record("i", outcome="spec", route="auto"),  # a missed injection that went through
    ]
    summary, _ = summarize(tickets, records)
    assert summary["runs"] == 4 and summary["tickets"] == 3
    assert summary["detection_rate"] == 0.5
    assert summary["detection_by_defect"]["ambiguity"] == {"caught": 1, "total": 1, "rate": 1.0}
    assert summary["detection_by_catcher"]["checks"]["caught"] == 0
    assert summary["false_positive_rate"] == round(1 / 2, 3)  # 1 of 2 runs of the clean ticket
    assert summary["false_auto_rate"] == round(1 / 2, 3)  # the missed injection, of 2 human runs
    assert summary["over_escalation_rate"] == round(1 / 2, 3)
    assert summary["schema_valid_rate"] == 1.0 and summary["first_try_valid_rate"] == 1.0
    assert summary["tokens_per_ticket"] == 100 and summary["seconds_per_ticket"] == 2.0
    assert summary["stable_outcome_rate"] == 0.0  # the clean ticket changed between repeats


def test_markdown_table_has_a_column_per_system():
    tickets = [ticket("a", "ambiguity", "questions", "human", by="models")]
    one, _ = summarize(tickets, [record("a", outcome="questions", route="human")])
    table = format_markdown({"swarm": one, "baseline": one})
    assert table.splitlines()[0] == "| Metric | swarm | baseline |"
    assert "| Seeded defects caught | 100% | 100% |" in table
    assert "| Same outcome on every repeat | n/a | n/a |" in table


# ---- the runners ----------------------------------------------------------------------------


def test_swarm_runner_records_outcome_route_tokens_and_validity(ctx, cfg, tmp_path):
    blocking = json.dumps(
        {
            "findings": [{"category": "ambiguity", "severity": "major", "message": "vague"}],
            "questions": ["Which one?"],
        }
    )

    class Picky(FakeLLM):
        def complete(self, *, model, system, user, schema=None):
            if "Ambiguity" in system and "<ticket>\nTitle: Vague" in user:
                self.replies["Ambiguity"] = blocking
            else:
                self.replies["Ambiguity"] = json.dumps({"findings": [], "questions": []})
            return super().complete(model=model, system=system, user=user, schema=schema)

    tickets = [
        GoldenTicket("ok", "clean", "Fine", GWT, "spec", "auto"),
        GoldenTicket("vague", "ambiguity", "Vague", GWT, "questions", "human", None, "models"),
    ]
    lines: list[str] = []
    records = run_swarm(tickets, cfg, Picky(), ctx, artifacts_dir=tmp_path, progress=lines.append)

    ok, vague = records
    assert (ok["outcome"], ok["route"], ok["tier"]) == ("spec", "auto", 0)
    assert (vague["outcome"], vague["route"]) == ("questions", "human")
    assert "ambiguity" in vague["flags"] and "ambiguity" in vague["blocking_sources"]
    assert ok["calls"] == ok["calls_ok"] == 6 and ok["tokens"] == 6 * 15
    assert ok["first_try_total"] == 3 and ok["first_try_calls"] == 3
    [blocker] = [f for f in vague["findings"] if f["severity"] == "major"]
    assert blocker == {
        "source": "ambiguity",
        "category": "ambiguity",
        "severity": "major",
        "message": "vague",
    }
    assert all(r["error"] is None for r in records) and len(lines) == 2


def test_swarm_runner_turns_a_crash_into_an_error_record(ctx, cfg, tmp_path, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("budget used up")

    monkeypatch.setattr("sifu.evalrun.run_ticket", explode)
    [rec] = run_swarm(
        [ticket()], cfg, FakeLLM(), ctx, artifacts_dir=tmp_path, progress=lambda s: None
    )
    assert (
        rec["outcome"] == "error" and rec["route"] == "human" and "budget used up" in rec["error"]
    )


class OneReply:
    """Returns the same text for every call and counts them."""

    def __init__(self, text):
        self.text, self.calls = text, 0

    def complete(self, *, model, system, user, schema=None):
        self.calls += 1
        return LLMResult(self.text, 40, 10)


def test_baseline_runner_maps_the_verdict_and_issue_categories(ctx, cfg):
    reply = json.dumps(
        {
            "verdict": "questions",
            "route": "human",
            "rationale": "r",
            "issues": [{"category": "prompt_injection", "severity": "blocker", "message": "m"}],
        }
    )
    [rec] = run_baseline([ticket()], cfg, OneReply(reply), ctx, progress=lambda s: None)
    assert (rec["outcome"], rec["route"]) == ("questions", "human")
    assert rec["flags"] == ["prompt_injection"] and rec["blocking_sources"] == ["baseline"]
    assert rec["findings"] == [
        {
            "source": "baseline",
            "category": "prompt_injection",
            "severity": "blocker",
            "message": "m",
        }
    ]
    assert rec["tokens"] == 50 and rec["calls_ok"] == 1 and rec["first_try_calls"] == 1


def test_baseline_fails_safe_to_a_human_when_the_model_never_answers_validly(ctx, cfg):
    llm = OneReply("not json at all")
    [rec] = run_baseline([ticket()], cfg, llm, ctx, progress=lambda s: None)
    assert (rec["outcome"], rec["route"]) == ("questions", "human")
    assert rec["calls_ok"] == 0 and rec["first_try_calls"] == 0 and llm.calls == 3
    assert rec["error"]


def test_baseline_prompt_carries_the_same_context_as_the_swarm(ctx):
    prompt = baseline_system_prompt(ctx)
    assert "Traveller" in prompt and "REQ-1" in prompt and "Use the glossary." in prompt


# ---- results file and overrides -------------------------------------------------------------


def test_write_results_makes_one_file_with_a_summary_per_system(ctx, cfg, tmp_path):
    tickets = [ticket()]
    path, table = write_results(
        tmp_path,
        "unit",
        tickets,
        {"swarm": [record()], "baseline": [record(system="baseline")]},
        cfg,
    )
    data = json.loads(path.read_text())
    assert set(data["summaries"]) == {"swarm", "baseline"} and data["label"] == "unit"
    assert data["config"]["models"]["author"] == cfg.models.author
    assert "| Metric | swarm | baseline |" in table


def test_overrides_change_nested_config_values_and_reject_bad_ones():
    cfg = load_config_with_overrides(
        ROOT / "config.yaml",
        ["models.spec_reviewer=other:7b", "models.risk_assessors=[a, b]", "ollama.num_ctx=32768"],
    )
    assert cfg.models.spec_reviewer == "other:7b" and cfg.models.risk_assessors == ["a", "b"]
    assert cfg.ollama.num_ctx == 32768
    with pytest.raises(ValueError, match="override must look like"):
        load_config_with_overrides(ROOT / "config.yaml", ["models.author"])


def test_eval_config_turns_off_planner_and_critic_without_touching_the_original(cfg):
    cfg.planner.enabled = cfg.critic.enabled = True
    limited = eval_config(cfg)
    assert not limited.planner.enabled and not limited.critic.enabled
    assert cfg.planner.enabled and cfg.critic.enabled


def test_a_line_that_differs_from_its_labels_prints_the_blocking_findings():
    rec = record(
        "t",
        outcome="questions",
        route="human",
        system="swarm",
        findings=[
            {
                "source": "ambiguity",
                "category": "undefined_term",
                "severity": "major",
                "message": "What is a coverage report?",
            },
            {"source": "scope", "category": "x", "severity": "minor", "message": "ignored"},
        ],
        error=None,
    )
    text = _line(rec, ticket())  # expects spec/auto, so this is a DIFF
    assert text.startswith("DIFF") and "ambiguity major undefined_term: What is a coverage" in text
    assert "ignored" not in text
    assert _line(record(error=None), ticket()).startswith("ok")
