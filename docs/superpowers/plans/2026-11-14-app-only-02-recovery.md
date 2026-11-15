# Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** No ticket is left in a state the app cannot act on: a build that gave up, a merge blocked by a red check, a failed ticket and a run cut off by a restart each get a button.

**Architecture:** The graph gains one pause, `escalate_stuck`, reached when verify or the critics give up or the merge is blocked; its answer is `retry` or `close`. A failed ticket is reopened on a fresh LangGraph thread through one new state-machine transition, `failed -> intent`. A run cut off mid-graph is resumed from its last checkpoint. Each action is an `ops` function and a job kind.

**Tech Stack:** Python 3.10+, SQLite, LangGraph, PyGithub, Streamlit 1.66, pytest, ruff.

**Spec:** `docs/superpowers/plans/2026-11-14-app-only-00-overview.md` (gaps 5 to 8). Requires Plan 1 (`sifu/live.py`, `ops.py`, `jobs.py`, the `jobs` table) to be merged.

## Global Constraints

- The only `state.py` change is adding `S.INTENT` to `TRANSITIONS[S.FAILED]`. `router.py` does not change.
- A retry, reopen or resume never lowers a risk tier and never skips a decision: the ticket passes through the same router decisions again.
- A ticket that has a pull request cannot be reopened.
- A reopened ticket's token budget counts only model calls made after the reopen.
- Text a person types with a retry is passed to the implementer as a finding, inside the existing "treat as data" findings prompt.
- Job kinds after this plan, exactly: `run`, `answer`, `continue`, `reopen`, `resume`.

## Review Focus

1. **Retry after a draft pull request exists.** Expected: no second pull request; the same one is updated and marked ready when verify passes. Pinned in Task 2.
2. **Reopening a ticket that failed on its token budget.** Expected: it runs again instead of failing at the first model call. Pinned in Task 4.
3. **Reopening a ticket whose earlier run left state behind** (design round count, old findings, an old pull request number). Expected: the new run starts clean. Pinned in Task 4 (fresh thread).
4. **Resuming a ticket that is actually waiting for an answer, or has nothing to resume.** Expected: refused with a clear message; nothing runs. Pinned in Task 5.
5. **Retrying a blocked merge while the check is still red.** Expected: blocked again with a new request, not merged and not failed. Pinned in Task 3.

## File Structure

| File | Responsibility |
| --- | --- |
| `src/sifu/github.py` (modify) | `mark_ready`: take a pull request out of draft |
| `src/sifu/graph.py` (modify) | `escalate_stuck`, `retry_build`, `retry_merge` nodes; reuse of an existing pull request; `reopen_ticket`; `resume_interrupted`; thread id per attempt |
| `src/sifu/state.py` (modify) | `failed -> intent` |
| `src/sifu/db.py` (modify) | `tokens_used` (budget since the last reopen) |
| `src/sifu/implementer.py` (modify) | `remove_worktree`, `Implementer.discard` |
| `src/sifu/ops.py`, `src/sifu/jobs.py` (modify) | `reopen`, `resume` and their job kinds |
| `src/sifu/ui_data.py`, `src/sifu/ui.py` (modify) | `retry` buttons, Reopen and Resume |
| `scripts/live_probe.py` (modify) | Its `always` scenario now ends paused on a request |

---

### Task 1: `GitHubClient.mark_ready`

**Files:**
- Modify: `src/sifu/github.py`
- Test: `tests/test_github.py`

**Interfaces:**
- Produces: `GitHubClient.mark_ready(self, number: int) -> None` (raises `GitHubError` on failure).

- [ ] **Step 1: Write the failing test** (append to `tests/test_github.py`; read the file first and build the fake repo the way its existing tests do)

```python
def test_mark_ready_takes_the_pull_request_out_of_draft():
    calls = []

    class PR:
        def mark_ready_for_review(self):
            calls.append("ready")

    class Repo:
        def get_pull(self, number):
            calls.append(number)
            return PR()

    GitHubClient(Repo()).mark_ready(7)
    assert calls == [7, "ready"]


def test_mark_ready_wraps_a_failure():
    class Repo:
        def get_pull(self, number):
            raise RuntimeError("404")

    with pytest.raises(GitHubError, match="get pull request failed"):
        GitHubClient(Repo()).mark_ready(7)
```

- [ ] **Step 2: Run and confirm failure.** `uv run --extra agents pytest tests/test_github.py -q` fails with `AttributeError: ... 'mark_ready'`.

- [ ] **Step 3: Implement `mark_ready`** with the same two `self._call(...)` steps as `comment`: `"get pull request"` then `"mark ready"` on `pr.mark_ready_for_review`.

