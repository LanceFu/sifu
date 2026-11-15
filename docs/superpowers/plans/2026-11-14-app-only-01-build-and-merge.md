# Build and Merge from the App Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A ticket created in the Streamlit app runs through Build, Test, pull request and merge, and every plan or merge request is answered in the app, without blocking the page.

**Architecture:** The wiring for the real implementer, verifier and GitHub client moves from `scripts/live_run.py` into `sifu/live.py`. A new `sifu/ops.py` holds every write the app can ask for, keyed by database path. A new `sifu/jobs.py` runs those operations on one background thread and records each as a row in a `jobs` table, which the page polls. `graph.continue_ticket` continues a ticket that stopped at the plan gate.

**Tech Stack:** Python 3.10+, SQLite, LangGraph (SQLite checkpointer), Streamlit 1.66, pytest, ruff.

**Spec:** `docs/superpowers/plans/2026-11-14-app-only-00-overview.md` (gaps 1 to 4). Read its "Decisions already made" and "Rules for every task" sections first; they apply to every task here.

## Global Constraints

- One worker thread per Streamlit process; one job runs at a time; at most one queued or running job per ticket.
- `live` mode is used only when `live.missing_tools()` returns `[]`; otherwise `design` mode.
- The GitHub token is never stored in a job payload, a job error, an event or session state.
- `router.py` and `state.py` do not change in this plan.
- Looking at a tab never writes to the database, including a database that has no `jobs` table.
- `demo.answer_in_database` keeps its signature and behaviour (the README and tests use it).

## Review Focus

1. **A second click or a second browser tab submits the same answer while the first is running.** Expected: the second is refused with a clear message and the ticket runs once. Pinned in Task 1 (`JobConflict`) and Task 7.
2. **The app is restarted while a job is running.** Expected: the job shows as `interrupted`, not `running` forever. Pinned in Task 5.
3. **A job raises (Ollama down, git fails).** Expected: the job is `failed` with the error text, and the worker still runs the next job. Pinned in Task 5.
4. **Build tools are missing (no token, no `claude`).** Expected: plan and merge requests get no buttons, and nothing reaches a graph node that needs GitHub. Pinned in Task 7.
5. **A database recorded before this change has no `jobs` table.** Expected: every tab renders and the file's bytes do not change. Pinned in Tasks 5 and 6.

## File Structure

| File | Responsibility |
| --- | --- |
| `src/sifu/db.py` (modify) | `jobs` table and its functions; a 30 s busy timeout on connections |
| `src/sifu/live.py` (create) | Build `Deps` with the real implementer, verifier and GitHub client; say which tools are missing |
| `src/sifu/graph.py` (modify) | `continue_ticket`: continue a ticket from the plan gate |
| `src/sifu/ops.py` (create) | Every write the app can request, by database path: create, run, answer, continue |
| `src/sifu/jobs.py` (create) | Submit an operation, run it on the worker thread, record the outcome, recover after a restart |
| `src/sifu/ui_data.py` (modify) | Read jobs; tell whether a ticket waits at the plan gate |
| `src/sifu/ui.py` (modify) | Submit jobs instead of blocking; show job state; buttons for plan and merge requests |
| `src/sifu/demo.py` (modify, Task 7) | Remove `start_in_database` (replaced by `ops.create_ticket` + `ops.run`) |
| `scripts/live_run.py`, `scripts/live_probe.py` (modify) | Import `open_live` from `sifu.live` |

---

### Task 1: Jobs table and connection timeout

**Files:**
- Modify: `src/sifu/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces:
  - `class JobConflict(ValueError)`
  - `create_job(conn, kind: str, payload: Mapping[str, Any], ticket_id: int | None = None) -> int`
  - `start_job(conn, job_id: int) -> bool`
  - `finish_job(conn, job_id: int, *, result: str | None = None, error: str | None = None) -> None`
  - `cancel_job(conn, job_id: int) -> bool`
  - `interrupt_stale_jobs(conn) -> int`
  - `list_jobs(conn, active_only: bool = False) -> list[sqlite3.Row]`
  - Job statuses, exactly: `queued`, `running`, `done`, `failed`, `cancelled`, `interrupted`. "Active" means `queued` or `running`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_db.py`; it already has `conn` and `ticket` fixtures)

```python
def test_a_job_moves_from_queued_to_running_to_done(conn, ticket):
    job = db.create_job(conn, "run", {"ticket_id": ticket, "live": False}, ticket)
    [row] = db.list_jobs(conn)
    assert (row["status"], row["kind"], json.loads(row["payload"])["live"]) == (
        "queued",
        "run",
        False,
    )
    assert db.start_job(conn, job) is True and db.start_job(conn, job) is False
    assert db.list_jobs(conn)[0]["started_at"] is not None
    db.finish_job(conn, job, result="spec")
    [row] = db.list_jobs(conn)
    assert (row["status"], row["result"], row["error"]) == ("done", "spec", None)
    assert row["finished_at"] is not None and db.list_jobs(conn, active_only=True) == []


def test_a_job_that_ends_with_an_error_is_failed(conn, ticket):
    job = db.create_job(conn, "run", {}, ticket)
    db.start_job(conn, job)
    db.finish_job(conn, job, error="ConnectionError: ollama is down")
    assert tuple(db.list_jobs(conn)[0][k] for k in ("status", "error")) == (
        "failed",
        "ConnectionError: ollama is down",
    )


def test_a_ticket_has_at_most_one_active_job(conn, ticket):
    first = db.create_job(conn, "run", {}, ticket)
    with pytest.raises(db.JobConflict):
        db.create_job(conn, "answer", {}, ticket)
    db.start_job(conn, first)
    with pytest.raises(db.JobConflict):
        db.create_job(conn, "answer", {}, ticket)
    db.finish_job(conn, first, result="spec")
    assert db.create_job(conn, "answer", {}, ticket) > first  # allowed once the first ended


def test_jobs_without_a_ticket_do_not_conflict(conn):
    assert db.create_job(conn, "lessons", {}) != db.create_job(conn, "lessons", {})


def test_only_a_queued_job_can_be_cancelled(conn, ticket):
    job = db.create_job(conn, "run", {}, ticket)
    assert db.cancel_job(conn, job) is True and db.list_jobs(conn)[0]["status"] == "cancelled"
    assert db.start_job(conn, job) is False  # a cancelled job never starts
    other = db.create_job(conn, "run", {}, ticket)
    db.start_job(conn, other)
    assert db.cancel_job(conn, other) is False


def test_stale_jobs_are_marked_interrupted(conn, ticket):
    running = db.create_job(conn, "run", {}, ticket)
    db.start_job(conn, running)
    queued = db.create_job(conn, "lessons", {})
    assert db.interrupt_stale_jobs(conn) == 2
    assert {r["id"]: r["status"] for r in db.list_jobs(conn)} == {
        running: "interrupted",
        queued: "interrupted",
    }
    assert db.interrupt_stale_jobs(conn) == 0


def test_list_jobs_is_newest_first(conn, ticket):
    first = db.create_job(conn, "lessons", {})
    second = db.create_job(conn, "lessons", {})
    assert [r["id"] for r in db.list_jobs(conn)] == [second, first]
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `uv run pytest tests/test_db.py -q`
Expected: FAIL with `AttributeError: module 'sifu.db' has no attribute 'create_job'`.

- [ ] **Step 3: Add the table to `SCHEMA` in `src/sifu/db.py`**

```sql
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    ticket_id INTEGER REFERENCES tickets(id),
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued',
    result TEXT,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_active_per_ticket
    ON jobs (ticket_id) WHERE status IN ('queued', 'running');
