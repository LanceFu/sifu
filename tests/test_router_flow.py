"""The router and human-in-the-loop flow through the real graph, with a scripted fake model."""

import json

import pytest

pytest.importorskip("langgraph")

from sifu import db  # noqa: E402
from sifu.graph import is_paused, resume_ticket, run_ticket, sqlite_checkpointer  # noqa: E402
from sifu.implementer import ImplementerResult  # noqa: E402
from tests.fakes import CLEAN, FakeLLM  # noqa: E402

BODY = "Given a Traveller saves a Trip, When the name is blank, Then nameBlank is thrown."
PROTECTED_BODY = BODY + " Also add a dependency in Package.swift."


def risk(tier, confidence=0.9):
    return json.dumps({"tier": tier, "confidence": confidence, "rationale": "r"})


def major(question="Which action should change?"):
    finding = {"category": "missing_acceptance_criteria", "severity": "major", "message": "m"}
    return json.dumps({"findings": [finding], "questions": [question]})


def decision(conn, ticket):
    return conn.execute(
        "SELECT * FROM decisions WHERE ticket_id = ? ORDER BY id", (ticket,)
    ).fetchall()


def status(conn, ticket):
    return db.get_ticket(conn, ticket)["status"]


@pytest.fixture()
def repo(tmp_path, cfg):
    path = tmp_path / "product"
    path.mkdir()
    (path / "conventions.md").write_text("# Conventions\n\n## Lessons\n")
    cfg.workspace.product_repo_path = str(path)
    return path


def test_low_risk_spec_is_auto_approved_and_logged(conn, make_ticket, make_deps, cfg):
    ticket = make_ticket("T", BODY)
    state = run_ticket(make_deps(FakeLLM()), ticket)
    assert not is_paused(state) and state["facts"]["spec_accepted"] is True
    [row] = decision(conn, ticket)
    assert (row["final_tier"], row["floor_tier"], row["resolution"]) == (0, 0, "auto")
    kinds = [e["kind"] for e in db.list_events(conn, ticket)]
    assert "tool_call" in kinds and "router_decision" in kinds
    models = {
        r["model"] for r in conn.execute("SELECT model FROM runs WHERE agent LIKE 'assessor:%'")
    }
    assert models == set(cfg.models.risk_assessors)


def test_floor_rule_beats_assessors_that_say_auto(conn, make_ticket, make_deps):
    ticket = make_ticket("Add dependency", PROTECTED_BODY)
    state = run_ticket(make_deps(FakeLLM({"Risk Assessor": risk(0)})), ticket)  # models say AUTO
    assert is_paused(state) and status(conn, ticket) == "needs_human"
    [row] = decision(conn, ticket)
    assert (row["floor_tier"], row["final_tier"], row["resolution"]) == (2, 2, "pending_human")
    assert "touches_protected_path" in row["rules_fired"]
    [request] = db.open_requests(conn)
    assert request["decision_id"] == row["id"] and "approve" in request["options"]


def test_assessor_can_raise_the_tier_to_human(conn, make_ticket, make_deps):
    ticket = make_ticket("T", BODY)
    state = run_ticket(make_deps(FakeLLM({"Risk Assessor": risk(2)})), ticket)
    assert is_paused(state) and decision(conn, ticket)[0]["floor_tier"] == 0


def test_assessor_disagreement_gives_auto_with_veto_and_continues(
    conn, make_ticket, make_deps, cfg
):
    first, second = cfg.models.risk_assessors[:2]
    replies = {first: risk(0), second: risk(1)}
    ticket = make_ticket("T", BODY)
    state = run_ticket(make_deps(FakeLLM({"Risk Assessor": replies})), ticket)
    assert not is_paused(state) and status(conn, ticket) == "spec"
    [row] = decision(conn, ticket)
    assert row["final_tier"] == 1 and "assessor_disagreement" in row["rules_fired"]
    assert row["resolution"] == "auto_with_veto"


def test_an_assessor_that_fails_counts_as_a_vote_for_a_human(conn, make_ticket, make_deps, cfg):
    first, second = cfg.models.risk_assessors[:2]
    ticket = make_ticket("T", BODY)
    state = run_ticket(
        make_deps(FakeLLM({"Risk Assessor": {first: risk(0), second: "garbage"}})), ticket
    )
    assert is_paused(state) and decision(conn, ticket)[0]["final_tier"] == 2