- [ ] **Step 4: Run all checks** (`uv run --extra agents --extra ui pytest -q && uv run ruff check . && uv run ruff format --check .`). Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/sifu/github.py tests/test_github.py
git commit -m "GitHubClient.mark_ready: take a draft pull request out of draft"
```

---

### Task 2: A build that gave up asks a person, and `retry` runs Build again

**Files:**
- Modify: `src/sifu/graph.py`, `src/sifu/ui_data.py` (`KEY_KINDS`, `COMMANDS`), `scripts/live_probe.py`
- Test: `tests/test_verify_flow.py`, `tests/test_pr_flow.py`

**Interfaces:**
- Consumes: Task 1's `mark_ready`.
- Produces:
  - Graph nodes `escalate_stuck` and `retry_build`.
  - A human request with `options == ["retry", "retry with a note", "close"]` for a stuck build.
  - `TicketState["wait_kind"] == "stuck"` while it waits.
  - Events: `build_retried` (actor `human`, payload `{"note": str}`), `pr_updated` and `draft_pr_updated` (actor `supervisor`, payload `{"number", "url", "sha"}`).
  - `ui_data.COMMANDS == ("approve", "close", "retry")`.

How it must work (read `verify`, `after_verify`, `critic`, `after_critic`, `draft_pr`, `open_pr`, `await_human`, `after_answer` in `graph.py` first):

- `escalate_stuck(state)`: the ticket is already `needs_human` (the node that gave up moved it), so do **not** call `deps.move`. Open a request and return `{"wait_kind": "stuck", "request_id": request_id, "status": "needs_human"}`. The question is one line:
  `f"Build stopped after {state.get('build_round', 0)} rounds ({state.get('outcome')}); {len(state.get('findings') or [])} findings remain."`, then `f" Draft pull request: {state['pr_url']}."` when `pr_url` is set, then `" Answer retry to run Build again (add a note to guide it), or close."`.
- Edges: `draft_pr -> escalate_stuck` (replacing `draft_pr -> END`). In `after_verify` and `after_critic`, the branch that returns `END` when `status == "needs_human"` and the branch cannot publish returns `"escalate_stuck"` instead. `escalate_stuck -> await_human`. Add `"escalate_stuck"` to the path lists of the `verify` and `critic` conditional edges.
- `after_answer`: before the existing checks, `if state["wait_kind"] == "stuck": return "close_ticket" if answer.startswith("close") else "retry_build"`. Add `"retry_build"` to the `await_human` path list.
- `retry_build(state)`: `note` is the answer with a leading `retry` removed (case-insensitive) and then `strip(" :,-\n")`. Log `build_retried` with `{"note": note[:500]}`. Return `{"findings": findings, "build_round": 0, "fingerprint": "", "outcome": "", "status": "build"}` where `findings` is the state's findings plus, when `note` is not empty, `{"source": "human", "message": note, "file": "", "line": None, "rule": "note"}`. Edge `retry_build -> build`. (`build` itself moves the ticket `needs_human -> build`; the facts already hold `spec_accepted` and `plan_accepted`.)
- `draft_pr` when `state.get("pr_number")` is already set: push with `deps.publisher`, post the findings comment on that number, log `draft_pr_updated`, return `{"head_sha": sha}`. Do not call `create_pr`.
- `open_pr` when `state.get("pr_number")` is already set: push, `deps.github.mark_ready(number)`, comment `"Sifu: the checks now pass after a retry; this pull request is ready for review."`, log `pr_updated`, call `post_critic_notes`, return `{"head_sha": sha}`. Failures go through the existing `fail(tid, "pr_failed", exc)` path.

- [ ] **Step 1: Update the tests that expect the old ending**

- `tests/test_verify_flow.py::test_the_iteration_cap_sends_the_ticket_to_a_human` and `::test_the_same_findings_twice_stop_early_for_lack_of_progress`: add `assert is_paused(state)` and

```python
    [request] = db.open_requests(conn)
    assert json.loads(request["options"]) == ["retry", "retry with a note", "close"]
    assert "Build stopped after" in request["question"]