```

- [ ] **Step 4: Implement the six functions and `JobConflict`**

- `create_job` inserts the row with `json.dumps(payload)`; catch `sqlite3.IntegrityError` and raise `JobConflict(f"ticket {ticket_id} already has a job queued or running")`.
- `start_job`: `UPDATE jobs SET status='running', started_at=datetime('now') WHERE id=? AND status='queued'`; return `cur.rowcount == 1`.
- `finish_job`: status is `failed` when `error` is not None, else `done`; sets `finished_at`.
- `cancel_job`: same shape as `start_job`, to `cancelled`, also sets `finished_at`.
- `interrupt_stale_jobs`: every `queued` or `running` row to `interrupted`, sets `finished_at`, returns the count.
- `list_jobs`: `ORDER BY id DESC`; `active_only` adds `WHERE status IN ('queued','running')`.

- [ ] **Step 5: Give connections a busy timeout**

In `db.connect`, change `sqlite3.connect(str(path), check_same_thread=False)` to `sqlite3.connect(str(path), check_same_thread=False, timeout=30)`. The worker thread and a chat message may write at the same moment; the default 5 s is too short for a write held during a model call.

- [ ] **Step 6: Update the module docstring's first line** to end "...decisions, requests, chats and jobs."

- [ ] **Step 7: Run all checks**

Run: `uv run --extra agents --extra ui pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/sifu/db.py tests/test_db.py
git commit -m "Jobs table: one queued or running job per ticket, with a 30 s busy timeout"
```

---

### Task 2: `sifu/live.py`, the real Build wiring

**Files:**
- Create: `src/sifu/live.py`
- Modify: `scripts/live_run.py`, `scripts/live_probe.py`
- Test: `tests/test_live.py`

**Interfaces:**
- Consumes: `demo.open_demo(cfg, llm, db_path) -> tuple[Deps, int]`, `credentials.get_github_token`, `credentials.SecretNotFound`, `implementer.Implementer(cfg, tag=)`, `verify.Verifier(cfg)`, `github.GitHubClient.from_config`, `pr_flow.publish_branch`.
- Produces:
  - `DEFAULT_DB: Path` (= `ROOT / "data" / "live.db"`)
  - `tag_for(db_path: str | Path) -> str | None`
  - `open_live(db_path: str | Path = DEFAULT_DB, *, cfg: Config | None = None, llm: LLM | None = None, merge_mode: str | None = None, planner: bool | None = None) -> tuple[Deps, int]`
  - `missing_tools(cfg: Config | None = None) -> list[str]`

- [ ] **Step 1: Write the failing tests** in `tests/test_live.py`

```python
"""The real Build wiring: which tools it needs and what it puts in Deps."""

import pytest

pytest.importorskip("langgraph")

from pydantic import SecretStr  # noqa: E402

from sifu import live  # noqa: E402
from sifu.credentials import SecretNotFound  # noqa: E402
from sifu.implementer import Implementer  # noqa: E402
from sifu.verify import Verifier  # noqa: E402
from tests.fakes import FakeLLM  # noqa: E402


@pytest.fixture()
def product(tmp_path, cfg):
    """A product repo folder with the files ProductContext.load needs, and a .git folder."""
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "docs" / "glossary.yaml").write_text("terms: []\nout_of_scope: []\n")
    (repo / "docs" / "PRD.md").write_text("PRD")
    cfg.workspace.product_repo_path = str(repo)
    return repo


@pytest.fixture()
def tools(monkeypatch):
    """Every tool present: a token, a GitHub client and `claude` on PATH."""
    monkeypatch.setattr(live, "get_github_token", lambda github_cfg: SecretStr("t0ken"))
    monkeypatch.setattr(live.GitHubClient, "from_config", classmethod(lambda cls, c: "github"))
    monkeypatch.setattr(live.shutil, "which", lambda name: f"/usr/bin/{name}")


def test_open_live_wires_the_implementer_verifier_and_github(tmp_path, cfg, product, tools):
    deps, project = live.open_live(tmp_path / "d.db", cfg=cfg, llm=FakeLLM())
    assert isinstance(deps.implementer, Implementer) and isinstance(deps.verifier, Verifier)
    assert deps.github == "github" and callable(deps.publisher) and project == 1
    assert deps.implementer.tag == "d"  # not the default database, so branches are tagged


def test_open_live_overrides_only_what_it_is_given(tmp_path, cfg, product, tools):
    cfg.router.merge_mode, cfg.planner.enabled = "risk_based", True
    deps, _ = live.open_live(tmp_path / "d.db", cfg=cfg, llm=FakeLLM())
    assert (deps.cfg.router.merge_mode, deps.cfg.planner.enabled) == ("risk_based", True)
    deps, _ = live.open_live(
        tmp_path / "e.db", cfg=cfg, llm=FakeLLM(), merge_mode="manual", planner=False
    )
    assert (deps.cfg.router.merge_mode, deps.cfg.planner.enabled) == ("manual", False)
    assert (cfg.router.merge_mode, cfg.planner.enabled) == ("risk_based", True)  # caller's copy


def test_the_default_database_is_not_tagged():
    assert live.tag_for(live.DEFAULT_DB) is None and live.tag_for("/x/data/design.db") == "design"


def test_missing_tools_is_empty_when_everything_is_there(cfg, product, tools):
    assert live.missing_tools(cfg) == []


def test_missing_tools_names_each_missing_tool_and_never_the_token(cfg, tmp_path, monkeypatch):
    cfg.workspace.product_repo_path = str(tmp_path / "nowhere")

    def no_token(github_cfg):
        raise SecretNotFound("No GitHub token found.")

    monkeypatch.setattr(live, "get_github_token", no_token)
    monkeypatch.setattr(live.shutil, "which", lambda name: None)
    missing = live.missing_tools(cfg)
    assert len(missing) == 3
    assert "claude" in missing[0] and "nowhere" in missing[1] and "token" in missing[2].lower()
