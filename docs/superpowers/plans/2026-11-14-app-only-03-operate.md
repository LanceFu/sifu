# Operate Sifu from the App Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Everything a person does around a ticket run (choose run options, check the setup, make a database, read the artifacts and the diff, see token use, send lessons, clean up, talk a plan through) is done in the Streamlit app.

**Architecture:** A fourth tab, Settings, holds setup checks, run options, the read-only configuration, lessons, cleanup and database creation. Run options are per-database overrides of four `config.yaml` values, stored in a `settings` table and applied when `ops` builds its dependencies. The Ticket tab gains a files view, a diff view, a token meter and a chat on plan decisions. Each feature is a function in a non-UI module with its own tests, plus a thin block in `ui.py`.

**Tech Stack:** Python 3.10+, SQLite, Streamlit 1.66, git CLI, pytest, ruff.

**Spec:** `docs/superpowers/plans/2026-11-14-app-only-00-overview.md` (gaps 9 to 17). Requires Plans 1 and 2 to be merged.

## Global Constraints

- Only these four options can be overridden from the app, with exactly these values: `router.merge_mode` (`manual`, `risk_based`, `auto`), `planner.enabled` (bool), `critic.enabled` (bool), `implementer.backend` (`cloud`, `ollama`). Models, caps and `router.protected_paths` stay read-only.
- An override never bypasses a floor rule: it only changes a config value that `router.decide` already honours in every mode.
- The app shows whether a GitHub token exists, never its value, and has no field to enter one.
- Any path the app reads or deletes is checked to be inside the directory it should be in (`artifacts_dir`, `workspace.worktrees_dir`, `data/`) after `Path.resolve()`.
- A database name is `^[a-z0-9][a-z0-9-]{0,39}$`.
- Looking at the Settings tab never writes to the database.
- Tasks 1 to 8 are independent of each other except where "Consumes" says so; do them in order.

## Review Focus

1. **A database recorded before this plan has no `settings` table.** Expected: runs use `config.yaml` unchanged, and the Settings tab renders without writing. Pinned in Task 1.
2. **An artifact or worktree path that points outside its directory** (a symlink, or a `worktree` value in an old event). Expected: not read, not diffed, not deleted. Pinned in Tasks 4 and 7.
3. **Ollama is not running when the Settings tab opens.** Expected: the check fails within about 3 seconds and the tab still renders. Pinned in Task 2.
4. **A database name with a slash, dots or capitals, or one that exists.** Expected: refused with a message; no file created or overwritten. Pinned in Task 3.
5. **A lessons preview or pull request when every lesson is already in `conventions.md`.** Expected: a plain message, and no worktree or branch left behind. Pinned in Task 6.

## File Structure

| File | Responsibility |
| --- | --- |
| `src/sifu/settings.py` (create) | The four overridable options: validate, store, apply to a `Config` |
| `src/sifu/preflight.py` (create) | Setup checks and their results |
| `src/sifu/db.py` (modify) | `settings` table |
| `src/sifu/ops.py` (modify) | Apply overrides; create a database; worktree diff; cleanup; lessons |
| `src/sifu/live.py` (modify) | `missing_tools` built on `preflight` |
| `src/sifu/lessons.py`, `src/sifu/lessons_pr.py` (modify) | Pending lessons; one `run` used by the script and the app |
| `src/sifu/brainstorm.py`, `src/sifu/prompts/brainstorm_plan/persona.md` | The plan chat |
| `src/sifu/ui_data.py` (modify) | Artifact listing, worktree lookup, token use, chat request |
| `src/sifu/ui.py` (modify) | Settings tab; files, diff, token meter and plan chat on the Ticket tab |
| `.streamlit/config.toml` (create) | Bind the server to localhost |

---

### Task 1: Run options per database

**Files:**
- Create: `src/sifu/settings.py`
- Modify: `src/sifu/db.py`, `src/sifu/ops.py`, `src/sifu/ui.py`
- Test: `tests/test_settings.py`, `tests/test_ops.py`, `tests/test_ui.py`

**Interfaces:**
- Produces:
  - Table `settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT (datetime('now')))`.
  - `settings.OPTIONS: dict[str, tuple[Any, ...]]` = `{"router.merge_mode": ("manual", "risk_based", "auto"), "planner.enabled": (True, False), "critic.enabled": (True, False), "implementer.backend": ("cloud", "ollama")}`
  - `settings.load(db_path: str | Path) -> dict[str, Any]` (read-only; `{}` when the file or table is missing)
  - `settings.save(db_path: str | Path, key: str, value: Any) -> None` (`ValueError` for an unknown key or value)
  - `settings.clear(db_path: str | Path, key: str) -> None`
  - `settings.apply(cfg: Config, overrides: Mapping[str, Any]) -> Config` (a new `Config`; unknown keys ignored)
  - `ops.effective_config(db_path: str | Path, cfg: Config | None = None) -> Config`
  - A fourth tab: `ui.VIEWS == ("Board", "Ticket", "Inbox", "Settings")`; widget keys `opt-router.merge_mode`, `opt-planner.enabled`, `opt-critic.enabled`, `opt-implementer.backend`.

- [ ] **Step 1: Write the failing tests** in `tests/test_settings.py`