```

  (import `is_paused` and `resume_ticket` from `sifu.graph`).
- `tests/test_pr_flow.py::test_a_verify_cap_opens_a_draft_pr_with_the_findings` and `::test_a_draft_pr_failure_leaves_the_ticket_waiting_for_a_human`: add `assert is_paused(state)` and that exactly one request is open; in the first, that its question contains `"https://github.com/o/r/pull/7"`.
- Add `mark_ready` to `FakeGitHub` in `tests/test_pr_flow.py`: `self.ready = []` in `__init__`, and `def mark_ready(self, number): self.ready.append(number)`.

- [ ] **Step 2: Add the new failing tests**

In `tests/test_verify_flow.py`:

```python
def test_retry_runs_build_again_with_the_findings_and_a_fresh_round_count(
    conn, cfg, make_deps, make_ticket
):
    cfg.router.max_build_iterations = 2
    impl = Implementer()
    ver = Verifier(failing("a"), failing("b"), PASSING)
    deps, ticket, state = run(make_deps, make_ticket, impl, ver)
    assert is_paused(state) and len(impl.calls) == 2
    final = resume_ticket(deps, ticket, "retry")
    assert final["status"] == "done" and len(impl.calls) == 3
    assert impl.calls[2]["feedback"][0]["message"] == "b"  # the findings it stopped on
    assert db.open_requests(conn) == []
    assert "build_retried" in [e["kind"] for e in db.list_events(conn, ticket)]


def test_a_note_with_the_retry_reaches_the_implementer_as_a_finding(
    conn, cfg, make_deps, make_ticket
):
    cfg.router.max_build_iterations = 1
    impl = Implementer()
    deps, ticket, _ = run(make_deps, make_ticket, impl, Verifier(failing("a"), PASSING))
    resume_ticket(deps, ticket, "retry: use trimmingCharacters, not a regex")
    assert impl.calls[1]["feedback"][-1] == {
        "source": "human",
        "message": "use trimmingCharacters, not a regex",
        "file": "",
        "line": None,
        "rule": "note",
    }


def test_a_retry_that_gives_up_again_asks_again(conn, cfg, make_deps, make_ticket):
    cfg.router.max_build_iterations = 1
    deps, ticket, _ = run(make_deps, make_ticket, Implementer(), Verifier(failing("a"), failing("b")))
    state = resume_ticket(deps, ticket, "retry")
    assert is_paused(state) and len(db.open_requests(conn)) == 1


def test_closing_a_stuck_ticket_fails_it(conn, cfg, make_deps, make_ticket):
    cfg.router.max_build_iterations = 1
    deps, ticket, _ = run(make_deps, make_ticket, Implementer(), Verifier(failing("a")))
    resume_ticket(deps, ticket, "close")
    assert db.get_ticket(conn, ticket)["status"] == "failed"
    assert "ticket_closed" in [e["kind"] for e in db.list_events(conn, ticket)]
```

In `tests/test_pr_flow.py`:

```python
def test_a_retry_after_a_draft_pr_updates_that_pr_and_marks_it_ready(
    conn, cfg, make_deps, make_ticket
):
    cfg.router.max_build_iterations = 1
    cfg.router.merge_mode = "manual"
    deps = make_deps(FakeLLM())
    deps.implementer = Implementer()
    results = iter(
        [VerifyResult(False, [Finding("swiftlint", "Too long", "Sources/Trip.swift", 4, "len")])]
    )
    deps.verifier = lambda tid, spec: next(results, VerifyResult(True))
    gh = FakeGitHub()
    created = []
    original = gh.create_pr
    gh.create_pr = lambda **kw: created.append(kw["draft"]) or original(**kw)
    deps.github, deps.publisher, deps.sleep = gh, publisher, lambda s: None
    ticket = make_ticket("T", BODY)
    assert is_paused(run_ticket(deps, ticket, facts={"plan_accepted": True}))
    state = resume_ticket(deps, ticket, "retry")
    assert created == [True]  # one pull request, opened as a draft, never a second
    assert gh.ready == [7]
    assert "pr_updated" in kinds(conn, ticket)
    assert is_paused(state)  # manual merge mode: now waiting on the merge decision
```

- [ ] **Step 3: Run and confirm failure.** `uv run --extra agents pytest tests/test_verify_flow.py tests/test_pr_flow.py -q`. Expected: the updated and new tests fail (no open request; `KeyError`/assertion on `is_paused`).

- [ ] **Step 4: Implement the graph changes** listed under "How it must work". Register the two nodes in the `for name, fn in {...}` dict.

- [ ] **Step 5: `ui_data.py`**: `COMMANDS = ("approve", "close", "retry")`; add `"build_retried"`, `"pr_updated"`, `"draft_pr_updated"` to `KEY_KINDS`; in `_summarise` give `pr_updated` and `draft_pr_updated` the same summary as `pr_opened`, and `build_retried` the summary `p.get("note") or "no note"`.

- [ ] **Step 6: `scripts/live_probe.py`**: read the `always` scenario's PASS/FAIL checklist. Where it checks that the ticket ended in Needs Human with a draft PR, keep that and add a check that one request with `retry` in its options is open. Update the module docstring line "The ticket stays in Needs Human." to "The ticket waits in Needs Human on a retry-or-close request."

- [ ] **Step 7: Run all checks.** Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/sifu/graph.py src/sifu/ui_data.py scripts/live_probe.py tests/test_verify_flow.py tests/test_pr_flow.py
git commit -m "A build that gave up opens a retry-or-close request; a retry reuses its draft pull request"
```