```

- [ ] **Step 2: Run and confirm failure**

Run: `uv run --extra agents pytest tests/test_live.py -q`
Expected: FAIL, `ImportError: cannot import name 'live'`.

- [ ] **Step 3: Create `src/sifu/live.py`**

Move the body of `open_live` from `scripts/live_run.py` into it with the new signature. Rules the tests do not fully pin:
- Work on `cfg = (cfg or load_config(ROOT / "config.yaml")).model_copy(deep=True)`; apply `merge_mode` and `planner` only when not `None`.
- `ROOT = Path(__file__).resolve().parents[2]`.
- `tag_for` returns `None` when `Path(db_path).resolve() == DEFAULT_DB.resolve()`, else `Path(db_path).stem`. Pass the same tag to `Implementer(cfg, tag=tag)` **and** `Verifier(cfg, tag=tag)` (the script passed none to `Verifier`; the verifier looks the worktree up by the same name, so they must match).
- Import the names at module top (`import shutil`, `from sifu.credentials import SecretNotFound, get_github_token`, `from sifu.github import GitHubClient`) so the tests can patch `live.get_github_token`, `live.GitHubClient` and `live.shutil.which`.
- `missing_tools` returns messages in this order, each only when the tool is missing:
  1. `"Claude Code is not installed (no `claude` on PATH)"`
  2. `f"The product repo is not a git clone: {path}"` (test: `(path / ".git").exists()`)
  3. the `SecretNotFound` message text (it names the env var and Keychain item, never a token)

- [ ] **Step 4: Point the scripts at it**

In `scripts/live_run.py` delete `open_live` and its now-unused imports, add `from sifu.live import DEFAULT_DB, open_live`, set `DB = str(DEFAULT_DB)`, and call `open_live(args.db, merge_mode=args.merge_mode, planner=args.planner)`. In `scripts/live_probe.py` replace `from live_run import ROOT, open_live` with imports from `sifu.live` (keep `ROOT` from wherever it still resolves to the repo root) and change the call on the line that reads `open_live("manual", db_path=str(db_path))` to `open_live(str(db_path), merge_mode="manual")`.

- [ ] **Step 5: Run all checks**

Run: `uv run --extra agents --extra ui pytest -q && uv run ruff check . && uv run ruff format --check . && uv run --extra agents python scripts/live_run.py --help`
Expected: all pass; the help text prints.

- [ ] **Step 6: Commit**

```bash
git add src/sifu/live.py scripts/live_run.py scripts/live_probe.py tests/test_live.py
git commit -m "live.py: the real implementer, verifier and GitHub wiring, shared by the scripts and the app"
```

---

### Task 3: `graph.continue_ticket`

**Files:**
- Modify: `src/sifu/graph.py` (add after `resume_ticket`), `src/sifu/ui_data.py` (`KEY_KINDS`)
- Test: `tests/test_plan_flow.py`

**Interfaces:**
- Consumes: `build_graph`, `_thread`, `_invoke`, `Deps` (all in `graph.py`).
- Produces: `continue_ticket(deps: Deps, ticket_id: int, prompts_dir: Path = PROMPTS_DIR) -> dict[str, Any]`; a `build_requested` event (actor `human`, empty payload).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_plan_flow.py`; reuse its `BODY`, `FakeLLM` and its fake implementer; import `continue_ticket` from `sifu.graph`)

```python
def test_a_ticket_stopped_at_the_plan_gate_continues_into_build(cfg, conn, make_deps, make_ticket):
    deps = make_deps(FakeLLM())  # planner off, no implementer: Build and Test are stubs
    ticket = make_ticket("T", BODY)
    run_ticket(deps, ticket)
    assert db.get_ticket(conn, ticket)["status"] == "spec"
    state = continue_ticket(deps, ticket)
    assert state["status"] == "done" and db.get_ticket(conn, ticket)["status"] == "done"
    kinds = [e["kind"] for e in db.list_events(conn, ticket)]
    assert kinds.index("build_requested") < kinds.index("stub_ran")


def test_continuing_with_the_planner_on_writes_the_plan_first(cfg, conn, make_deps, make_ticket):
    deps = make_deps(FakeLLM())
    ticket = make_ticket("T", BODY)
    run_ticket(deps, ticket)
    cfg.planner.enabled = True
    cfg.workspace.product_repo_path = str(Path(__file__).resolve().parents[1])
    continue_ticket(deps, ticket)
    kinds = [e["kind"] for e in db.list_events(conn, ticket)]
    assert "plan_accepted" in kinds or "human_request_opened" in kinds  # the plan was routed


def test_only_a_ticket_waiting_at_the_plan_gate_can_be_continued(conn, make_deps, make_ticket):
    deps = make_deps(FakeLLM())
    never_run = make_ticket("T", BODY)
    with pytest.raises(ValueError, match="no accepted spec"):
        continue_ticket(deps, never_run)
    done = make_ticket("T2", BODY)
    run_ticket(deps, done, facts={"plan_accepted": True})
    with pytest.raises(ValueError, match="is done"):
        continue_ticket(deps, done)
    paused = make_ticket("T3", BODY + " Also add a dependency in Package.swift.")
    run_ticket(deps, paused)
    with pytest.raises(ValueError, match="waiting for an answer"):
        continue_ticket(deps, paused)
```

If `tests/test_plan_flow.py` already has a fixture that points `product_repo_path` at a usable folder for the planner (it has a `planning` fixture), use that fixture in the second test instead of setting the path by hand.

- [ ] **Step 2: Run and confirm failure**

Run: `uv run --extra agents pytest tests/test_plan_flow.py -q`
Expected: FAIL, `ImportError: cannot import name 'continue_ticket'`.

- [ ] **Step 3: Implement `continue_ticket`**

The algorithm (verified against LangGraph on 2026-11-14; do not substitute another approach):

```python
graph = build_graph(deps, prompts_dir)
snapshot = graph.get_state(_thread(ticket_id))
facts = dict(snapshot.values.get("facts") or {})
status = db.get_ticket(deps.conn, ticket_id)["status"]
if snapshot.interrupts:
    raise ValueError(f"ticket {ticket_id} is waiting for an answer")
if not facts.get("spec_accepted"):
    raise ValueError(f"ticket {ticket_id} has no accepted spec to build")
if snapshot.next or status != TicketStatus.SPEC.value:
    raise ValueError(f"ticket {ticket_id} is {status}; only a ticket waiting at spec can continue")
if not deps.cfg.planner.enabled:
    facts["plan_accepted"] = True  # no plan to accept, as scripts/live_run.py does up front
deps.log(ticket_id, "build_requested", "human", {})
graph.update_state(_thread(ticket_id), {"facts": facts}, as_node="accept")
return _invoke(deps, ticket_id, None, prompts_dir)
```