```python
"""Run options a person can override per database, and nothing else."""

import pytest

from sifu import db, settings


@pytest.fixture()
def path(tmp_path):
    file = tmp_path / "s.db"
    conn = db.connect(file)
    db.init_db(conn)
    conn.close()
    return file


def test_a_saved_option_is_loaded_and_applied_to_a_copy_of_the_config(path, cfg):
    cfg.router.merge_mode, cfg.critic.enabled = "risk_based", False
    settings.save(path, "router.merge_mode", "manual")
    settings.save(path, "critic.enabled", True)
    assert settings.load(path) == {"router.merge_mode": "manual", "critic.enabled": True}
    applied = settings.apply(cfg, settings.load(path))
    assert (applied.router.merge_mode, applied.critic.enabled) == ("manual", True)
    assert (cfg.router.merge_mode, cfg.critic.enabled) == ("risk_based", False)


def test_clearing_an_option_goes_back_to_the_config_file(path, cfg):
    settings.save(path, "planner.enabled", True)
    settings.clear(path, "planner.enabled")
    assert settings.load(path) == {}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("router.protected_paths", []),
        ("models.author", "other"),
        ("router.max_tokens_per_ticket", 1),
        ("router.merge_mode", "yolo"),
        ("planner.enabled", "yes"),
    ],
)
def test_only_the_four_options_and_their_values_can_be_saved(path, key, value):
    with pytest.raises(ValueError):
        settings.save(path, key, value)
    assert settings.load(path) == {}


def test_apply_ignores_keys_it_does_not_know(cfg):
    applied = settings.apply(cfg, {"router.protected_paths": [], "models.author": "x"})
    assert applied.router.protected_paths == cfg.router.protected_paths
    assert applied.models.author == cfg.models.author


def test_load_is_empty_and_writes_nothing_without_the_table_or_the_file(path, tmp_path):
    conn = db.connect(path)
    conn.execute("DROP TABLE settings")
    conn.commit()
    conn.close()
    before = path.read_bytes()
    assert settings.load(path) == {} and path.read_bytes() == before
    assert settings.load(tmp_path / "missing.db") == {}
    assert not (tmp_path / "missing.db").exists()
```

Append to `tests/test_ops.py`:

```python
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
```

- [ ] **Step 2: Run and confirm failure**, then **Step 3: implement**:

- Add the table to `db.SCHEMA`.
- `settings.py`: values are stored with `json.dumps`. `load` opens read-only (`sifu.evidence.open_recorded`) and returns `{}` on `FileNotFoundError` or `sqlite3.OperationalError`. `save` validates `key in OPTIONS and value in OPTIONS[key]` with a type check so `"yes"` and `1` are refused for a bool option (`type(value) is type(OPTIONS[key][0])`), then `db.connect`, `db.init_db`, `INSERT ... ON CONFLICT(key) DO UPDATE`. `apply` deep-copies the config and sets each known `section.field` with `setattr(getattr(copy, section), field, value)`.
- `ops.effective_config`: `settings.apply(cfg or load_config(ROOT / "config.yaml"), settings.load(db_path))`. `ops.open_deps` calls it first, in both modes, before the design-mode `planner.enabled = False`. `live.open_live` receives the resulting config through its `cfg` argument.

- [ ] **Step 4: UI tests** (append to `tests/test_ui.py`)

```python
def test_settings_shows_each_option_with_the_config_files_value(database):
    at = run("Settings")
    assert at.selectbox(key="opt-router.merge_mode").value == "risk_based"
    assert at.toggle(key="opt-critic.enabled").value is False


def test_changing_an_option_saves_it_for_this_database(database):
    at = run("Settings")
    at.selectbox(key="opt-router.merge_mode").set_value("manual").run()
    assert not at.exception, at.exception
    assert settings.load(database) == {"router.merge_mode": "manual"}
    assert run("Settings").selectbox(key="opt-router.merge_mode").value == "manual"
```

The first test reads `config.yaml`'s real values; if `router.merge_mode` or `critic.enabled` there differ from `risk_based` / `False` when you run it, assert against `load_config(ROOT / "config.yaml")` instead of literals.

- [ ] **Step 5: Implement the Settings tab's "Run options" section** in `ui.py`

- Add `"Settings"` to `VIEWS`, unpack four tabs in `main`, and add `show_settings(conn, db_path)`.
- Section header `st.markdown("**Run options**")` and caption `"These apply to this database only and override config.yaml. Risk floor rules apply in every mode."`.
- One widget per option, value = `ops.effective_config(db_path)`'s current value: `st.selectbox` for the two enumerations, `st.toggle` for the two booleans, each with `on_change=_save_option, args=(db_path, key)`. `_save_option` reads `st.session_state[f"opt-{key}"]` and calls `settings.save`. Saving happens only in the callback, so viewing never writes.
- Under them: `st.button("Use config.yaml values", key="opt-reset")` that clears all four.
- Then `st.markdown("**Configuration (read-only)**")` and `st.json(ops.effective_config(db_path).model_dump(), expanded=False)`. The config holds no secret: only the token's env-var name and Keychain item name.

- [ ] **Step 6: Run all checks** (`uv run --extra agents --extra ui pytest -q && uv run ruff check . && uv run ruff format --check .`). The `test_viewing_never_writes...` tests loop over `ui.VIEWS`, so they now cover Settings; they must pass.

- [ ] **Step 7: Commit**

```bash
git add src/sifu/settings.py src/sifu/db.py src/sifu/ops.py src/sifu/ui.py tests
git commit -m "Run options per database: merge mode, planner, critic and implementer backend, set in a Settings tab"
```

---

### Task 2: Setup checks

**Files:**
- Create: `src/sifu/preflight.py`
- Modify: `src/sifu/live.py`, `src/sifu/ui.py`
- Test: `tests/test_preflight.py`, `tests/test_live.py`, `tests/test_ui.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Check: name: str; ok: bool; detail: str; needed_for: Literal["design", "build"]`
  - `preflight.run_checks(cfg: Config, *, fetch: Callable[[str], bytes] | None = None, which: Callable[[str], str | None] = shutil.which, token: Callable[[GithubConfig], Any] = get_github_token, git: Callable[..., str] = git_output) -> list[Check]`
  - Check names, in this order: `Ollama`, `Models`, `Product repo`, `Claude Code`, `GitHub token`, `Clean clone`, `Verify tools`, `Worktrees folder`. The first three are `design`; the rest `build`.
  - `live.missing_tools(cfg=None)` now returns `[f"{c.name}: {c.detail}" for c in run_checks(cfg) if c.needed_for == "build" and not c.ok]`.
  - Widget key `checks_run`; session-state key `checks` (the last `list[Check]`).