---

### Task 3: A blocked merge asks a person, and `retry` tries the merge again

**Files:**
- Modify: `src/sifu/graph.py`, `src/sifu/ui_data.py` (`KEY_KINDS`)
- Test: `tests/test_pr_flow.py`

**Interfaces:**
- Consumes: Task 2's `escalate_stuck` and `wait_kind == "stuck"`.
- Produces: node `retry_merge`; event `merge_retried` (actor `human`, empty payload); for a blocked merge the request's `options == ["retry", "close"]`.

How it must work:

- `merge` already moves the ticket to `needs_human` and returns `{"status": "needs_human", "outcome": "merge_blocked"}` when the check is not green. Replace `graph.add_edge("merge", END)` with a conditional edge: `"escalate_stuck"` when `state.get("outcome") == "merge_blocked" and state.get("status") == "needs_human"`, else `END`.
- `escalate_stuck`: when `state.get("outcome") == "merge_blocked"`, the question is `f"The merge of {state.get('pr_url')} was blocked because the required check is not green. Answer retry once the check passes, or close."` and the options are `["retry", "close"]`.
- `after_answer` for `wait_kind == "stuck"`: `close` -> `close_ticket`; otherwise `"retry_merge"` when `state.get("outcome") == "merge_blocked"`, else `"retry_build"`.
- `retry_merge(state)`: `deps.move(tid, TicketStatus.TEST, state.get("facts", {}))`, log `merge_retried`, return `{"status": "test", "outcome": ""}`. Edge `retry_merge -> merge`.
- `merge` must clear the outcome when it succeeds only if it set it: it already returns `{"facts", "status": "done"}`; leave that.

- [ ] **Step 1: Update `test_approving_while_the_check_is_still_red_blocks_the_merge`** in `tests/test_pr_flow.py`: after the existing assertions add `assert is_paused(final)` and that the one open request has `json.loads(request["options"]) == ["retry", "close"]`.

- [ ] **Step 2: Add the new failing tests**

```python
def test_retrying_a_blocked_merge_merges_once_the_check_is_green(conn, make_deps, make_ticket):
    gh = FakeGitHub(ci=("failure", "failure", "success"))
    deps, ticket, _ = run(make_deps, make_ticket, gh)
    resume_ticket(deps, ticket, "approve")  # still red: blocked
    final = resume_ticket(deps, ticket, "retry")
    assert final["status"] == "done" and len(gh.merges) == 1
    assert "merge_retried" in kinds(conn, ticket)
    assert db.get_ticket(conn, ticket)["status"] == "done"


def test_retrying_while_the_check_is_still_red_blocks_again(conn, make_deps, make_ticket):
    gh = FakeGitHub(ci=("failure",))
    deps, ticket, _ = run(make_deps, make_ticket, gh)
    resume_ticket(deps, ticket, "approve")
    final = resume_ticket(deps, ticket, "retry")
    assert is_paused(final) and gh.merges == []
    assert db.get_ticket(conn, ticket)["status"] == "needs_human"
    assert len(db.open_requests(conn)) == 1


def test_closing_a_blocked_merge_fails_the_ticket_without_merging(conn, make_deps, make_ticket):
    gh = FakeGitHub(ci=("failure",))
    deps, ticket, _ = run(make_deps, make_ticket, gh)
    resume_ticket(deps, ticket, "approve")
    resume_ticket(deps, ticket, "close")
    assert db.get_ticket(conn, ticket)["status"] == "failed" and gh.merges == []
```

Count the `ci` values against the calls: `merge_decision` polls once (via `wait_for_ci`), each `merge` attempt polls once. Adjust the tuple in the first test if `FakeGitHub.ci_status` is called a different number of times; the property to pin is "red at the first merge attempt, green at the retry".

- [ ] **Step 3: Run and confirm failure**, then **Step 4: implement**, then **Step 5: add `"merge_retried"` to `KEY_KINDS`.**