`as_node="accept"` makes LangGraph evaluate `after_accept`, which goes to `plan` when the planner is on and to `plan_gate` otherwise. Order the three checks exactly as above so the test's error messages match.

- [ ] **Step 4: Add `"build_requested"` to `KEY_KINDS` in `src/sifu/ui_data.py`.**

- [ ] **Step 5: Run all checks** (same command as Task 1 Step 7). Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/sifu/graph.py src/sifu/ui_data.py tests/test_plan_flow.py
git commit -m "continue_ticket: a ticket that stopped at the plan gate can go on to Build"
```

---

### Task 4: `sifu/ops.py`, the app's writes by database path

**Files:**
- Create: `src/sifu/ops.py`
- Test: `tests/test_ops.py`
- Do not touch `demo.start_in_database` or its tests here: `ui.py` still calls it until Task 7, which deletes it.

**Interfaces:**
- Consumes: `demo.open_demo`, `demo.archive_chat(conn, artifacts, ticket_id, session, stage, name=None)`, `demo.artifacts_dir(db_path, cfg=None)`, `demo.answer_request(deps, request_id, answer)`, `live.open_live`, `graph.run_ticket`, `graph.continue_ticket`.
- Produces (every `db_path` is `str | Path`; every function closes the connection it opens):
  - `open_deps(db_path, *, live: bool, cfg: Config | None = None, llm: LLM | None = None) -> Deps`
  - `create_ticket(db_path, title: str, body: str, session: str | None = None, *, cfg: Config | None = None) -> int`
  - `run(db_path, ticket_id: int, *, live: bool, cfg: Config | None = None, llm: LLM | None = None) -> str`
  - `answer(db_path, request_id: int, answer: str, *, live: bool, cfg: Config | None = None, llm: LLM | None = None) -> str`
  - `continue_to_build(db_path, ticket_id: int, *, cfg: Config | None = None, llm: LLM | None = None) -> str`
  - `run`, `answer` and `continue_to_build` return the ticket's status after the call.

- [ ] **Step 1: Write the failing tests** in `tests/test_ops.py`

The three `start_in_database` tests at the end of `tests/test_router_flow.py` show the setup to copy (a product repo folder under `tmp_path`, `demo.ROOT` patched, a seeded chat). Leave those tests where they are and write these:

```python
"""The app's writes, by database path: create, run, answer and continue a ticket."""


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


def test_answer_resumes_a_paused_ticket_in_either_mode(home, cfg, monkeypatch):
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


def test_a_failing_run_marks_the_ticket_failed_and_raises(home, cfg):
    ticket = ops.create_ticket(home, "T", BODY, cfg=cfg)

    class Down:
        def complete(self, **kw):
            raise ConnectionError("ollama is down")

    with pytest.raises(Exception):
        ops.run(home, ticket, live=False, cfg=cfg, llm=Down())
    # reviewers degrade on a model error, so the ticket may instead stop for a person
    assert db.get_ticket(db.connect(home), ticket)["status"] in ("failed", "needs_human")
```

If the last test does not raise because every model call degrades to a fallback (read `reviewers.unavailable_report` and `llm.structured_call`), replace `Down` with a fake `open_demo` that raises and assert the exception propagates from `ops.run`; the property to pin is "an exception inside the run reaches the caller".

Imports the file needs: `pytest`, `Path`, `db`, `demo`, `live`, `ops`, `FakeLLM`, and `BODY`/`PROTECTED_BODY` copied from `tests/test_router_flow.py`. Guard with `pytest.importorskip("langgraph")` as that file does.

- [ ] **Step 2: Run and confirm failure**

Run: `uv run --extra agents pytest tests/test_ops.py -q`
Expected: FAIL, `ImportError: cannot import name 'ops'`.

- [ ] **Step 3: Create `src/sifu/ops.py`**

- Module docstring: "Every write the app can ask for, by database path. `ui.py` and `jobs.py` call these; each opens its own connection and closes it."
- `open_deps`: `live=True` returns `live.open_live(db_path, cfg=cfg, llm=llm)[0]` (call it as `live.open_live`, through the module, so tests can patch it). `live=False`: deep-copy the config, set `planner.enabled = False`, return `demo.open_demo(cfg, llm, str(db_path))[0]`.
- `create_ticket`: raise `ValueError("a ticket needs a title and a description")` before touching the disk when either is blank after `strip()`. Then `db.connect`, `db.init_db`, `db.get_or_create_project(conn, "TripPlanner", <expanded product_repo_path>, cfg.github.repo)`, `db.create_ticket(conn, project, title.strip(), body.strip())`, and when `session` is given `demo.archive_chat(conn, demo.artifacts_dir(db_path, cfg), ticket_id, session, "intent")`.
- `run`: `deps = open_deps(...)`; `facts = {"plan_accepted": True} if live and not deps.cfg.planner.enabled else {}`; `run_ticket(deps, ticket_id, facts=facts)`; return the status. Close `deps.conn` in `finally`.
- `answer`: look up the request's `ticket_id`; `demo.answer_request(deps, request_id, answer)`; return the status.
- `continue_to_build`: `open_deps(live=True)`, `graph.continue_ticket`, return the status.

- [ ] **Step 4: Run all checks.** Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/sifu/ops.py tests/test_ops.py
git commit -m "ops.py: create, run, answer and continue a ticket by database path, in design or live mode"
```

---

### Task 5: `sifu/jobs.py`, the background runner

**Files:**
- Create: `src/sifu/jobs.py`
- Test: `tests/test_jobs.py`

**Interfaces:**
- Consumes: Task 1's `db` job functions; Task 4's `ops.run`, `ops.answer`, `ops.continue_to_build`.
- Produces:
  - `HANDLERS: dict[str, Callable[..., str]]` with keys exactly `"run"`, `"answer"`, `"continue"`
  - `submit(db_path: str | Path, kind: str, payload: Mapping[str, Any], ticket_id: int | None = None) -> int` (raises `db.JobConflict`, or `ValueError` for an unknown kind)
  - `run_job(db_path: str, job_id: int) -> None` (never raises)
  - `recover(db_path: str | Path) -> int`
  - `cancel(db_path: str | Path, job_id: int) -> bool`
  - Module attribute `_EXECUTOR` with a `submit(fn, *args)` method; tests replace it.
  - Payload shapes: `run` = `{"ticket_id": int, "live": bool}`; `answer` = `{"request_id": int, "answer": str, "live": bool}`; `continue` = `{"ticket_id": int}`. A handler is called as `handler(db_path, **payload)`.