def test_pause_then_approve_resumes_and_marks_the_decision(conn, make_ticket, make_deps):
    deps = make_deps(FakeLLM())
    ticket = make_ticket("Add dependency", PROTECTED_BODY)
    paused = run_ticket(deps, ticket)
    assert is_paused(paused) and status(conn, ticket) == "needs_human"

    final = resume_ticket(deps, ticket, "approve")
    assert not is_paused(final) and final["facts"]["spec_accepted"] is True
    assert status(conn, ticket) == "spec"
    assert decision(conn, ticket)[0]["resolution"] == "human_approved"
    assert decision(conn, ticket)[0]["decided_by"] == "human"
    assert db.open_requests(conn) == []
    answered = conn.execute("SELECT status, answer FROM human_requests").fetchone()
    assert (answered["status"], answered["answer"]) == ("answered", "approve")


def test_resuming_without_a_paused_checkpoint_is_refused(conn, make_ticket, make_deps):
    deps = make_deps(FakeLLM())
    never_run = make_ticket("T", BODY)
    with pytest.raises(ValueError, match="no paused checkpoint"):
        resume_ticket(deps, never_run, "approve")
    assert status(conn, never_run) == "intent"

    finished = make_ticket("T", BODY)
    run_ticket(deps, finished)
    with pytest.raises(ValueError, match="no paused checkpoint"):
        resume_ticket(deps, finished, "approve")
    assert status(conn, finished) == "spec"


def test_rejection_with_feedback_loops_back_and_proposes_a_lesson(
    conn, make_ticket, make_deps, repo
):
    deps = make_deps(FakeLLM())
    ticket = make_ticket("Add dependency", PROTECTED_BODY)
    run_ticket(deps, ticket)
    again = resume_ticket(deps, ticket, "reject: do not touch the manifest")

    assert is_paused(again) and again["round"] == 1  # the Design stage ran again and paused again
    assert (
        "Clarification (round 1): reject: do not touch the manifest"
        in db.get_ticket(conn, ticket)["body"]
    )
    lessons = deps.proposed_conventions.read_text()
    assert f"(ticket {ticket})" in lessons and "do not touch the manifest" in lessons
    assert "do not touch" not in (repo / "conventions.md").read_text()  # approved copy untouched
    assert [r["resolution"] for r in decision(conn, ticket)] == ["human_rejected", "pending_human"]


def test_close_ends_the_ticket(conn, make_ticket, make_deps):
    deps = make_deps(FakeLLM())
    ticket = make_ticket("Add dependency", PROTECTED_BODY)
    run_ticket(deps, ticket)
    resume_ticket(deps, ticket, "close")
    assert status(conn, ticket) == "failed"
    assert decision(conn, ticket)[0]["resolution"] == "human_closed"


def test_answering_questions_reruns_the_design_stage(conn, make_ticket, make_deps):
    deps = make_deps(
        FakeLLM({"Ambiguity": [major(), CLEAN]})
    )  # vague first, clear after the answer
    ticket = make_ticket("Make stops better", BODY)
    paused = run_ticket(deps, ticket)
    assert is_paused(paused) and paused["outcome"] == "questions"
    assert status(conn, ticket) == "needs_human"

    final = resume_ticket(deps, ticket, "Reordering Stops within a Day Plan.")
    assert not is_paused(final) and final["outcome"] == "spec" and status(conn, ticket) == "spec"
    assert "ticket_clarified" in [e["kind"] for e in db.list_events(conn, ticket)]


def test_design_rounds_are_capped(conn, make_ticket, make_deps, cfg):
    cfg.router.max_design_rounds = 1
    deps = make_deps(FakeLLM({"Ambiguity": major()}))  # never gets clearer
    ticket = make_ticket("Vague", BODY)
    run_ticket(deps, ticket)
    assert is_paused(resume_ticket(deps, ticket, "first answer"))
    resume_ticket(deps, ticket, "second answer")
    assert status(conn, ticket) == "failed"
    assert "design_round_cap" in [e["kind"] for e in db.list_events(conn, ticket)]