- [ ] **Step 6: Run all checks.** Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/sifu/graph.py src/sifu/ui_data.py tests/test_pr_flow.py
git commit -m "A merge blocked by a red check opens a retry-or-close request"
```

---

### Task 4: Reopen a failed ticket

**Files:**
- Modify: `src/sifu/state.py`, `src/sifu/db.py`, `src/sifu/implementer.py`, `src/sifu/graph.py`, `src/sifu/ui_data.py` (`KEY_KINDS`)
- Test: `tests/test_state.py`, `tests/test_db.py`, `tests/test_implementer.py`, `tests/test_router_flow.py`

**Interfaces:**
- Produces:
  - `TRANSITIONS[S.FAILED] == frozenset({S.INTENT})`
  - `db.tokens_used(conn, ticket_id: int) -> int`
  - `implementer.remove_worktree(repo: Path, worktrees_dir: Path, ticket_id: int, tag: str | None = None) -> bool`
  - `Implementer.discard(self, ticket_id: int) -> bool`
  - `graph.reopen_ticket(deps: Deps, ticket_id: int, facts: dict[str, bool] | None = None, prompts_dir: Path = PROMPTS_DIR) -> dict[str, Any]`
  - Event `ticket_reopened` (actor `human`, payload `{"runs_before": int}`).
  - Thread ids: `ticket-<id>` for a ticket never reopened, `ticket-<id>-r<n>` after its n-th reopen.

- [ ] **Step 1: State machine tests** in `tests/test_state.py`: replace `test_terminal_states_have_no_exits` with

```python
def test_done_is_terminal_and_failed_can_only_be_reopened():
    assert TRANSITIONS[S.DONE] == frozenset()
    assert TRANSITIONS[S.FAILED] == frozenset({S.INTENT})
    assert_transition(S.FAILED, S.INTENT)
    with pytest.raises(InvalidTransition):
        assert_transition(S.FAILED, S.BUILD, {"spec_accepted": True, "plan_accepted": True})
```

Run it, see it fail, change `S.FAILED: frozenset()` to `S.FAILED: frozenset({S.INTENT})` in `state.py`, see it pass.

- [ ] **Step 2: Budget test** in `tests/test_db.py`

```python
def test_tokens_used_counts_only_calls_since_the_last_reopen(conn, ticket):
    db.record_run(conn, "reviewer", "m", ticket, 100, 50)
    db.record_run(conn, "reviewer", "m", ticket, None, None)  # missing counts are zero
    assert db.tokens_used(conn, ticket) == 150
    before = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
    db.append_event(conn, ticket, "ticket_reopened", "human", {"runs_before": before})
    assert db.tokens_used(conn, ticket) == 0
    db.record_run(conn, "reviewer", "m", ticket, 10, 5)
    assert db.tokens_used(conn, ticket) == 15
```

Implement `tokens_used`: the baseline is `payload["runs_before"]` of the ticket's newest `ticket_reopened` event, else 0; sum `COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0)` over the ticket's runs with `id > baseline`. Then change `Deps.check_budget` in `graph.py` to use `db.tokens_used(self.conn, ticket_id)` in place of its inline query (keep the lock).

- [ ] **Step 3: Worktree removal test** in `tests/test_implementer.py` (it has a `repo` fixture, a temporary git repo with one commit, and a `git(repo, *args)` helper)

```python
def test_remove_worktree_deletes_the_worktree_and_its_branch(repo, tmp_path):
    worktrees = tmp_path / "wt"
    path, branch = create_worktree(repo, worktrees, 5, "d")
    assert remove_worktree(repo, worktrees, 5, "d") is True
    assert not path.exists()
    assert branch not in git_output(repo, "branch", "--list", branch)
    assert remove_worktree(repo, worktrees, 5, "d") is False  # nothing left to remove
```

Import `remove_worktree` and `git_output` from `sifu.implementer` if the file does not already. Implement `remove_worktree` beside `create_worktree` with the same naming rule: when the path does not exist return False; else `git worktree remove --force <path>`, then `git branch -D <branch>` ignoring a failure of the branch delete, and return True. `Implementer.discard(ticket_id)` calls it with the instance's config paths and tag.

- [ ] **Step 4: Reopen tests** (append to `tests/test_router_flow.py`; import `reopen_ticket`)

```python
def failed_ticket(deps, conn, make_ticket):
    """A ticket closed by a person at its spec request: status failed, with old state."""
    ticket = make_ticket("T", PROTECTED_BODY)
    run_ticket(deps, ticket)
    resume_ticket(deps, ticket, "close")
    assert status(conn, ticket) == "failed"
    return ticket


def test_reopening_a_failed_ticket_runs_the_design_stage_again_from_a_clean_state(
    conn, make_ticket, make_deps
):
    deps = make_deps(FakeLLM())
    ticket = failed_ticket(deps, conn, make_ticket)
    state = reopen_ticket(deps, ticket)
    assert is_paused(state) and status(conn, ticket) == "needs_human"  # asks again, as the first time
    assert state.get("round", 0) == 0 and "answer" not in state  # nothing carried over
    kinds = [e["kind"] for e in db.list_events(conn, ticket)]
    assert kinds.count("design_started") == 2 and "ticket_reopened" in kinds
    assert len(db.open_requests(conn)) == 1