- [ ] **Step 1: Write the failing tests** in `tests/test_jobs.py`

```python
"""Background jobs: submitted to one worker, recorded in the jobs table, recovered after a restart."""

import json

import pytest

from sifu import db, jobs


class Inline:
    """Runs a submitted job at once, on the calling thread."""

    def submit(self, fn, *args):
        fn(*args)


class Held:
    """Keeps submitted jobs until the test runs them."""

    def __init__(self):
        self.waiting = []

    def submit(self, fn, *args):
        self.waiting.append((fn, args))

    def run_all(self):
        while self.waiting:
            fn, args = self.waiting.pop(0)
            fn(*args)


@pytest.fixture()
def path(tmp_path):
    file = tmp_path / "j.db"
    conn = db.connect(file)
    db.init_db(conn)
    project = db.create_project(conn, "TripPlanner", "/repo", "o/r")
    db.create_ticket(conn, project, "First")
    db.create_ticket(conn, project, "Second")
    conn.close()
    return str(file)


def rows(path):
    conn = db.connect(path)
    try:
        return [dict(r) for r in db.list_jobs(conn)]
    finally:
        conn.close()


def test_submit_runs_the_handler_with_the_payload_and_records_the_result(path, monkeypatch):
    calls = []
    monkeypatch.setattr(jobs, "_EXECUTOR", Inline())
    monkeypatch.setitem(
        jobs.HANDLERS, "run", lambda db_path, **kw: calls.append((db_path, kw)) or "spec"
    )
    job = jobs.submit(path, "run", {"ticket_id": 1, "live": False}, ticket_id=1)
    assert calls == [(path, {"ticket_id": 1, "live": False})]
    [row] = rows(path)
    assert (row["id"], row["status"], row["result"], row["ticket_id"]) == (job, "done", "spec", 1)


def test_a_handler_that_raises_fails_the_job_and_the_next_job_still_runs(path, monkeypatch):
    monkeypatch.setattr(jobs, "_EXECUTOR", Inline())

    def boom(db_path, **kw):
        raise ConnectionError("ollama is down")

    monkeypatch.setitem(jobs.HANDLERS, "run", boom)
    monkeypatch.setitem(jobs.HANDLERS, "continue", lambda db_path, **kw: "done")
    jobs.submit(path, "run", {"ticket_id": 1, "live": False}, ticket_id=1)
    jobs.submit(path, "continue", {"ticket_id": 2}, ticket_id=2)
    second, first = rows(path)
    assert (first["status"], first["error"]) == ("failed", "ConnectionError: ollama is down")
    assert (second["status"], second["result"]) == ("done", "done")


def test_a_second_job_for_a_busy_ticket_is_refused_and_nothing_extra_runs(path, monkeypatch):
    held = Held()
    calls = []
    monkeypatch.setattr(jobs, "_EXECUTOR", held)
    monkeypatch.setitem(jobs.HANDLERS, "answer", lambda db_path, **kw: calls.append(kw) or "spec")
    payload = {"request_id": 1, "answer": "approve", "live": False}
    jobs.submit(path, "answer", payload, ticket_id=1)
    with pytest.raises(db.JobConflict):
        jobs.submit(path, "answer", payload, ticket_id=1)
    held.run_all()
    assert len(calls) == 1 and len(rows(path)) == 1


def test_a_cancelled_job_never_runs(path, monkeypatch):
    held = Held()
    calls = []
    monkeypatch.setattr(jobs, "_EXECUTOR", held)
    monkeypatch.setitem(jobs.HANDLERS, "run", lambda db_path, **kw: calls.append(kw) or "spec")
    job = jobs.submit(path, "run", {"ticket_id": 1, "live": False}, ticket_id=1)
    assert jobs.cancel(path, job) is True
    held.run_all()
    assert calls == [] and rows(path)[0]["status"] == "cancelled"


def test_an_unknown_kind_is_refused_before_anything_is_stored(path):
    with pytest.raises(ValueError):
        jobs.submit(path, "merge_everything", {})
    assert rows(path) == []


def test_recover_marks_jobs_left_by_a_dead_process_once_per_database(path, monkeypatch):
    monkeypatch.setattr(jobs, "_RECOVERED", set())
    conn = db.connect(path)
    stale = db.create_job(conn, "run", {}, 1)
    db.start_job(conn, stale)
    conn.close()
    assert jobs.recover(path) == 1 and rows(path)[0]["status"] == "interrupted"
    monkeypatch.setattr(jobs, "_EXECUTOR", Held())
    monkeypatch.setitem(jobs.HANDLERS, "run", lambda db_path, **kw: "spec")
    jobs.submit(path, "run", {"ticket_id": 1, "live": False}, ticket_id=1)
    assert jobs.recover(path) == 0  # this process's own queued job is left alone
    assert rows(path)[0]["status"] == "queued"


def test_recover_does_not_touch_a_database_without_a_jobs_table_or_without_stale_jobs(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(jobs, "_RECOVERED", set())
    old = tmp_path / "old.db"
    conn = db.connect(old)
    db.init_db(conn)
    conn.execute("DROP INDEX jobs_one_active_per_ticket")
    conn.execute("DROP TABLE jobs")
    conn.commit()
    conn.close()
    before = old.read_bytes()
    assert jobs.recover(old) == 0 and old.read_bytes() == before


def test_the_job_payload_is_stored_as_given(path, monkeypatch):
    monkeypatch.setattr(jobs, "_EXECUTOR", Held())
    jobs.submit(path, "answer", {"request_id": 3, "answer": "Trim it.", "live": True}, ticket_id=1)
    assert json.loads(rows(path)[0]["payload"]) == {
        "request_id": 3,
        "answer": "Trim it.",
        "live": True,
    }
```

- [ ] **Step 2: Run and confirm failure**

Run: `uv run pytest tests/test_jobs.py -q`
Expected: FAIL, `ImportError: cannot import name 'jobs'`.

- [ ] **Step 3: Create `src/sifu/jobs.py`**