What each check does (`fetch` defaults to a function that GETs a URL with `urllib.request.urlopen(url, timeout=3)` and returns the body):

| Name | Passes when | Detail when it fails |
| --- | --- | --- |
| `Ollama` | `fetch(f"{cfg.ollama.base_url}/api/tags")` returns | `f"not reachable at {base_url}"` |
| `Models` | every tag in `models.author`, `spec_reviewer`, `code_reviewer`, `risk_assessors` (plus `implementer.ollama_model` when `implementer.backend == "ollama"`) is a `name` in that response | `"missing: " + ", ".join(sorted(missing))`; when Ollama failed, `"not checked"` and `ok=False` |
| `Product repo` | the path has `.git`, `docs/glossary.yaml` and `docs/PRD.md` | names what is missing |
| `Claude Code` | `which("claude")` | ``"no `claude` on PATH"`` |
| `GitHub token` | `token(cfg.github)` returns | the `SecretNotFound` message |
| `Clean clone` | `git(repo, "status", "--porcelain")` is empty | `"uncommitted changes in the product repo"`; when the repo check failed, `"not checked"` |
| `Verify tools` | `which(step.command[0])` for every `verify.steps` entry | `"not on PATH: " + ", ".join(...)` |
| `Worktrees folder` | the folder, or its nearest existing parent, is writable (`os.access(..., os.W_OK)`) | `f"cannot write to {path}"` |

A passing check's detail is short and factual (`"3 models found"`, `"found"`). No detail ever contains the token: on success the token check's detail is the literal `"found"`.

- [ ] **Step 1: Write the failing tests** in `tests/test_preflight.py`

```python
"""Setup checks: what is missing for the Design stage and for Build."""

import json

from pydantic import SecretStr

from sifu import preflight
from sifu.credentials import SecretNotFound


def tags(*names):
    return lambda url: json.dumps({"models": [{"name": n} for n in names]}).encode()


def all_models(cfg):
    return {cfg.models.author, cfg.models.spec_reviewer, cfg.models.code_reviewer,
            *cfg.models.risk_assessors}  # fmt: skip


def good_repo(tmp_path, cfg):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "docs" / "glossary.yaml").write_text("terms: []\n")
    (repo / "docs" / "PRD.md").write_text("PRD")
    cfg.workspace.product_repo_path = str(repo)
    cfg.workspace.worktrees_dir = str(tmp_path / "wt")


def run(cfg, **overrides):
    kw = {
        "fetch": tags(*all_models(cfg)),
        "which": lambda name: f"/bin/{name}",
        "token": lambda github_cfg: SecretStr("t0ken-value"),
        "git": lambda repo, *args: "",
    }
    kw.update(overrides)
    return {c.name: c for c in preflight.run_checks(cfg, **kw)}


def test_everything_present_passes_in_the_documented_order(tmp_path, cfg):
    good_repo(tmp_path, cfg)
    checks = preflight.run_checks(
        cfg,
        fetch=tags(*all_models(cfg)),
        which=lambda name: f"/bin/{name}",
        token=lambda github_cfg: SecretStr("t0ken-value"),
        git=lambda repo, *args: "",
    )
    assert [c.name for c in checks] == [
        "Ollama", "Models", "Product repo", "Claude Code", "GitHub token", "Clean clone",
        "Verify tools", "Worktrees folder",
    ]  # fmt: skip
    assert all(c.ok for c in checks)
    assert [c.needed_for for c in checks] == ["design"] * 3 + ["build"] * 5
    assert not any("t0ken-value" in c.detail for c in checks)


def test_ollama_down_fails_both_model_checks_without_raising(tmp_path, cfg):
    good_repo(tmp_path, cfg)

    def down(url):
        raise OSError("connection refused")

    checks = run(cfg, fetch=down)
    assert not checks["Ollama"].ok and cfg.ollama.base_url in checks["Ollama"].detail
    assert not checks["Models"].ok and checks["Models"].detail == "not checked"


def test_a_missing_model_tag_is_named(tmp_path, cfg):
    good_repo(tmp_path, cfg)
    present = all_models(cfg) - {cfg.models.code_reviewer}
    checks = run(cfg, fetch=tags(*present))
    assert not checks["Models"].ok and cfg.models.code_reviewer in checks["Models"].detail


def test_the_local_implementer_model_is_checked_only_for_the_ollama_backend(tmp_path, cfg):
    good_repo(tmp_path, cfg)
    cfg.implementer.backend = "cloud"
    assert run(cfg)["Models"].ok
    cfg.implementer.backend = "ollama"
    assert cfg.implementer.ollama_model in run(cfg)["Models"].detail


def test_each_missing_build_tool_fails_its_own_check(tmp_path, cfg):
    good_repo(tmp_path, cfg)

    def no_token(github_cfg):
        raise SecretNotFound("No GitHub token found. Set $SIFU_GITHUB_TOKEN.")

    checks = run(
        cfg,
        which=lambda name: None,
        token=no_token,
        git=lambda repo, *args: " M Sources/Trip.swift\n",
    )
    assert not checks["Claude Code"].ok
    assert checks["GitHub token"].detail == "No GitHub token found. Set $SIFU_GITHUB_TOKEN."
    assert checks["Clean clone"].detail == "uncommitted changes in the product repo"
    assert "swift" in checks["Verify tools"].detail
    assert checks["Ollama"].ok and checks["Product repo"].ok  # design checks are unaffected


def test_a_missing_product_repo_skips_the_clean_clone_check(tmp_path, cfg):
    cfg.workspace.product_repo_path = str(tmp_path / "nowhere")
    checks = run(cfg)
    assert not checks["Product repo"].ok
    assert (checks["Clean clone"].ok, checks["Clean clone"].detail) == (False, "not checked")
```