def test_a_reopened_ticket_can_be_answered_and_reopened_again(conn, make_ticket, make_deps):
    deps = make_deps(FakeLLM())
    ticket = failed_ticket(deps, conn, make_ticket)
    reopen_ticket(deps, ticket)
    resume_ticket(deps, ticket, "close")  # resume finds the new thread
    assert status(conn, ticket) == "failed"
    assert is_paused(reopen_ticket(deps, ticket))


def test_a_reopened_ticket_gets_a_fresh_token_budget(conn, cfg, make_ticket, make_deps):
    deps = make_deps(FakeLLM())
    ticket = failed_ticket(deps, conn, make_ticket)
    cfg.router.max_tokens_per_ticket = db.tokens_used(conn, ticket)  # already spent
    assert is_paused(reopen_ticket(deps, ticket))  # not failed at the first model call


def test_only_a_failed_ticket_without_a_pull_request_can_be_reopened(conn, make_ticket, make_deps):
    deps = make_deps(FakeLLM())
    fine = make_ticket("T", BODY)
    with pytest.raises(ValueError, match="not failed"):
        reopen_ticket(deps, fine)
    ticket = failed_ticket(deps, conn, make_ticket)
    db.append_event(conn, ticket, "pr_opened", "supervisor", {"number": 7, "url": "u"})
    with pytest.raises(ValueError, match="pull request"):
        reopen_ticket(deps, ticket)
    assert status(conn, ticket) == "failed"


def test_reopening_discards_the_old_worktree(conn, make_ticket, make_deps):
    deps = make_deps(FakeLLM())
    discarded = []

    class Impl:
        def discard(self, ticket_id):
            discarded.append(ticket_id)

    deps.implementer = Impl()
    ticket = failed_ticket(deps, conn, make_ticket)
    reopen_ticket(deps, ticket)
    assert discarded == [ticket]
```

- [ ] **Step 5: Run and confirm failure.** `ImportError: cannot import name 'reopen_ticket'`.

- [ ] **Step 6: Implement in `graph.py`**

- Replace `_thread(ticket_id)` with `_thread(deps: Deps, ticket_id: int)`. It counts the ticket's `ticket_reopened` events (under `deps.lock`): 0 gives `ticket-<id>`, n gives `ticket-<id>-r<n>`. Update every caller: `_invoke`, `resume_ticket`, `continue_ticket` (and `resume_interrupted` in Task 5). `grep -rn "_thread(" src tests scripts` must show no call with one argument afterwards.
- `reopen_ticket`:
  1. status must be `failed`, else `ValueError(f"ticket {ticket_id} is {status}, not failed")`;
  2. if the ticket has a `pr_opened` or `draft_pr_opened` event, `ValueError(f"ticket {ticket_id} has a pull request; create a new ticket instead")`;
  3. `discard = getattr(deps.implementer, "discard", None)`; call it when present;
  4. `runs_before = SELECT COALESCE(MAX(id), 0) FROM runs`;
  5. `deps.move(ticket_id, TicketStatus.INTENT)`, then `deps.log(ticket_id, "ticket_reopened", "human", {"runs_before": runs_before})`;
  6. `return run_ticket(deps, ticket_id, facts, prompts_dir)`. Because the reopened event now exists, `_thread` gives a new thread id and the run starts with empty state.
- Add `"ticket_reopened"` to `KEY_KINDS` in `ui_data.py`.

- [ ] **Step 7: Run all checks.** Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/sifu/state.py src/sifu/db.py src/sifu/implementer.py src/sifu/graph.py src/sifu/ui_data.py tests
git commit -m "A failed ticket without a pull request can be reopened: fresh thread, fresh budget, old worktree removed"
```

---

### Task 5: Resume a run that was cut off

**Files:**
- Modify: `src/sifu/graph.py`
- Test: `tests/test_router_flow.py`

**Interfaces:**
- Produces: `graph.resume_interrupted(deps: Deps, ticket_id: int, prompts_dir: Path = PROMPTS_DIR) -> dict[str, Any]`; event `run_resumed` (actor `human`, payload `{"next": list[str]}`).

- [ ] **Step 1: Write the failing tests** (import `build_graph`, `resume_interrupted`, `_thread`)