- `_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sifu-job")`.
- `HANDLERS = {"run": ops.run, "answer": ops.answer, "continue": ops.continue_to_build}`. Importing `ops` imports the graph; that is fine, `jobs` is only imported where the `agents` extra is installed. Guard `tests/test_jobs.py` with `pytest.importorskip("langgraph")` if the import fails without the extra.
- `submit`: raise `ValueError(f"unknown job kind {kind!r}")` first; then open a connection, `db.init_db` (adds the table to an older database; a submit is a write, so this is allowed), `db.create_job`, close, and `_EXECUTOR.submit(run_job, str(db_path), job_id)`.
- `run_job`: open its own connection. If `db.start_job` returns False (cancelled) return. Call `HANDLERS[row["kind"]](db_path, **json.loads(row["payload"]))`. On success `finish_job(result=str(status))`. On any `Exception`: `finish_job(error=f"{type(exc).__name__}: {exc}"[:500])`. Always close the connection. Look the handler up at call time (`HANDLERS[kind]`), not at import, so tests can patch the dict.
- `recover`: keep a module set `_RECOVERED: set[str]` of resolved paths. If the path is already in it return 0; add it. Then, to honour "looking never writes": open the file **read-only** (`sifu.evidence.open_recorded`), run `SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')` and treat `sqlite3.OperationalError` (no table) as 0. Only when the count is above 0 open a normal connection and call `db.interrupt_stale_jobs`. Return the count.
- `submit` must add the resolved path to `_RECOVERED` **before** creating its row, after calling `recover(db_path)` itself, so a later `recover` never interrupts this process's own jobs.
- `cancel`: open, `db.cancel_job`, close.

- [ ] **Step 4: Run all checks.** Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/sifu/jobs.py tests/test_jobs.py
git commit -m "jobs.py: one background worker runs ticket operations and records each in the jobs table"
```

---

### Task 6: Read jobs and the plan-gate state in `ui_data`

**Files:**
- Modify: `src/sifu/ui_data.py`
- Test: `tests/test_ui_data.py`

**Interfaces:**
- Produces:
  - `jobs(conn, active_only: bool = False) -> list[dict[str, Any]]`, newest first; `[]` when the table is missing. Each dict has `id, ticket_id, kind, status, result, error, created_at, started_at, finished_at`.
  - `active_job(conn, ticket_id: int) -> dict[str, Any] | None`
  - `last_job(conn, ticket_id: int) -> dict[str, Any] | None` (the ticket's newest job of any status)
  - `awaiting_build(conn, ticket_id: int) -> bool`
  - Each `board()` card gains `"running": bool`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_ui_data.py`; it has `conn`, `project` and `decide`)

```python
def test_jobs_are_read_newest_first_and_the_active_one_is_found(conn, project):
    tid = db.create_ticket(conn, project, "Rename a Trip")
    first = db.create_job(conn, "run", {}, tid)
    db.start_job(conn, first)
    db.finish_job(conn, first, error="boom")
    second = db.create_job(conn, "answer", {}, tid)
    assert [j["id"] for j in ui_data.jobs(conn)] == [second, first]
    assert [j["id"] for j in ui_data.jobs(conn, active_only=True)] == [second]
    assert ui_data.active_job(conn, tid)["kind"] == "answer"
    db.cancel_job(conn, second)
    assert ui_data.active_job(conn, tid) is None
    assert ui_data.last_job(conn, tid)["status"] == "cancelled"


def test_job_queries_are_empty_on_a_database_recorded_before_jobs(conn, project):
    tid = db.create_ticket(conn, project, "Rename a Trip")
    conn.execute("DROP INDEX jobs_one_active_per_ticket")
    conn.execute("DROP TABLE jobs")
    assert ui_data.jobs(conn) == [] and ui_data.active_job(conn, tid) is None
    assert ui_data.last_job(conn, tid) is None
    assert ui_data.board(conn)["intent"][0]["running"] is False


def test_the_board_marks_a_ticket_with_an_active_job(conn, project):
    busy = db.create_ticket(conn, project, "Rename a Trip")
    idle = db.create_ticket(conn, project, "Remove a Stop")
    db.create_job(conn, "run", {}, busy)
    cards = {c["id"]: c["running"] for c in ui_data.board(conn)["intent"]}
    assert cards == {busy: True, idle: False}


def test_awaiting_build_is_a_spec_ticket_whose_last_step_was_the_plan_gate(conn, project):
    waiting = db.create_ticket(conn, project, "Rename a Trip")
    db.set_status(conn, waiting, TicketStatus.SPEC)
    db.append_event(conn, waiting, "awaiting_plan", "supervisor", {})
    assert ui_data.awaiting_build(conn, waiting) is True

    fresh = db.create_ticket(conn, project, "Remove a Stop")
    assert ui_data.awaiting_build(conn, fresh) is False  # still at intent

    built = db.create_ticket(conn, project, "Limit a Day Plan")
    db.set_status(conn, built, TicketStatus.SPEC)
    db.append_event(conn, built, "awaiting_plan", "supervisor", {})
    db.append_event(conn, built, "build_requested", "human", {})
    assert ui_data.awaiting_build(conn, built) is False  # already asked for

    asked = db.create_ticket(conn, project, "Reorder Stops")
    db.set_status(conn, asked, TicketStatus.SPEC)
    db.append_event(conn, asked, "awaiting_plan", "supervisor", {})
    db.open_human_request(conn, asked, "Approve?", ["approve", "close"])
    assert ui_data.awaiting_build(conn, asked) is False  # a request is open
```

- [ ] **Step 2: Run and confirm failure.** `uv run pytest tests/test_ui_data.py -q` fails with `AttributeError: ... 'jobs'`.

- [ ] **Step 3: Implement**

- Follow the pattern of `_chat_rows` in the same file: one private helper that runs the query and returns `[]` on `sqlite3.OperationalError`.
- `awaiting_build` is True when all hold: the ticket's status is `spec`; it has an `awaiting_plan` event; no event of kind `build_requested`, `implementer_result` or `stub_ran` has a higher id than the newest `awaiting_plan`; it has no open human request.
- In `board`, compute the set of ticket ids with an active job once and add `"running"` to each card.

