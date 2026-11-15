"""The app's writes, by database path: create, run, answer and continue a ticket."""

from pathlib import Path

import pytest

pytest.importorskip("langgraph")

from sifu import db, demo, live, ops  # noqa: E402
from tests.fakes import FakeLLM  # noqa: E402

BODY = "Given a Traveller saves a Trip, When the name is blank, Then nameBlank is thrown."
PROTECTED_BODY = BODY + " Also add a dependency in Package.swift."


@pytest.fixture()
def home(tmp_path, cfg, monkeypatch):
    """A product repo, a database path and artifacts under tmp_path. Returns the database path."""
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "glossary.yaml").write_text("terms: []\nout_of_scope: []\n")
    (repo / "docs" / "PRD.md").write_text("PRD")
    cfg.workspace.product_repo_path = str(repo)
    monkeypatch.setattr(demo, "ROOT", tmp_path)
    return str(tmp_path / "d.db")


def test_create_ticket_files_the_draft_and_links_its_chat_without_running_anything(home, cfg):
    seed = db.connect(home)
    db.init_db(seed)
    db.add_chat_message(seed, "s1", "intent", "user", "Blank names save.")
    seed.close()
    ticket = ops.create_ticket(home, " Reject blank Trip names ", BODY, "s1", cfg=cfg)
    check = db.connect(home)
    row = db.get_ticket(check, ticket)
    assert (row["title"], row["status"]) == ("Reject blank Trip names", "intent")
    assert [e["kind"] for e in db.list_events(check, ticket)] == ["ticket_created", "chat_saved"]
    assert {r["ticket_id"] for r in db.list_chat(check, "s1")} == {ticket}


def test_create_ticket_refuses_an_empty_draft(home, cfg):
    with pytest.raises(ValueError):
        ops.create_ticket(home, "Title", "  ", cfg=cfg)
    assert not Path(home).exists()


def test_run_in_design_mode_stops_at_the_plan_gate_even_with_the_planner_on(home, cfg):
    cfg.planner.enabled = True
    ticket = ops.create_ticket(home, "T", BODY, cfg=cfg)
    assert ops.run(home, ticket, live=False, cfg=cfg, llm=FakeLLM()) == "spec"
    kinds = [e["kind"] for e in db.list_events(db.connect(home), ticket)]
    assert "spec_accepted" in kinds and "stub_ran" not in kinds
    assert cfg.planner.enabled is True  # the caller's config is left alone


def test_run_in_live_mode_goes_through_build(home, cfg, monkeypatch):
    opened = []

    def fake_open_live(db_path, *, cfg=None, llm=None, **kw):
        deps, project = demo.open_demo(cfg, llm, str(db_path))  # stub Build and Test
        opened.append(str(db_path))
        return deps, project

    monkeypatch.setattr(live, "open_live", fake_open_live)
    cfg.planner.enabled = False
    ticket = ops.create_ticket(home, "T", BODY, cfg=cfg)
    assert ops.run(home, ticket, live=True, cfg=cfg, llm=FakeLLM()) == "done"
    assert opened == [home]


def test_answer_resumes_a_paused_ticket_in_either_mode(home, cfg):
    ticket = ops.create_ticket(home, "T", PROTECTED_BODY, cfg=cfg)
    assert ops.run(home, ticket, live=False, cfg=cfg, llm=FakeLLM()) == "needs_human"
    [request] = db.open_requests(db.connect(home))
    assert ops.answer(home, request["id"], "approve", live=False, cfg=cfg, llm=FakeLLM()) == "spec"


def test_continue_to_build_uses_the_live_wiring(home, cfg, monkeypatch):
    monkeypatch.setattr(
        live,
        "open_live",
        lambda db_path, *, cfg=None, llm=None, **kw: demo.open_demo(cfg, llm, str(db_path)),
    )
    cfg.planner.enabled = False
    ticket = ops.create_ticket(home, "T", BODY, cfg=cfg)
    ops.run(home, ticket, live=False, cfg=cfg, llm=FakeLLM())
    assert ops.continue_to_build(home, ticket, cfg=cfg, llm=FakeLLM()) == "done"


def test_an_exception_inside_a_run_reaches_the_caller_and_fails_the_ticket(home, cfg, monkeypatch):
    ticket = ops.create_ticket(home, "T", BODY, cfg=cfg)

    class Down:
        def complete(self, **kw):
            raise ConnectionError("ollama is down")

    # reviewers degrade on a model error, so make the failure unmistakable at the graph level
    from sifu import graph

    def boom(*a, **kw):
        raise ConnectionError("git is down")

    monkeypatch.setattr(graph, "build_graph", boom)
    with pytest.raises(ConnectionError):
        ops.run(home, ticket, live=False, cfg=cfg, llm=Down())
    assert db.get_ticket(db.connect(home), ticket)["status"] == "failed"