```python
def test_a_run_cut_off_before_build_is_resumed_from_its_checkpoint(conn, make_ticket, make_deps):
    deps = make_deps(FakeLLM())
    ticket = make_ticket("T", BODY)
    # stop the graph just before Build, as a killed process would leave it
    build_graph(deps).invoke(
        {"ticket_id": ticket, "facts": {"plan_accepted": True}},
        _thread(deps, ticket),
        interrupt_before=["build"],
    )
    assert status(conn, ticket) == "spec"
    state = resume_interrupted(deps, ticket)
    assert state["status"] == "done" and status(conn, ticket) == "done"
    kinds = [e["kind"] for e in db.list_events(conn, ticket)]
    assert kinds.count("design_started") == 1 and "run_resumed" in kinds  # not restarted


def test_resume_refuses_a_ticket_waiting_for_an_answer_or_with_nothing_left(
    conn, make_ticket, make_deps
):
    deps = make_deps(FakeLLM())
    waiting = make_ticket("T", PROTECTED_BODY)
    run_ticket(deps, waiting)
    with pytest.raises(ValueError, match="waiting for an answer"):
        resume_interrupted(deps, waiting)
    finished = make_ticket("T2", BODY)
    run_ticket(deps, finished)
    with pytest.raises(ValueError, match="nothing to resume"):
        resume_interrupted(deps, finished)
    assert len(db.open_requests(conn)) == 1  # nothing ran
```

- [ ] **Step 2: Run and confirm failure**, then **Step 3: implement**:

```python
graph = build_graph(deps, prompts_dir)
snapshot = graph.get_state(_thread(deps, ticket_id))
if snapshot.interrupts:
    raise ValueError(f"ticket {ticket_id} is waiting for an answer, not cut off")
if not snapshot.next:
    raise ValueError(f"ticket {ticket_id} has nothing to resume")
deps.log(ticket_id, "run_resumed", "human", {"next": list(snapshot.next)})
return _invoke(deps, ticket_id, None, prompts_dir)
```

- [ ] **Step 4: Run all checks; commit**

```bash
git add src/sifu/graph.py tests/test_router_flow.py
git commit -m "resume_interrupted: a run cut off mid-graph continues from its last checkpoint"
```

---

### Task 6: `ops`, job kinds and the app's buttons

**Files:**
- Modify: `src/sifu/ops.py`, `src/sifu/jobs.py`, `src/sifu/ui_data.py`, `src/sifu/ui.py`, `README.md`
- Test: `tests/test_ops.py`, `tests/test_jobs.py`, `tests/test_ui_data.py`, `tests/test_ui.py`

**Interfaces:**
- Consumes: Tasks 2 to 5; Plan 1's `ops.open_deps`, `jobs.submit`, `ui_data.last_job`, `ui_data.active_job`.
- Produces:
  - `ops.reopen(db_path, ticket_id: int, *, live: bool, cfg=None, llm=None) -> str`
  - `ops.resume(db_path, ticket_id: int, *, live: bool, cfg=None, llm=None) -> str`
  - `jobs.HANDLERS["reopen"] = ops.reopen`, `jobs.HANDLERS["resume"] = ops.resume`; payloads `{"ticket_id": int, "live": bool}` for both.
  - `ui_data.can_reopen(conn, ticket_id: int) -> bool`
  - Each `ui_data.inbox()` item gains `"takes_note": bool` (True when `"retry with a note"` is in its options).
  - Widget keys: `ticket_reopen`, `ticket_resume`, `note-<request id>`.

- [ ] **Step 1: `ops` tests** (append to `tests/test_ops.py`, using its `home` fixture)

```python
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
```

Implement both like `ops.run`: `open_deps`, call the graph function, return the status, close the connection. `reopen` passes the same `facts` rule as `ops.run`.

- [ ] **Step 2: `jobs` test** (append to `tests/test_jobs.py`)

```python
def test_reopen_and_resume_are_job_kinds():
    assert set(jobs.HANDLERS) == {"run", "answer", "continue", "reopen", "resume"}
```

- [ ] **Step 3: `ui_data` tests**

```python
def test_can_reopen_is_a_failed_ticket_without_a_pull_request(conn, project):
    failed = db.create_ticket(conn, project, "Rename a Trip")
    db.set_status(conn, failed, TicketStatus.FAILED)
    with_pr = db.create_ticket(conn, project, "Limit a Day Plan")
    db.append_event(conn, with_pr, "draft_pr_opened", "supervisor", {"number": 2, "url": PR_URL})
    db.set_status(conn, with_pr, TicketStatus.FAILED)
    alive = db.create_ticket(conn, project, "Remove a Stop")
    assert [ui_data.can_reopen(conn, t) for t in (failed, with_pr, alive)] == [True, False, False]


def test_a_stuck_build_request_offers_retry_and_close_and_takes_a_note(conn, project):
    tid = db.create_ticket(conn, project, "Rename a Trip")
    db.append_event(conn, tid, "implementer_result", "implementer", {"ok": True})
    db.open_human_request(conn, tid, "Build stopped", ["retry", "retry with a note", "close"])
    [item] = ui_data.inbox(conn)
    assert item["actions"] == ["retry", "close"] and item["takes_note"] is True
    assert item["needs_live_tools"] is True
```