- [ ] **Step 4: Run all checks.** Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/sifu/ui_data.py tests/test_ui_data.py
git commit -m "ui_data: jobs, the ticket's active job, and whether a ticket waits at the plan gate"
```

---

### Task 7: The app submits jobs and shows them

**Files:**
- Modify: `src/sifu/ui.py`, `src/sifu/demo.py` (delete `start_in_database`), `tests/test_router_flow.py` (delete its three `start_in_database` tests), `README.md`
- Test: `tests/test_ui.py`

**Interfaces:**
- Consumes: `ops.create_ticket`, `jobs.submit`, `jobs.recover`, `jobs.cancel`, `db.JobConflict`, `live.missing_tools`, `ui_data.jobs`, `ui_data.active_job`, `ui_data.last_job`, `ui_data.awaiting_build`, `demo.archive_chat_in_database`.
- Produces (widget keys other tasks and tests rely on): `live_recheck`, `ticket_continue`, `ticket_refresh`, `job-cancel-<job id>`; session-state key `live_missing` (a `list[str]`).

Behaviour to build, in the order the steps below test it:

1. **Mode.** `_missing() -> list[str]`: return `st.session_state["live_missing"]`, computing it with `live.missing_tools()` the first time in a session. The sidebar shows `"Build and merge: ready"` when the list is empty, otherwise `"Design only. Missing:"` followed by one caption per entry, and a button "Check again" (`key="live_recheck"`) whose callback pops `live_missing`. `live = not _missing()`.
2. **Create.** `_start` calls `ops.create_ticket(db_path, title, body, session)` on the page thread (fast, no model), then `jobs.submit(db_path, "run", {"ticket_id": ticket_id, "live": live}, ticket_id=ticket_id)`. Notice text: `f"Ticket #{ticket_id} created; the Design stage is running."`. The rest of `_start` (clearing the draft, `goto` the Ticket tab) is unchanged. Remove the spinner.
3. **Answer.** `_answer(db_path, item, answer, chat=None)` keeps the chat archive step, then calls `jobs.submit(db_path, "answer", {"request_id": item["id"], "answer": answer, "live": live}, ticket_id=item["ticket_id"])`. On `db.JobConflict` show `st.warning(f"Ticket #{item['ticket_id']} is already running.")` and return. Otherwise set the notice `f"Ticket #{item['ticket_id']}: your answer was sent."` and `st.rerun()`. Remove the spinner and the call to `demo.answer_in_database`.
4. **Inbox.** For an item with `needs_live_tools`:
   - when `live` is False, keep today's block (the info text and the `scripts/live_run.py` command) and add one caption per missing tool;
   - when `live` is True, show the same answer buttons as a Design request. Show the free-text reply (text input and "Send reply") only when `item["stage"] != "merge"`, because the graph closes a ticket on any merge answer that is not `approve`.
   For every item, when `ui_data.active_job(conn, item["ticket_id"])` is not None, show `st.caption("Running…")` instead of any buttons.
5. **Ticket tab.** Under the status line:
   - active job: `st.info(f"Running: {job['kind']} (job {job['id']}, {job['status']})")`, a "Refresh" button (`key="ticket_refresh"`, does nothing but rerun), and when the job is `queued` a "Cancel" button (`key=f"job-cancel-{job['id']}"`) that calls `jobs.cancel` and reruns;
   - else if `ui_data.last_job` is `failed`: `st.error(f"The last run failed: {job['error']}")`; if `interrupted`: `st.warning("The last run was cut off when the app stopped.")`;
   - else if `ui_data.awaiting_build(conn, ticket_id)`: when `live`, a primary button "Continue to Build" (`key="ticket_continue"`) that submits `("continue", {"ticket_id": ticket_id})`; when not, `st.caption("This ticket's spec is accepted. Build needs: " + "; ".join(_missing()))`.
   The spec chat panel (`show_spec_chat`) is not shown while the ticket has an active job.
6. **Board.** A card with `running` shows `st.caption("Running…")`.
7. **Polling.** A function decorated `@st.fragment(run_every=3)` named `_watch_jobs(db_path: Path)` rendered inside the sidebar. It opens its own read-only connection (`open_recorded`), reads `ui_data.jobs(conn, active_only=True)`, shows `st.caption(f"{n} job{'s' if n != 1 else ''} running")` when `n > 0`, and compares `tuple((j["id"], j["status"]) for j in active)` with `st.session_state.get("jobs_seen")`. When the key is missing it stores the value; when the value differs it stores it and calls `st.rerun()` (full app). It must not rerun when nothing changed: a rerun every 3 s would disturb typing.
8. **Recovery.** In `main`, right after the database is chosen, call `jobs.recover(db_path)`.

- [ ] **Step 1: Update the existing tests that the new behaviour breaks**

In `tests/test_ui.py`:
- Add an autouse fixture so no test starts a real job or looks for real tools:

```python
@pytest.fixture(autouse=True)
def no_real_work(monkeypatch):
    """No test starts a worker thread or looks for claude, a token or the product repo."""
    submitted = []
    monkeypatch.setattr(
        jobs,
        "submit",
        lambda path, kind, payload, ticket_id=None: (
            submitted.append((kind, payload, ticket_id)) or len(submitted)
        ),
    )
    monkeypatch.setattr(jobs, "recover", lambda path: 0)
    monkeypatch.setattr(live, "missing_tools", lambda cfg=None: ["No GitHub token found."])
    return submitted
```

- `test_clicking_an_option_answers_the_request`: drop the `demo.answer_in_database` patch; assert `no_real_work == [("answer", {"request_id": 1, "answer": "approve", "live": False}, 1)]`.
- `test_start_the_workflow_files_the_reviewed_draft_and_opens_the_ticket`: patch `ops.create_ticket` (signature `(path, title, body, session)`, returns `3` after really creating the ticket as the test's `start` does now); assert `no_real_work == [("run", {"ticket_id": 3, "live": False}, 3)]` and the success text is `"Ticket #3 created; the Design stage is running."`.
- `test_sending_the_reply_saves_the_spec_chat_then_answers_the_request` and `test_approving_without_a_chat_answers_and_archives_nothing`: replace the `demo.answer_in_database` patch with the `no_real_work` list; the archive must still come first (record both into one list by patching `jobs.submit` again inside the test).
- `test_inbox_sends_a_merge_request_to_the_command_line`: unchanged in result (tools are missing by default); also assert `"No GitHub token found." in text(at)`.

- [ ] **Step 2: Add the new failing tests**

```python
def ready(monkeypatch):
    monkeypatch.setattr(live, "missing_tools", lambda cfg=None: [])


def test_the_sidebar_says_whether_build_is_available(database, monkeypatch):
    assert "Design only. Missing:" in text(run("Board"))
    ready(monkeypatch)
    assert "Build and merge: ready" in text(run("Board"))


def test_with_the_tools_present_a_merge_request_gets_buttons_but_no_free_text(
    database, monkeypatch, no_real_work
):
    ready(monkeypatch)
    at = run("Inbox")
    assert [b.label for b in at.button if b.key and b.key.startswith("answer-2-")] == [
        "approve",
        "close",
    ]
    assert "scripts/live_run.py" not in text(at)
    assert not [w for w in at.text_input if w.key == "reply-2"]
    next(b for b in at.button if b.key == "answer-2-approve").click().run()
    assert no_real_work == [("answer", {"request_id": 2, "answer": "approve", "live": True}, 2)]