Update `tests/test_live.py`: `missing_tools` now returns `"<name>: <detail>"` strings from the build checks. Rewrite its two `missing_tools` tests to patch `live.run_checks` (return a list of `Check`s) and assert the filtering and format; delete the assertions on the old three-message order.

- [ ] **Step 2: Run and confirm failure**, **Step 3: implement `preflight.py` and the new `live.missing_tools`.** Every check is wrapped so an exception inside it becomes `ok=False` with the exception text as the detail; `run_checks` never raises.

- [ ] **Step 4: UI test and section**

```python
def test_settings_runs_the_setup_checks_on_demand_and_shows_each_result(database, monkeypatch):
    results = [
        preflight.Check("Ollama", True, "reachable", "design"),
        preflight.Check("GitHub token", False, "No GitHub token found.", "build"),
    ]
    calls = []
    monkeypatch.setattr(preflight, "run_checks", lambda cfg, **kw: calls.append(1) or results)
    at = run("Settings")
    assert calls == []  # opening the tab does not run them
    at.button(key="checks_run").click().run()
    page = text(at)
    assert calls == [1] and "Ollama" in page and "No GitHub token found." in page
```

In `show_settings`, first section: `st.markdown("**Setup checks**")`, a button "Run checks" (`key="checks_run"`) that stores `preflight.run_checks(ops.effective_config(db_path))` in `st.session_state["checks"]` and pops `live_missing` (so the sidebar's build-tools line is recomputed), then one line per stored check: `st.markdown(f":green-badge[ok] **{c.name}** — {c.detail}")` or `:red-badge[missing]`, with a caption `"needed for Build"` on build checks. The autouse fixture in `tests/test_ui.py` already patches `live.missing_tools`.

- [ ] **Step 5: Run all checks; commit**

```bash
git add src/sifu/preflight.py src/sifu/live.py src/sifu/ui.py tests
git commit -m "Setup checks in the Settings tab: Ollama, models, product repo, Claude Code, token, clone, verify tools"
```

---

### Task 3: Create a database from the app

**Files:**
- Modify: `src/sifu/ops.py`, `src/sifu/ui.py`
- Test: `tests/test_ops.py`, `tests/test_ui.py`

**Interfaces:**
- Produces:
  - `ops.DATABASE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")`
  - `ops.create_database(name: str, *, data_dir: Path | None = None, cfg: Config | None = None) -> Path` (default `data_dir` is `sifu.evidence.DATA`; raises `ValueError` for a bad name, `FileExistsError` when the file exists)
  - Widget keys `db_name`, `db_create`; the sidebar's database selectbox gets `key="db"`; session-state key `db_pending` (a `Path` to select on the next run).

- [ ] **Step 1: Write the failing tests** (in `tests/test_ops.py`)

```python
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
```

- [ ] **Step 2: Implement** with `DATABASE_NAME.fullmatch(name)`; the same project call as `ops.create_ticket`.

- [ ] **Step 3: UI.** In `show_settings`, section `st.markdown("**New database**")`: `st.text_input("Name", key="db_name", help="Lowercase letters, digits and hyphens.")` and a button "Create database" (`key="db_create"`). On click call `ops.create_database`, set `st.session_state["db_pending"] = path`, set the notice `f"Created {path.name}."` and `st.rerun()`; show `st.error(str(exc))` for `ValueError` and `FileExistsError`. In `main`, give the database selectbox `key="db"`, and before building it: `if (pending := st.session_state.pop("db_pending", None)) is not None: st.session_state["db"] = pending`. Test:

```python
def test_a_new_database_is_created_and_selected(database, monkeypatch, tmp_path):
    made = tmp_path / "trial.db"

    def create(name):
        conn = db.connect(made)
        db.init_db(conn)
        conn.close()
        return made

    monkeypatch.setattr(ops, "create_database", create)
    monkeypatch.setattr(ui_data, "list_databases", lambda data_dir: [made])
    at = run("Settings")
    at.text_input(key="db_name").set_value("trial").run()
    at.button(key="db_create").click().run()
    assert not at.exception, at.exception
    assert at.session_state["db"] == made
    assert "Created trial.db." in [s.value for s in at.success]
```

- [ ] **Step 4: Run all checks; commit**

```bash
git add src/sifu/ops.py src/sifu/ui.py tests
git commit -m "Create a database from the Settings tab"
```

---

### Task 4: Files, diff and token use on the Ticket tab

**Files:**
- Modify: `src/sifu/ui_data.py`, `src/sifu/ops.py`, `src/sifu/ui.py`
- Test: `tests/test_ui_data.py`, `tests/test_ops.py`, `tests/test_ui.py`

**Interfaces:**
- Consumes: Plan 2's `db.tokens_used`; `demo.artifacts_dir(db_path, cfg=None)`.
- Produces:
  - `ui_data.artifact_files(folder: Path) -> list[Path]`
  - `ui_data.worktree_of(conn, ticket_id: int) -> str | None`
  - `ops.safe_worktree(worktree: str | None, cfg: Config) -> Path | None`
  - `ops.worktree_diff(worktree: Path, limit: int = 200_000) -> str`
  - Widget keys `file_pick`, `file_download`.

- [ ] **Step 1: Write the failing tests**

`tests/test_ui_data.py`:

```python
def test_artifact_files_lists_markdown_files_in_the_folder_only(tmp_path):
    (tmp_path / "spec.md").write_text("s")
    (tmp_path / "intent.md").write_text("i")
    (tmp_path / "notes.txt").write_text("n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.md").write_text("d")
    outside = tmp_path.parent / "secret.md"
    outside.write_text("x")
    (tmp_path / "link.md").symlink_to(outside)
    assert [p.name for p in ui_data.artifact_files(tmp_path)] == ["intent.md", "spec.md"]
    assert ui_data.artifact_files(tmp_path / "missing") == []


def test_worktree_of_is_the_last_implementer_runs_worktree(conn, project):
    tid = db.create_ticket(conn, project, "Rename a Trip")
    assert ui_data.worktree_of(conn, tid) is None
    db.append_event(conn, tid, "implementer_result", "implementer", {"worktree": "/wt/ticket-1"})
    db.append_event(conn, tid, "implementer_result", "implementer", {"worktree": "/wt/ticket-1-b"})
    assert ui_data.worktree_of(conn, tid) == "/wt/ticket-1-b"
```

`tests/test_ops.py`:

```python
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


def test_worktree_diff_shows_changes_and_new_files_and_is_capped(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    git = lambda *a: subprocess.run(  # noqa: E731
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, check=True,
        capture_output=True,
    )  # fmt: skip
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
```

- [ ] **Step 2: Implement**

- `artifact_files`: `sorted` paths `p` in `folder.iterdir()` with `p.suffix == ".md"`, `p.is_file()` and `not p.is_symlink()`; `[]` when the folder is missing.
- `safe_worktree`: resolve both paths; return the resolved worktree only when it is a directory, is not the worktrees dir itself and `is_relative_to` it.
- `worktree_diff`: `git -C <worktree> diff HEAD` followed, when there are untracked files (`git ls-files --others --exclude-standard`), by a line `"Untracked files:"` and one line per file. Cut to `limit` characters and append `f"\n(diff cut at {limit} characters)"` when cut. Use `implementer.git_output`.

- [ ] **Step 3: UI tests**

```python
def test_the_ticket_tab_lists_the_tickets_files_and_shows_the_chosen_one(
    database, monkeypatch, tmp_path
):
    folder = tmp_path / "1"
    folder.mkdir()
    (folder / "intent.md").write_text("# Intent: Rename a Trip")
    (folder / "spec.md").write_text("# Spec: Rename a Trip\nAC-1 renames it.")
    monkeypatch.setattr(demo, "artifacts_dir", lambda path, cfg=None: tmp_path)
    at = run("Ticket")
    pick = at.selectbox(key="file_pick")
    assert pick.options == ["intent.md", "spec.md"]
    pick.set_value("spec.md").run()
    assert "AC-1 renames it." in text(at)


def test_the_ticket_tab_shows_token_use_against_the_budget(database):
    conn = db.connect(database)
    db.record_run(conn, "reviewer", "m", 1, 1000, 500)
    conn.close()
    assert "1,500 of" in text(run("Ticket")) and "model tokens" in text(run("Ticket"))
```

`text()` in `tests/test_ui.py` joins markdown, caption, info and code elements; show the file with `st.code` and the token line as `st.caption` so both are found. Note that the existing spec-chat test patches `demo.artifacts_dir` with a one-argument lambda; change that lambda to accept `cfg=None` as well if you call `artifacts_dir` with a config.

- [ ] **Step 4: Implement on the Ticket tab**, after the "Ticket text" expander:

- Token use: `used = db.tokens_used(conn, ticket_id)`; `limit = ops.effective_config(db_path).router.max_tokens_per_ticket`; `st.progress(min(used / limit, 1.0))` and `st.caption(f"{used:,} of {limit:,} model tokens")`. `tokens_used` is a read; call it on the page's read-only connection.
- `with st.expander("Files"):` when `artifact_files` is non-empty: `st.selectbox("File", names, key="file_pick")`, `st.code(text, language="markdown", wrap_lines=True)`, `st.download_button("Download", text, file_name=name, key="file_download")`. Read the file only if its name is in the listing.
- `with st.expander("Diff"):` only when `ops.safe_worktree(ui_data.worktree_of(conn, ticket_id), cfg)` is not None: `st.code(ops.worktree_diff(path), language="diff")`. Compute the diff inside the expander so it costs nothing on a ticket without a worktree.

- [ ] **Step 5: Run all checks; commit**

```bash
git add src/sifu/ui_data.py src/sifu/ops.py src/sifu/ui.py tests
git commit -m "Ticket tab: the ticket's files, its worktree diff and token use against the budget"
```

---

### Task 5: Chat on a plan decision

**Files:**
- Create: `src/sifu/prompts/brainstorm_plan/persona.md`
- Modify: `src/sifu/brainstorm.py`, `src/sifu/ui_data.py`, `src/sifu/ui.py`
- Test: `tests/test_brainstorm.py`, `tests/test_ui_data.py`, `tests/test_ui.py`

**Interfaces:**
- Produces:
  - `brainstorm.STAGES == ("intent", "spec", "plan")`
  - `ui_data.chat_request(conn, ticket_id: int) -> dict[str, Any] | None`: the ticket's open request when its `stage` is `None` (questions), `"accept_spec"` or `"accept_plan"`. It replaces `ui_data.design_request`; delete that function and update its callers and tests.
  - Each returned item gains `"chat_stage": "spec" | "plan"`.
  - Chat log file for a plan chat: `chat-plan-<request id>.md`.

- [ ] **Step 1: Write the failing tests**

`tests/test_brainstorm.py`:

```python
def test_the_plan_chat_has_its_own_persona(ctx):
    llm = Scripted(turn(draft=""))
    brainstorm.run_turn(llm, "m", ctx, "plan", said("Why this file?"), document="PLAN TEXT")
    [call] = llm.calls
    assert "Plan Coach" in call["system"] and "<document>\nPLAN TEXT" in call["user"]
```

`tests/test_ui_data.py`: rename `test_design_request_is_the_open_request_the_ui_can_answer` to use `chat_request`, and extend it:

```python
    plan = db.create_ticket(conn, project, "Reorder Stops")
    db.open_human_request(
        conn, plan, "Approve plan?", ["approve", "close"], decide(conn, plan, DecisionType.ACCEPT_PLAN)
    )
    assert ui_data.chat_request(conn, plan)["chat_stage"] == "plan"
    assert ui_data.chat_request(conn, spec)["chat_stage"] == "spec"
    assert ui_data.chat_request(conn, merge) is None
```

`tests/test_ui.py`:

```python
def test_a_plan_request_gets_a_chat_with_the_plan_beside_it(
    database, monkeypatch, tmp_path, no_real_work
):
    conn = db.connect(database)
    ticket = db.create_ticket(conn, 1, "Reorder Stops", "As a Traveller I reorder Stops.")
    result = DecisionResult(floor_tier=RiskTier.HUMAN, final_tier=RiskTier.HUMAN, rules_fired=["x"])
    decision = db.record_decision(conn, ticket, DecisionType.ACCEPT_PLAN, result, [])
    db.set_status(conn, ticket, TicketStatus.NEEDS_HUMAN)
    request = db.open_human_request(
        conn, ticket, "Approve this plan?", ["approve", "close"], decision
    )
    conn.close()
    (tmp_path / str(ticket)).mkdir()
    (tmp_path / str(ticket) / "plan.md").write_text("# Plan\nStep 1: reorder in DayPlan.swift")
    monkeypatch.setattr(demo, "artifacts_dir", lambda path, cfg=None: tmp_path)
    calls = []
    monkeypatch.setattr(brainstorm, "reply_in_database", fake_reply(calls, draft="Keep Stop ids."))
    ready(monkeypatch)
    at = open_ticket(ticket)
    assert "Step 1: reorder in DayPlan.swift" in text(at)
    at.chat_input(key=f"spec-input-ticket-{ticket}-request-{request}").set_value("Why?").run()
    assert (calls[0]["stage"], "DayPlan.swift" in calls[0]["document"]) == ("plan", True)
```

- [ ] **Step 2: Write `src/sifu/prompts/brainstorm_plan/persona.md`.** Model it on `brainstorm_spec/persona.md` (same two duties, same "no tools", same JSON reply format with `title` empty). Differences, all of which must be in the text: the first sentence names the role "Plan Coach"; the document is `plan.md`, the build plan written from an accepted spec; the reply explains which files the plan changes and why, where the plan goes outside the spec's affected paths, and which tests it adds; the draft is a revision note for the planner, or empty when the person will approve the plan as it is.

- [ ] **Step 3: Implement**

- `brainstorm.STAGES`: add `"plan"`.
- `ui_data.chat_request`: as specified. A plan request is in `LIVE_DECISIONS`, so its item has `needs_live_tools=True`; `chat_request` does not filter on that flag any more.
- `ui.py`: rename `show_spec_chat` to `show_request_chat`. The stage passed to `_chat_turn` and to the chat archive is `request["chat_stage"]`; the archive name is `f"chat-{stage}-{request['id']}.md"`. `_request_document` returns `plan.md` followed by `spec.md` for `accept_plan`. When the request `needs_live_tools` and build tools are missing, show the panel's document and chat but replace the answer buttons with the caption `"Answering needs: " + "; ".join(_missing())`.

- [ ] **Step 4: Run all checks; commit**

```bash
git add src/sifu/prompts/brainstorm_plan src/sifu/brainstorm.py src/sifu/ui_data.py src/sifu/ui.py tests
git commit -m "A plan decision gets the same chat as a spec decision, with plan.md beside it"
```

---

### Task 6: Lessons from the app

**Files:**
- Modify: `src/sifu/lessons.py`, `src/sifu/lessons_pr.py`, `scripts/lessons_pr.py`, `src/sifu/ops.py`, `src/sifu/jobs.py`, `src/sifu/ui.py`
- Test: `tests/test_lessons.py`, `tests/test_lessons_pr.py`, `tests/test_ops.py`, `tests/test_jobs.py`, `tests/test_ui.py`

**Interfaces:**
- Consumes: `lessons_pr.prepare`, `lessons_pr.publish`, `lessons_pr.discard`, `lessons_pr.Lesson`; `graph.Deps.proposed_conventions` is `<artifacts dir>/conventions.proposed.md`.
- Produces:
  - `lessons.pending(approved: str | Path, proposed: str | Path) -> list[str]`: bullets under `## Lessons` in the proposed file that are not lines of the approved file, without the leading `"- "`; `[]` when the proposed file is missing.
  - `lessons_pr.run(cfg: Config, lessons: list[Lesson], *, push: bool, db_path: str | Path | None = None, project: str | None = None, client: PullRequests | None = None, token: SecretStr | None = None, remote: str | None = None) -> dict[str, Any]` (`remote` is passed to `publish`, as its existing test does to avoid the network) with keys `diff`, `tier`, `rules`, `added`, `skipped`, `pr_url` (None on a dry run).
  - `ops.preview_lessons(db_path, lessons: list[dict[str, str]], *, cfg=None) -> dict[str, Any]` (dry run)
  - `ops.send_lessons(db_path, lessons: list[dict[str, str]], *, cfg=None) -> str` (opens the PR; returns its URL)
  - `jobs.HANDLERS["lessons"] = ops.send_lessons`; payload `{"lessons": [{"lesson": str, "evidence": str}, ...]}`; submitted with `ticket_id=None`.
  - Widget keys `lessons_editor`, `lessons_preview`, `lessons_send`.

- [ ] **Step 1: Write the failing tests**

`tests/test_lessons.py`:

```python
def test_pending_is_what_the_proposed_copy_adds_to_the_approved_one(tmp_path):
    approved = tmp_path / "conventions.md"
    proposed = tmp_path / "conventions.proposed.md"
    approved.write_text("# Conventions\n\n## Lessons\n- Old rule.\n")
    assert pending(approved, proposed) == []  # no proposed copy yet
    propose_lesson(approved, proposed, "Trim names before checking them.", 4)
    propose_lesson(approved, proposed, "Old rule.")
    assert pending(approved, proposed) == ["(ticket 4) Trim names before checking them."]
```

`tests/test_lessons_pr.py` (it has a `repo` fixture, a `git(*args, cwd=)` helper and a `FakeGitHub` class; its `publish` test pushes to a local bare repo through `remote=`). These tests need a `cfg` whose `workspace.product_repo_path` is `repo`, `workspace.worktrees_dir` is under `tmp_path` and `router.protected_paths` includes `conventions.md`; set those on the shared `cfg` fixture at the top of each test:

```python
def test_run_dry_returns_the_diff_and_decision_and_leaves_nothing_behind(repo, cfg):
    out = lessons_pr.run(cfg, [Lesson(lesson="Trim names.", evidence="ticket 4")], push=False)
    assert "+- Trim names." in out["diff"] and out["tier"] == "HUMAN"
    assert out["pr_url"] is None and out["added"] == ["Trim names."]
    assert git_output(repo, "branch", "--list", "sifu/lessons-*").strip() == ""


def test_run_with_nothing_new_raises_and_leaves_nothing_behind(repo, cfg):
    (repo / "conventions.md").write_text("# Conventions\n\n## Lessons\n- Trim names.\n")
    git("add", ".", cwd=repo)
    git("commit", "-qm", "lesson already there", cwd=repo)
    with pytest.raises(ValueError, match="already in conventions.md"):
        lessons_pr.run(cfg, [Lesson(lesson="Trim names.", evidence="e")], push=False)
    assert git_output(repo, "branch", "--list", "sifu/lessons-*").strip() == ""


def test_run_push_opens_the_pr_and_records_it_as_a_ticket(repo, cfg, tmp_path):
    remote = tmp_path / "remote.git"
    git("init", "--bare", "-q", str(remote), cwd=tmp_path)
    out = lessons_pr.run(
        cfg, [Lesson(lesson="Trim names.", evidence="ticket 4")], push=True,
        db_path=tmp_path / "l.db", project="TripPlanner", client=FakeGitHub(), token=None,
        remote=str(remote),
    )  # fmt: skip
    assert out["pr_url"]
    conn = db.connect(tmp_path / "l.db")
    [ticket] = conn.execute("SELECT * FROM tickets").fetchall()
    assert ticket["title"] == "Lessons for conventions.md (1)"
    assert "pr_opened" in [e["kind"] for e in db.list_events(conn, ticket["id"])]
```

`git_output` comes from `sifu.implementer`. If the `repo` fixture's git identity or commit flags differ from the two `git(...)` lines above, copy the form the fixture itself uses.

- [ ] **Step 2: Implement `lessons.pending` and `lessons_pr.run`.** `run` is the body of `scripts/lessons_pr.py`'s `main` from `name = ...` to the end, without the prints: `prepare`; on a dry run `discard` and return; otherwise `publish`, then record a ticket, a `MERGE` decision and a `pr_opened` event in `db_path` (default `ROOT / "data" / "lessons.db"`) under project `project or cfg.github.repo`. `client` and `token` default to `GitHubClient.from_config(cfg.github)` and `get_github_token(cfg.github)`, looked up only when `push` is True. Then make the script's `main` call `run` and print from its result.

- [ ] **Step 3: `ops` and `jobs`**

```python
def test_send_lessons_is_a_job_kind():
    assert jobs.HANDLERS["lessons"] is ops.send_lessons
```

`ops.preview_lessons` and `ops.send_lessons` build `Lesson` objects (a `ValidationError` for a missing field propagates), drop rows whose lesson is blank, raise `ValueError("no lessons to send")` when none remain, and call `lessons_pr.run(ops.effective_config(db_path, cfg), ..., push=False | True, db_path=db_path, project="TripPlanner")`. `send_lessons` returns `out["pr_url"]`.

- [ ] **Step 4: UI test and section**

```python
def test_lessons_are_previewed_then_sent_as_a_job(database, monkeypatch, no_real_work):
    monkeypatch.setattr(lessons, "pending", lambda approved, proposed: ["(ticket 4) Trim names."])
    monkeypatch.setattr(
        ops,
        "preview_lessons",
        lambda path, rows, cfg=None: {
            "diff": "+- Trim names.", "tier": "HUMAN", "rules": ["touches_protected_path"],
            "added": ["Trim names."], "skipped": [], "pr_url": None,
        },
    )  # fmt: skip
    ready(monkeypatch)
    at = run("Settings")
    at.button(key="lessons_preview").click().run()
    assert "+- Trim names." in text(at) and "touches_protected_path" in text(at)
    at.button(key="lessons_send").click().run()
    [(kind, payload, ticket_id)] = no_real_work
    assert kind == "lessons" and ticket_id is None
    assert payload["lessons"][0]["lesson"] == "(ticket 4) Trim names."
```

Section `st.markdown("**Lessons**")` in `show_settings`: caption `"Lessons go to conventions.md through a pull request that a person merges; Sifu never merges it."`; `st.data_editor` (`key="lessons_editor"`, `num_rows="dynamic"`) over rows `{"lesson": ..., "evidence": ""}` prefilled from `lessons.pending(<product repo>/conventions.md, demo.artifacts_dir(db_path) / "conventions.proposed.md")`; "Preview" (`key="lessons_preview"`) calls `ops.preview_lessons` in a spinner and shows the diff with `st.code(language="diff")`, the tier and rules as a caption, and any skipped lessons; "Open pull request" (`key="lessons_send"`, disabled when build tools are missing) submits the `lessons` job. Show `ValueError` text with `st.error`. Read the rows to send from the `data_editor`'s return value.

- [ ] **Step 5: Run all checks; commit**

```bash
git add src/sifu/lessons.py src/sifu/lessons_pr.py scripts/lessons_pr.py src/sifu/ops.py src/sifu/jobs.py src/sifu/ui.py tests
git commit -m "Lessons from the Settings tab: preview the conventions.md change, then open its pull request as a job"
```

---

### Task 7: Clean up finished tickets' worktrees

**Files:**
- Modify: `src/sifu/ops.py`, `src/sifu/ui.py`
- Test: `tests/test_ops.py`, `tests/test_ui.py`

**Interfaces:**
- Consumes: Plan 2's `implementer.remove_worktree`; Plan 1's `live.tag_for`; Task 4's `ui_data.worktree_of` and `ops.safe_worktree`.
- Produces:
  - `ops.stale_worktrees(db_path, *, cfg: Config | None = None) -> list[dict[str, Any]]`: one `{"ticket_id", "title", "status", "worktree"}` per ticket whose status is `done` or `failed` and whose `safe_worktree` exists.
  - `ops.remove_ticket_worktree(db_path, ticket_id: int, *, cfg: Config | None = None) -> bool`: refuses (`ValueError`) unless the ticket is `done` or `failed`; logs a `worktree_removed` event (actor `human`, payload `{"worktree": str}`) when it removed one.
  - Widget key `wt-remove-<ticket id>`.

- [ ] **Step 1: Write the failing tests** (`tests/test_ops.py`; build a real git repo and worktree with `implementer.create_worktree`, with `cfg.workspace.product_repo_path` and `worktrees_dir` under `tmp_path`, and a database whose stem is the worktree tag)

```python
def test_only_finished_tickets_with_a_worktree_on_disk_are_listed(wt_home, cfg):
    path, done, building = (
        wt_home  # fixture: db path, a done ticket and a build ticket, each with a worktree
    )
    assert [w["ticket_id"] for w in ops.stale_worktrees(path, cfg=cfg)] == [done]


def test_removing_a_finished_tickets_worktree_deletes_it_and_logs_it(wt_home, cfg):
    path, done, building = wt_home
    assert ops.remove_ticket_worktree(path, done, cfg=cfg) is True
    assert ops.stale_worktrees(path, cfg=cfg) == []
    assert "worktree_removed" in [e["kind"] for e in db.list_events(db.connect(path), done)]
    assert ops.remove_ticket_worktree(path, done, cfg=cfg) is False


def test_a_running_tickets_worktree_is_never_removed(wt_home, cfg):
    path, done, building = wt_home
    with pytest.raises(ValueError, match="is build"):
        ops.remove_ticket_worktree(path, building, cfg=cfg)
```

Write the `wt_home` fixture in the test file: create the repo (one commit), create two tickets, `create_worktree(repo, worktrees, ticket, tag=Path(path).stem)` for each, append an `implementer_result` event with that `worktree` path to each, and move the tickets to `done` (through `spec`, `build`, `test` with the gate facts) and `build`.

- [ ] **Step 2: Implement both functions**, then the UI section `st.markdown("**Cleanup**")` in `show_settings`: one row per `ops.stale_worktrees` entry (`#id title · status · path`) with a "Remove" button (`key=f"wt-remove-{id}"`) that calls `ops.remove_ticket_worktree` and reruns; `st.caption("No worktrees of finished tickets.")` when the list is empty. UI test: patch `ops.stale_worktrees` to return one entry and `ops.remove_ticket_worktree` to record its call; click the button; assert the call.

- [ ] **Step 3: Add `"worktree_removed"` to `ui_data.KEY_KINDS`. Run all checks; commit**

```bash
git add src/sifu/ops.py src/sifu/ui_data.py src/sifu/ui.py tests
git commit -m "Remove the worktrees and branches of finished tickets from the Settings tab"
```

---

### Task 8: Bind to localhost, and document the app as the way to run Sifu

**Files:**
- Create: `.streamlit/config.toml`
- Modify: `README.md`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
def test_the_streamlit_server_listens_on_localhost_only():
    text = (Path(__file__).resolve().parents[1] / ".streamlit" / "config.toml").read_text()
    assert 'address = "localhost"' in text
```

A plain string check, because `pyproject.toml` allows Python 3.10, which has no `tomllib`.

- [ ] **Step 2: Create `.streamlit/config.toml`**

```toml
# The app can start builds and merge pull requests, and it has no login.
# Keep it reachable from this machine only.
[server]
address = "localhost"

[browser]
gatherUsageStats = false
```

- [ ] **Step 3: README.** Rewrite "Streamlit UI" so it reads as the main way to run Sifu, in this order: the launch command (run from the repo root so `.streamlit/config.toml` is picked up); the sidebar (database, project, build-tools line); Board; Ticket (status and running job, files, diff, token use, spec and plan chat, Continue / Reopen / Resume); Inbox; Settings (setup checks, run options, configuration, lessons, cleanup, new database). State plainly: the server listens on localhost only and has no login; do not expose it. In "Status" update the UI row. In the script sections say `scripts/live_run.py` and `scripts/lessons_pr.py` remain as command-line equivalents, and that the evaluation and probe scripts are terminal-only.

- [ ] **Step 4: Check it in the real app**

```bash
uv run --extra agents --extra ui streamlit run src/sifu/ui.py
```

Confirm in the terminal output that the URL is `http://localhost:8501` and no Network URL is printed. In the browser: Settings, "Run checks" (real results for this machine), change merge mode to `manual` and back, create a database named `scratch-check` and see it selected, then delete `data/scratch-check.db` and `data/artifacts/scratch-check` by hand. Report what each step showed.

- [ ] **Step 5: Run all checks; commit**

```bash
git add .streamlit/config.toml README.md tests/test_config.py
git commit -m "The app listens on localhost only; README describes it as the way to run Sifu"
```

---

## After the last task

- Paste the output of the three check commands in the hand-off.
- `docs/ui` screenshots, the notebook's last section and `notebooks/sifu_demo.pdf` still show the app as it was at submission. Retaking them is not part of this plan; ask Lance whether he wants it.