- [ ] **Step 4: UI tests** (append to `tests/test_ui.py`; they use Plan 1's `no_real_work`, `ready` and `open_ticket` helpers)

```python
def test_a_stuck_build_can_be_retried_with_a_note(database, monkeypatch, no_real_work):
    conn = db.connect(database)
    ticket = db.create_ticket(conn, 1, "Remove a Stop")
    db.append_event(conn, ticket, "implementer_result", "implementer", {"ok": True})
    db.set_status(conn, ticket, TicketStatus.NEEDS_HUMAN)
    request = db.open_human_request(
        conn, ticket, "Build stopped after 3 rounds", ["retry", "retry with a note", "close"]
    )
    conn.close()
    ready(monkeypatch)
    at = run("Inbox")
    at.text_input(key=f"note-{request}").set_value("use trimmingCharacters").run()
    next(b for b in at.button if b.key == f"answer-{request}-retry").click().run()
    assert no_real_work == [
        ("answer", {"request_id": request, "answer": "retry: use trimmingCharacters", "live": True}, ticket)
    ]


def test_a_failed_ticket_offers_reopen_unless_it_has_a_pull_request(database, no_real_work):
    conn = db.connect(database)
    ticket = db.create_ticket(conn, 1, "Remove a Stop")
    db.set_status(conn, ticket, TicketStatus.FAILED)
    conn.close()
    at = open_ticket(ticket)
    at.button(key="ticket_reopen").click().run()
    assert no_real_work == [("reopen", {"ticket_id": ticket, "live": False}, ticket)]
    conn = db.connect(database)
    db.append_event(conn, ticket, "pr_opened", "supervisor", {"number": 9, "url": PR_URL})
    conn.close()
    assert not [b for b in open_ticket(ticket).button if b.key == "ticket_reopen"]


def test_an_interrupted_run_offers_resume_in_the_mode_it_was_started_in(database, no_real_work):
    conn = db.connect(database)
    job = db.create_job(conn, "run", {"ticket_id": 1, "live": True}, 1)
    db.start_job(conn, job)
    db.interrupt_stale_jobs(conn)
    conn.close()
    at = open_ticket(1)
    assert "The last run was cut off when the app stopped." in [w.value for w in at.warning]
    at.button(key="ticket_resume").click().run()
    assert no_real_work == [("resume", {"ticket_id": 1, "live": True}, 1)]
```

- [ ] **Step 5: Run and confirm failure**, then **Step 6: implement**:

- `ui_data.can_reopen`: status is `failed` and no `pr_opened`/`draft_pr_opened` event. `inbox`: add `takes_note`.
- Inbox (`show_inbox`): when `item["takes_note"]`, render `st.text_input("Note for the implementer (optional)", key=f"note-{item['id']}")` above the buttons; the `retry` button sends `f"retry: {note.strip()}"` when the note is not blank, else `"retry"`. For an item whose options include `retry`, do not render the generic free-text reply.
- Ticket tab: when the ticket has no active job and `ui_data.can_reopen`, a button "Reopen" (`key="ticket_reopen"`) submitting `("reopen", {"ticket_id": id, "live": live})`. When `ui_data.last_job` is `interrupted` and there is no active job, a button "Resume" (`key="ticket_resume"`) under Plan 1's warning, submitting `("resume", {"ticket_id": id, "live": <the interrupted job's payload "live", default True when the key is absent>})`. Both handle `db.JobConflict` like `_answer` does.
- When build tools are missing and the interrupted job's payload has `live: true`, show the Resume button disabled with the caption `"Resume needs: " + "; ".join(_missing())`.

- [ ] **Step 7: README**: in "Streamlit UI" add a short paragraph "When something stops": the retry-or-close request for a build that gave up and for a blocked merge; Reopen for a failed ticket (not available once it has a pull request; its token budget starts again); Resume after the app was restarted mid-run. In "Router, pause and resume" add one sentence that `failed -> intent` is the one way out of `failed`.

- [ ] **Step 8: Run all checks.** Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add src/sifu/ops.py src/sifu/jobs.py src/sifu/ui_data.py src/sifu/ui.py README.md tests
git commit -m "Streamlit UI: retry a stuck build or blocked merge, reopen a failed ticket, resume a cut-off run"
```

---

## After the last task

- Paste the output of the three check commands in the hand-off.
- Not covered by automated tests, for Lance to check on his machine: `scripts/live_probe.py always` ends on a retry-or-close request; answering `retry` from the app updates the same draft pull request on GitHub.