def test_a_request_on_a_running_ticket_has_no_buttons(database):
    conn = db.connect(database)
    db.create_job(conn, "answer", {}, 1)
    conn.close()
    at = run("Inbox")
    assert not [b for b in at.button if b.key and b.key.startswith("answer-1-")]
    assert "Running…" in text(at)


def test_a_refused_second_answer_warns_and_submits_nothing(database, monkeypatch):
    def busy(path, kind, payload, ticket_id=None):
        raise db.JobConflict("ticket 1 already has a job queued or running")

    monkeypatch.setattr(jobs, "submit", busy)
    at = run("Inbox")
    next(b for b in at.button if b.key == "answer-1-approve").click().run()
    assert not at.exception
    assert [w.value for w in at.warning] == ["Ticket #1 is already running."]


def test_the_ticket_tab_shows_a_running_job_and_hides_the_spec_chat(database):
    conn = db.connect(database)
    job = db.create_job(conn, "answer", {}, 1)
    conn.close()
    at = run("Ticket")
    assert f"Running: answer (job {job}, queued)" in text(at)
    assert not at.chat_input and at.button(key=f"job-cancel-{job}")


def test_the_ticket_tab_shows_why_the_last_run_failed(database):
    conn = db.connect(database)
    job = db.create_job(conn, "run", {}, 1)
    db.start_job(conn, job)
    db.finish_job(conn, job, error="ConnectionError: ollama is down")
    conn.close()
    assert "The last run failed: ConnectionError: ollama is down" in [
        e.value for e in run("Ticket").error
    ]


def spec_ticket_waiting_at_the_plan_gate(database):
    conn = db.connect(database)
    ticket = db.create_ticket(conn, 1, "Remove a Stop")
    db.set_status(conn, ticket, TicketStatus.SPEC)
    db.append_event(conn, ticket, "awaiting_plan", "supervisor", {})
    conn.close()
    return ticket


def open_ticket(ticket):
    at = AppTest.from_file(ui.__file__, default_timeout=30)
    at.session_state["view"] = "Ticket"
    at.session_state["ticket"] = ticket
    at.run()
    assert not at.exception, at.exception
    return at


def test_continue_to_build_is_offered_only_when_the_tools_are_present(
    database, monkeypatch, no_real_work
):
    ticket = spec_ticket_waiting_at_the_plan_gate(database)
    at = open_ticket(ticket)
    assert not [b for b in at.button if b.key == "ticket_continue"]
    assert "Build needs: No GitHub token found." in text(at)
    ready(monkeypatch)
    at = open_ticket(ticket)
    at.button(key="ticket_continue").click().run()
    assert no_real_work == [("continue", {"ticket_id": ticket}, ticket)]


def test_a_board_card_shows_a_running_ticket(database):
    conn = db.connect(database)
    db.create_job(conn, "run", {}, 2)
    conn.close()
    assert "Running…" in text(run("Board"))
```

Add `jobs`, `live`, `ops` to the `from sifu import ...` line.

- [ ] **Step 3: Run and confirm the new tests fail and say why** (`AttributeError` or missing text).

Run: `uv run --extra agents --extra ui pytest tests/test_ui.py -q`

- [ ] **Step 4: Implement behaviours 1 to 8 in `src/sifu/ui.py`** exactly as listed under "Behaviour to build". Update the module docstring's list of writes: chat message; "Create" files the draft with `ops.create_ticket` and queues its run; an answer is queued with `jobs.submit`. Remove the sentence about requests past Design needing the script, and say instead that they need the build tools listed in the sidebar.

- [ ] **Step 5: Delete `demo.start_in_database`** and its three tests in `tests/test_router_flow.py` (leave `answer_in_database`, `archive_chat`, `archive_chat_in_database` and `artifacts_dir`), and confirm `grep -rn start_in_database src tests scripts README.md` prints nothing.

- [ ] **Step 6: Extend the read-only test**

`test_viewing_never_writes_to_the_database` must also pass with tools present and with a failed job in the table. Add a second test:

```python
def test_viewing_never_writes_even_with_jobs_and_build_tools(database, monkeypatch):
    conn = db.connect(database)
    job = db.create_job(conn, "run", {}, 1)
    db.start_job(conn, job)
    db.finish_job(conn, job, error="boom")
    conn.close()
    ready(monkeypatch)
    before = database.read_bytes()
    for view in ui.VIEWS:
        run(view)
    assert database.read_bytes() == before
```

- [ ] **Step 7: Run all checks.** Expected: all pass.

- [ ] **Step 8: Update `README.md`**

In "Streamlit UI": replace the sentences that say an accepted spec "waits before Build" and that a plan or merge decision "shows the `scripts/live_run.py --answer` command" with: what the sidebar's build-tools line means; that Create and every answer are queued and run one at a time in the background while the page stays usable; that the Ticket tab shows the running job, a failed run's error, and "Continue to Build" for a ticket whose spec was accepted in design mode; that with the tools present a low-risk ticket runs from Create to the merge decision under `router.merge_mode` and `planner.enabled` from `config.yaml`; and that only one Streamlit server may use a database at a time. Add `src/sifu/live.py`, `ops.py` and `jobs.py` to the Layout list and `jobs` to the `db.py` line.

- [ ] **Step 9: Check it in the real app (design mode, real Ollama)**

```bash
cp data/design.db /tmp/sifu-plan1.db   # a throwaway copy
SIFU_UI_DB=/tmp/sifu-plan1.db uv run --extra agents --extra ui streamlit run src/sifu/ui.py
```

In the browser: New ticket, type a title and description by hand, Create. Expected: the page jumps to the ticket at once, shows "Running: run", and within a few minutes (after a rerun or Refresh) shows the Design-stage events and a status of `spec` or `needs_human`. If the GitHub token and `claude` are present on this machine the sidebar says "Build and merge: ready" and the run would go on into Build against the real product repo: for this check, do not create a ticket in that state; stop at confirming the sidebar text. Report what you saw; do not claim the live path works unless you ran it.

- [ ] **Step 10: Commit**

```bash
git add src/sifu/ui.py src/sifu/demo.py tests/test_ui.py tests/test_router_flow.py README.md
git commit -m "Streamlit UI: tickets run in the background, and Build and merge are driven from the app"
```

---

## After the last task

- Run `uv run --extra agents --extra ui pytest -q`, `uv run ruff check .`, `uv run ruff format --check .` once more and paste the three results in the hand-off.
- Not verified by tests in this plan, and to be checked by Lance on his machine: one real ticket from Create to a merged pull request with the build tools present.
- Known limits carried into Plan 2: a `failed` or `interrupted` run has no action yet; a stuck build or blocked merge has no request yet.