def test_token_budget_stops_a_ticket(conn, make_ticket, make_deps, cfg):
    cfg.router.max_tokens_per_ticket = 20  # the fake reports 15 tokens per call
    ticket = make_ticket("T", BODY)
    with pytest.raises(RuntimeError, match="used"):
        run_ticket(make_deps(FakeLLM()), ticket)
    assert status(conn, ticket) == "failed"
    assert "budget_exceeded" in [e["kind"] for e in db.list_events(conn, ticket)]


def test_pause_survives_a_restart_with_the_sqlite_checkpointer(tmp_path, cfg, ctx, make_ticket):
    from sifu.graph import Deps as D

    path = str(tmp_path / "sifu.db")
    conn = db.connect(path)
    db.init_db(conn)
    project = db.create_project(conn, "TripPlanner", "/tmp/x", "o/r")
    ticket = db.create_ticket(conn, project, "Add dependency", PROTECTED_BODY)
    first = D(conn, FakeLLM(), cfg, ctx, tmp_path / "a", checkpointer=sqlite_checkpointer(path))
    assert is_paused(run_ticket(first, ticket))

    # A new process: new connections, new objects, the same database file.
    conn2 = db.connect(path)
    second = D(conn2, FakeLLM(), cfg, ctx, tmp_path / "a", checkpointer=sqlite_checkpointer(path))
    final = resume_ticket(second, ticket, "approve")
    assert final["facts"]["spec_accepted"] is True
    assert db.get_ticket(conn2, ticket)["status"] == "spec"


def implementer_returning(result):
    calls = []

    def hook(ticket_id, spec_text, conventions):
        calls.append((ticket_id, spec_text, conventions))
        return result

    hook.calls = calls
    return hook


def test_build_stage_calls_the_implementer_and_stores_its_output(conn, make_ticket, make_deps):
    deps = make_deps(FakeLLM())
    result = ImplementerResult(
        ok=True,
        result="done",
        changed_files=["Sources/Trip.swift"],
        num_turns=4,
        cost_usd=0.1,
        duration_ms=1200,
        branch="sifu/ticket-1",
    )
    deps.implementer = implementer_returning(result)
    ticket = make_ticket("T", BODY)
    state = run_ticket(deps, ticket, facts={"plan_accepted": True})
    assert state["status"] == "done"
    event = next(e for e in db.list_events(conn, ticket) if e["kind"] == "implementer_result")
    assert json.loads(event["payload"])["changed_files"] == ["Sources/Trip.swift"]
    assert deps.implementer.calls[0][0] == ticket and "Given" in deps.implementer.calls[0][1]
    assert conn.execute("SELECT ok FROM runs WHERE agent = 'implementer'").fetchone()["ok"] == 1


def test_failed_implementer_run_fails_the_ticket(conn, make_ticket, make_deps):
    deps = make_deps(FakeLLM())
    deps.implementer = implementer_returning(
        ImplementerResult(ok=False, error="timeout after 1800s")
    )
    ticket = make_ticket("T", BODY)
    run_ticket(deps, ticket, facts={"plan_accepted": True})
    assert status(conn, ticket) == "failed"


def test_demo_helpers_pause_and_resume_the_protected_ticket(tmp_path, cfg, monkeypatch, capsys):
    from sifu import demo

    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "glossary.yaml").write_text("terms: []\nout_of_scope: []\n")
    (repo / "docs" / "PRD.md").write_text("PRD")
    cfg.workspace.product_repo_path = str(repo)
    monkeypatch.setattr(demo, "ROOT", tmp_path)
    deps, project = demo.open_demo(cfg, FakeLLM(), str(tmp_path / "d.db"))
    assert deps.artifacts_dir == tmp_path / cfg.workspace.artifacts_dir / "d"
    ids = demo.run_router_demo(deps, project)
    assert "PAUSED for a human" in capsys.readouterr().out
    assert db.get_ticket(deps.conn, ids["clean"])["status"] == "spec"
    assert db.get_ticket(deps.conn, ids["protected_path"])["status"] == "needs_human"
    [request] = db.open_requests(deps.conn)
    demo.answer_request(deps, request["id"], "approve")
    assert db.get_ticket(deps.conn, ids["protected_path"])["status"] == "spec"
