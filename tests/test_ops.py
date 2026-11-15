"""The app's writes, by database path: create, run, answer and continue a ticket."""

import subprocess
from pathlib import Path

import pytest

pytest.importorskip("langgraph")

from sifu import db, demo, live, ops, settings  # noqa: E402
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


def test_reopen_runs_a_failed_ticket_again(home, cfg):
    ticket = ops.create_ticket(home, "T", PROTECTED_BODY, cfg=cfg)
    ops.run(home, ticket, live=False, cfg=cfg, llm=FakeLLM())
    [request] = db.open_requests(db.connect(home))
    assert ops.answer(home, request["id"], "close", live=False, cfg=cfg, llm=FakeLLM()) == "failed"
    assert ops.reopen(home, ticket, live=False, cfg=cfg, llm=FakeLLM()) == "needs_human"


def test_resume_refuses_a_ticket_with_nothing_to_resume(home, cfg):
    ticket = ops.create_ticket(home, "T", BODY, cfg=cfg)
    ops.run(home, ticket, live=False, cfg=cfg, llm=FakeLLM())
    with pytest.raises(ValueError, match="nothing to resume"):
        ops.resume(home, ticket, live=False, cfg=cfg, llm=FakeLLM())


def test_runs_use_the_databases_saved_options(home, cfg):
    cfg.router.merge_mode = "risk_based"
    seed = db.connect(home)
    db.init_db(seed)
    seed.close()
    settings.save(home, "router.merge_mode", "manual")
    assert ops.effective_config(home, cfg).router.merge_mode == "manual"
    deps = ops.open_deps(home, live=False, cfg=cfg, llm=FakeLLM())
    assert deps.cfg.router.merge_mode == "manual" and deps.cfg.planner.enabled is False
    deps.conn.close()


def test_create_database_makes_an_initialised_file_with_the_project(tmp_path, cfg):
    path = ops.create_database("trial-2", data_dir=tmp_path, cfg=cfg)
    assert path == tmp_path / "trial-2.db"
    conn = db.connect(path)
    assert [r["name"] for r in conn.execute("SELECT name FROM projects")] == ["TripPlanner"]
    assert db.list_jobs(conn) == []  # every table exists


@pytest.mark.parametrize("name", ["", "Trial", "a/b", "../x", "a.db", "-a", "a" * 41, "a b"])
def test_create_database_refuses_a_bad_name(tmp_path, cfg, name):
    with pytest.raises(ValueError):
        ops.create_database(name, data_dir=tmp_path, cfg=cfg)
    assert list(tmp_path.iterdir()) == []


def test_create_database_never_overwrites(tmp_path, cfg):
    ops.create_database("trial", data_dir=tmp_path, cfg=cfg)
    before = (tmp_path / "trial.db").read_bytes()
    with pytest.raises(FileExistsError):
        ops.create_database("trial", data_dir=tmp_path, cfg=cfg)
    assert (tmp_path / "trial.db").read_bytes() == before


def test_safe_worktree_accepts_only_an_existing_folder_inside_the_worktrees_dir(tmp_path, cfg):
    cfg.workspace.worktrees_dir = str(tmp_path / "wt")
    inside = tmp_path / "wt" / "ticket-1"
    inside.mkdir(parents=True)
    assert ops.safe_worktree(str(inside), cfg) == inside.resolve()
    assert ops.safe_worktree(str(tmp_path / "wt" / "gone"), cfg) is None
    assert ops.safe_worktree(str(tmp_path), cfg) is None
    assert ops.safe_worktree(str(inside / ".." / ".." / "elsewhere"), cfg) is None
    assert ops.safe_worktree(None, cfg) is None
    assert ops.safe_worktree(str(tmp_path / "wt"), cfg) is None  # the folder itself is not one


def test_a_worktree_reached_through_a_symlink_out_of_the_folder_is_refused(tmp_path, cfg):
    cfg.workspace.worktrees_dir = str(tmp_path / "wt")
    (tmp_path / "wt").mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "wt" / "ticket-9").symlink_to(elsewhere)
    assert ops.safe_worktree(str(tmp_path / "wt" / "ticket-9"), cfg) is None


def test_worktree_diff_shows_changes_and_new_files_and_is_capped(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()

    def git(*a):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *a],
            cwd=repo,
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    (repo / "a.txt").write_text("one\n")
    git("add", ".")
    git("commit", "-qm", "first")
    (repo / "a.txt").write_text("two\n")
    (repo / "new.txt").write_text("x" * 50)
    diff = ops.worktree_diff(repo)
    assert "-one" in diff and "+two" in diff and "new.txt" in diff
    capped = ops.worktree_diff(repo, limit=40)
    assert len(capped) < 200 and capped.endswith("(diff cut at 40 characters)")


def test_send_lessons_needs_at_least_one_lesson(home, cfg):
    with pytest.raises(ValueError, match="no lessons to send"):
        ops.send_lessons(home, [{"lesson": "  ", "evidence": ""}], cfg=cfg)
    with pytest.raises(ValueError, match="no lessons to send"):
        ops.preview_lessons(home, [], cfg=cfg)
