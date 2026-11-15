"""Spec -> plan -> build through the real graph, with the planner switched on."""

import json

import pytest

pytest.importorskip("langgraph")

from sifu import db  # noqa: E402
from sifu.graph import is_paused, resume_ticket, run_ticket  # noqa: E402
from tests.fakes import FakeLLM  # noqa: E402

BODY = "Given a Traveller saves a Trip, When the name is blank, Then nameBlank is thrown."


def plan_reply(*files):
    return json.dumps(
        {
            "summary": "s",
            "steps": ["a"],
            "files_to_change": list(files),
            "tests_to_add": [],
            "risks": [],
        }
    )


def spec_reply(*paths):
    return json.dumps(
        {
            "title": "Reject blank names",
            "summary": "s",
            "acceptance_criteria": ["Given x, When y, Then z"],
            "requirement_refs": ["REQ-1"],
            "out_of_scope": [],
            "affected_paths": list(paths),
        }
    )


@pytest.fixture()
def planning(cfg, tmp_path):
    cfg.planner.enabled = True
    cfg.workspace.product_repo_path = str(tmp_path / "no-repo")
    return cfg


def decisions(conn, ticket):
    return [
        (r["decision_type"], r["rules_fired"], r["resolution"])
        for r in conn.execute("SELECT * FROM decisions WHERE ticket_id = ? ORDER BY id", (ticket,))
    ]


def test_an_in_scope_plan_is_accepted_by_code_and_build_runs(
    planning, conn, make_deps, make_ticket
):
    llm = FakeLLM(
        {"Spec Writer": spec_reply("Sources/"), "Planner": plan_reply("Sources/Trip.swift")}
    )
    deps = make_deps(llm)
    ticket = make_ticket("T", BODY)
    state = run_ticket(deps, ticket)
    assert state["status"] == "done" and state["facts"]["plan_accepted"] is True
    assert (deps.artifacts_dir / str(ticket) / "plan.md").exists()
    kinds = [e["kind"] for e in db.list_events(conn, ticket)]
    assert "plan_accepted" in kinds and kinds.index("plan_accepted") < kinds.index("stub_ran")
    assert [d[0] for d in decisions(conn, ticket)] == ["accept_spec", "accept_plan"]
    # The plan is judged by code: only the planner and spec-stage models were called.
    assert [c["key"] for c in llm.calls].count("Planner") == 1


def test_build_is_blocked_until_the_plan_is_accepted(planning, make_deps, make_ticket, conn):
    llm = FakeLLM({"Spec Writer": spec_reply("Sources/"), "Planner": plan_reply("Package.swift")})
    ticket = make_ticket("T", BODY)
    state = run_ticket(make_deps(llm), ticket)
    assert is_paused(state)
    assert db.get_ticket(conn, ticket)["status"] == "needs_human"
    assert "stub_ran" not in [e["kind"] for e in db.list_events(conn, ticket)]


def test_a_protected_file_in_the_plan_pauses_and_approval_resumes_build(
    planning, conn, make_deps, make_ticket
):
    llm = FakeLLM({"Spec Writer": spec_reply("Sources/"), "Planner": plan_reply("Package.swift")})
    deps = make_deps(llm)
    ticket = make_ticket("T", BODY)
    assert is_paused(run_ticket(deps, ticket))
    [request] = db.open_requests(conn)
    assert "plan" in request["question"]
    final = resume_ticket(deps, ticket, "approve")
    assert final["status"] == "done" and final["facts"]["plan_accepted"] is True
    rows = decisions(conn, ticket)
    assert rows[1][0] == "accept_plan" and "touches_protected_path" in rows[1][1]
    assert rows[1][2] == "human_approved"


def test_a_file_outside_the_spec_goes_to_a_human(planning, conn, make_deps, make_ticket):
    llm = FakeLLM(
        {
            "Spec Writer": spec_reply("Sources/Trip.swift"),
            "Planner": plan_reply("Sources/Other.swift"),
        }
    )
    ticket = make_ticket("T", BODY)
    assert is_paused(run_ticket(make_deps(llm), ticket))
    assert "plan_outside_spec" in decisions(conn, ticket)[1][1]


def test_an_unusable_plan_goes_to_a_human(planning, conn, make_deps, make_ticket):
    llm = FakeLLM({"Spec Writer": spec_reply("Sources/"), "Planner": "not json"})
    ticket = make_ticket("T", BODY)
    assert is_paused(run_ticket(make_deps(llm), ticket))
    assert "plan_unusable" in decisions(conn, ticket)[1][1]


def test_with_the_planner_off_nothing_changes(cfg, conn, make_deps, make_ticket):
    assert cfg.planner.enabled is False
    llm = FakeLLM()
    ticket = make_ticket("T", BODY)
    run_ticket(make_deps(llm), ticket)
    assert "Planner" not in [c["key"] for c in llm.calls]
    assert db.get_ticket(conn, ticket)["status"] == "spec"  # still waiting for a plan
