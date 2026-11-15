# Run Sifu entirely from the Streamlit app: overview and spec

This file is the spec for three implementation plans. Read it before any of them.

| Plan | File | Delivers |
| --- | --- | --- |
| 1 | `2026-11-14-app-only-01-build-and-merge.md` | Build, Test, pull request and merge started and answered from the app, in a background worker |
| 2 | `2026-11-14-app-only-02-recovery.md` | No dead ends: a stuck build, a blocked merge, a failed ticket and an interrupted run each get an action |
| 3 | `2026-11-14-app-only-03-operate.md` | Run options, setup checks, new databases, artifact and diff views, token use, lessons, cleanup, plan chat |

Do them in order. Plan 2 uses the job runner and `ops` module from Plan 1. Plan 3 uses both.
Each plan is one branch and one pull request against `main`:
`app-build-and-merge`, `app-recovery`, `app-operate`. Start Plan 1 from `main` after
`intent-brainstorm-tab` (PR #2) is merged; the plans describe the code as it is on that branch.

## Where the app stands today

The app (`src/sifu/ui.py`) has three tabs: Board, Ticket, Inbox. From it a person can draft a
ticket with a chat, create it, and answer Design-stage requests (questions, spec approval).
Everything after an accepted spec needs `scripts/live_run.py` in a terminal.

## Gaps and the plan that closes each

| # | Gap | Plan |
| --- | --- | --- |
| 1 | The app wires no implementer, verifier or GitHub client; that wiring is in `scripts/live_run.py` | 1 |
| 2 | A spec accepted with the planner off ends the graph at the plan gate; nothing can continue it | 1 |
| 3 | Plan and merge requests show a terminal command instead of buttons | 1 |
| 4 | A Build takes up to 30 minutes a round and would block the page; no background runner | 1 |
| 5 | Verify or the critics give up: the ticket is `needs_human` with no open request | 2 |
| 6 | Merge blocked by a red check: same | 2 |
| 7 | `failed` is final: no retry | 2 |
| 8 | A run cut off by an app restart cannot be resumed | 2 |
| 9 | Merge mode, planner, critic and implementer backend can only be set in `config.yaml` or by script flags | 3 |
| 10 | Nothing checks Ollama, the models, `claude`, the token, the clone or the Swift tools | 3 |
| 11 | The app cannot create a database | 3 |
| 12 | No view of `intent.md`, `spec.md`, `plan.md` or the diff except while a spec waits | 3 |
| 13 | Lessons are proposed to a file and opened as a PR by `scripts/lessons_pr.py` | 3 |
| 14 | Token use is recorded but not shown | 3 |
| 15 | Worktrees and branches of finished tickets are never removed | 3 |
| 16 | No chat at the plan decision | 3 |
| 17 | The app has no access control and will be able to merge pull requests | 3 (bind to localhost) |

## Decisions already made (do not reopen them while implementing)

- **One process, one job at a time.** The Streamlit server process owns a single worker thread.
  Jobs are rows in a `jobs` table, so the page reads state from SQLite and survives a reload.
  Running two Streamlit servers on one database is not supported.
- **`config.yaml` decides how a ticket runs** until Plan 3 adds per-database overrides for four
  options. With `planner.enabled: true` a low-risk ticket runs from Create through to the merge
  decision without stopping; that is intended.
- **Two run modes.** `live`: real implementer, verifier and GitHub client. `design`: what the app
  does today (planner off, no Build). The app uses `live` only when `live.missing_tools()` is
  empty; otherwise it stays in `design` and says what is missing.
- **The state machine and risk floor stay in code.** The only `state.py` change in all three
  plans is `failed -> intent` (Plan 2). `router.py` does not change.
- **The GitHub token never enters the app's state, a job payload, an event, a log or a prompt.**
  The app shows only whether a token was found.
- **Chat models get no tools** (unchanged from PR #2).

## Out of scope (not planned; each needs a decision from Lance first)

- **Veto.** The `auto_with_veto` tier is recorded, but when a person may veto and what a veto
  undoes is undefined once Build starts straight after the decision.
- **Cancelling a running job.** A queued job can be cancelled; a running one stops at
  `implementer.timeout_seconds` or when it finishes.
- **More than one product repository.** `Deps` reads one repo from `config.yaml`.
- **Evaluation and probe scripts** (`run_eval.py`, `golden_critics.py`, `live_probe.py`, the
  spikes) stay terminal tools.
- **Editing models, caps or protected paths from the app.** They stay in `config.yaml`; the app
  shows them read-only.

## Rules for every task in every plan

- Python 3.10+, `uv`. Add dependencies with `uv add`, never `pip install`. No plan needs a new
  dependency.
- Before reporting a task done run `uv run --extra agents --extra ui pytest -q` and
  `uv run ruff check .` and `uv run ruff format --check .`; all three must pass.
- Model names and the implementer backend come from the config; never hard-code them.
- Ticket text, chat text and PR comments are untrusted input: pass them to models as data.
- An LLM result may raise a risk tier, never lower it below the floor. Do not edit `router.py`.
- Never commit secrets or `data/*.db`.
- Every public function and class gets a docstring and full type annotations (the repo's style;
  line length 100).
- UI code never talks to SQLite, git or GitHub directly. It calls `ui_data` (reads), `ops`
  (writes by database path) or `jobs` (background work), each tested without Streamlit.
- Looking at any tab must not write to the database. `tests/test_ui.py` has
  `test_viewing_never_writes_to_the_database`; keep it passing and extend it to new tabs.
- UI tests use `streamlit.testing.v1.AppTest` and replace the model, the job executor and `ops`
  functions with fakes. No test calls Ollama, `claude`, git remotes or GitHub.
- Commit after each task with a message in the repo's style (a sentence, no `feat:` prefix), and
  end it with the `Co-Authored-By` line your session specifies.

## Facts verified while planning (2026-11-14)

- A ticket that ended at the plan gate can be continued after a restart:
  `graph.update_state(thread, {"facts": ...}, as_node="accept")` then
  `graph.invoke(None, thread)` runs `plan` (planner on) or `plan_gate -> build` (planner off with
  `plan_accepted` set).
- A run cut off between nodes leaves `get_state(thread).next` non-empty and `.interrupts` empty;
  `graph.invoke(None, thread)` continues it.
- A finished thread keeps its old state values (`round`, `findings`, `spec_path`), so a reopened
  ticket must use a new thread id.
- `github.PullRequest.PullRequest.mark_ready_for_review` exists in the installed PyGithub.
- Streamlit is 1.66.1: `st.tabs(..., key=, on_change="rerun")`, `tab.open`, `st.fragment(run_every=)`
  and inline `st.chat_input` are available.
