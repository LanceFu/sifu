# Sifu

Sifu moves software tickets through **Plan, Design, Build, Test** using a LangGraph agent pipeline
on local Ollama models. A separate risk router decides which decisions are automatic and which need a human.
Course project for Agentic AI (University of Waterloo). The target product is the fictional
[`tripplanner-ios`](https://github.com/LanceFu/tripplanner-ios) repo, so no real data is involved.

## Status

| Piece | State |
| --- | --- |
| Config, models, SQLite schema, state machine, risk router floor rules | Done |
| Design stage (deterministic checks, three parallel reviewers, synthesizer) | Done; run on real Ollama |
| Risk router (two assessors), pause and resume, lessons, FastMCP tools, caps | Done; run on real Ollama |
| Planner, headless Claude Code implementer, verify loop, GitHub PR and merge flow | Done; run live on `tripplanner-ios` (PRs #2 to #5) |
| LLM critic pass | Done; off by default (`critic.enabled`) |
| Evaluation (golden tickets against a single-prompt baseline, golden diffs for the critics) | Done; results in the notebook |
| Streamlit UI: draft, run, build, merge, recover and operate from the app (board, ticket, inbox, settings) | Done; see Streamlit UI |

## Setup

This project uses [uv](https://docs.astral.sh/uv/) for Python versions, dependencies and running commands.

```bash
uv sync                      # creates .venv, installs the project and the dev group
uv run pytest
uv run ruff check .
uv sync --extra agents       # LangGraph, Ollama, FastMCP and GitHub libraries
uv sync --extra ui           # Streamlit
uv add <package>             # add a dependency; commit pyproject.toml and uv.lock
```

## Secrets

The GitHub token lives in the macOS Keychain, not in the repo. Create a fine-grained token for
`tripplanner-ios` only (Contents and Pull requests read/write, no Workflows or Administration), then:

```bash
security add-generic-password -a "$USER" -s sifu-github-token -w   # prompts for the token
```

`sifu.credentials.get_github_token()` uses `$SIFU_GITHUB_TOKEN` if it is set, otherwise the Keychain item
named by `github.keychain_service` in `config.yaml`. macOS may ask you to allow access the first time.

## Design rules

- Code owns state transitions (`state.py`) and the risk floor (`router.py`). An LLM assessor can
  raise a decision's tier, never lower it below the floor. A missing assessment escalates to a human.
- Models and the implementer backend come from `config.yaml`. The implementer is Claude Code in headless mode:
  `backend: cloud` by default, or `backend: ollama` with a local model such as `qwen3-coder:30b`.
- `merge_mode` is `manual`, `risk_based` or `auto`. Floor rules apply in all three.
- Ticket text, repo contents and PR comments are untrusted input.

## Layout

```
config.yaml            every setting; see Configuration
src/sifu/models.py     enums and Pydantic models for findings, assessments and decisions
src/sifu/state.py      ticket state machine and stage gates
src/sifu/router.py     floor rules and the final tier
src/sifu/db.py         SQLite tables: projects, tickets, events, decisions, human_requests, runs,
                       chat_messages, jobs
src/sifu/graph.py      the LangGraph ticket graph: every node and edge
src/sifu/reviewers.py, synthesizer.py, planner.py, critic.py, assessors.py   the model agents
src/sifu/implementer.py, verify.py, pr_flow.py, github.py                    Build, Test and merge
src/sifu/evalset.py, evalrun.py, baseline.py, golden.py, report.py           evaluation
src/sifu/evidence.py   notebook tables read from a recorded database
src/sifu/brainstorm.py the intent and spec chats: a model that talks and drafts, with no tools
src/sifu/live.py       the real implementer, verifier and GitHub wiring, and the check for what is missing
src/sifu/ops.py        every write the app can ask for (create, run, answer, continue), by database path
src/sifu/jobs.py       one background worker that runs those operations and records them in `jobs`
src/sifu/ui_data.py, ui.py   the Streamlit UI and the queries behind it
src/sifu/prompts/      persona, rubric and few-shot files for each agent
scripts/               live runs, probes and the evaluation harness
golden/, seed/         labelled tickets and diffs
tests/                 pytest suite (fake models, no network)
notebooks/             demo notebook for the course submission
```

## Configuration

Everything is set in `config.yaml` and validated by `src/sifu/config.py`. Options marked
"default" may be left out of the file.

| Option | Default | Meaning |
| --- | --- | --- |
| `ollama.base_url` | `http://localhost:11434` | Where Ollama runs. |
| `ollama.num_ctx` | `65536` | Context window for every model call. |
| `ollama.num_predict` | `8192` | Most output tokens per reply, thinking included. Stops a looping model. |
| `models.author` | required | Model for the spec writer and the planner. |
| `models.spec_reviewer` | required | Model for the three Design-stage reviewers. |
| `models.code_reviewer` | required | Model for the critics. Use a different family from the author. |
| `models.risk_assessors` | required | One or more models that assess each decision. Two families make agreement a real signal. |
| `implementer.backend` | `cloud` | `cloud` or `ollama`. `SIFU_IMPLEMENTER_BACKEND` overrides it for one run. |
| `implementer.cloud_model` | none | Claude Code model for the cloud backend; none means its default. |
| `implementer.ollama_model` | `qwen3-coder:30b` | Local model for the `ollama` backend. |
| `implementer.max_turns` | `30` | Turn cap for one headless Claude Code run. |
| `implementer.timeout_seconds` | `1800` | Time limit for one implementer run. |
| `implementer.allowed_tools` | empty | Tools Claude Code may use; anything else is denied. |
| `planner.enabled` | `false` | Write `plan.md` after the spec is accepted and gate Build on it. |
| `planner.listing_cap` | `150` | File names shown to the planner. |
| `planner.excerpt_chars` | `3000` | Characters shown per file. |
| `planner.total_chars` | `15000` | Characters of excerpts in total. |
| `critic.enabled` | `false` | Run the LLM critics after verify passes. |
| `critic.names` | `edge_cases, contract, security` | Which critics run. |
| `critic.max_diff_chars` | `24000` | Diff size shown to a critic. |
| `critic.min_scenario_words` | `6` | Shortest failing scenario a finding may give. |
| `critic.confirm_blockers` | `true` | A second model re-reads each blocker against nearby lines. |
| `critic.confirm_model` | none | Model for that check; none means `models.author`. |
| `critic.context_lines` | `6` | Unchanged lines shown around each change. |
| `verify.steps` | empty | Commands Sifu runs after each implementer run, in order: `name`, `command`, `parser` (`compiler`, `swiftlint_json` or `generic`). Empty makes the stage a stub. |
| `verify.timeout_seconds` | `900` | Time limit per step. |
| `verify.max_findings` | `20` | Findings sent back to the implementer per round. |
| `verify.extra_allowed_paths` | `Tests/` | Paths the implementer may touch beyond the spec's list. |
| `router.merge_mode` | `risk_based` | `manual`, `risk_based` or `auto`. Floor rules apply in every mode. |
| `router.max_build_iterations` | `3` | Build and verify rounds before the ticket goes to a human. |
| `router.max_design_rounds` | `3` | Clarification rounds before the ticket fails. |
| `router.max_tokens_per_ticket` | `400000` | Model tokens per ticket before it fails. |
| `router.protected_paths` | empty | Files and folders that always need a human. A trailing slash covers a folder. |
| `github.repo` | required | The product repository, `owner/name`. |
| `github.token_env` | `SIFU_GITHUB_TOKEN` | Environment variable checked first for the token. |
| `github.keychain_service` | `sifu-github-token` | macOS Keychain item used otherwise. |
| `github.base_branch` | `main` | Branch pull requests target. |
| `github.required_check` | `test` | The Actions check Sifu waits for before the merge decision. |
| `github.ci_timeout_seconds` | `600` | How long to wait for that check. |
| `github.ci_poll_seconds` | `15` | How often to poll it. |
| `github.merge_method` | `squash` | `squash`, `merge` or `rebase`. |
| `github.commit_name`, `github.commit_email` | `Sifu`, `sifu@users.noreply.github.com` | Identity on Sifu's commits. |
| `workspace.product_repo_path` | required | Local clone of the product repository. |
| `workspace.worktrees_dir` | required | Where per-ticket git worktrees are made. |
| `workspace.artifacts_dir` | `data/artifacts` | Where `spec.md`, `plan.md` and results files go. |
| `database.path` | `data/sifu.db` | The SQLite database. |

## Design stage

`sifu.graph.run_ticket` runs one ticket: load, then in parallel the deterministic checks (glossary,
out-of-scope words, Given / When / Then format, prompt-injection phrases) and three reviewers
(ambiguity, scope, policy), then the synthesizer. Code decides the outcome: any `major` or `blocker`
finding, or a reviewer that gave no valid answer, sends the ticket to `needs_human` with questions;
otherwise the model drafts `spec.md`. Artifacts go to `workspace.artifacts_dir/<ticket id>/`
(`intent.md`, `spec.md` or `questions.md`). Every agent output and status change is an `events` row.

`intent.md` and `spec.md` are rendered from `docs/templates/intent.md` and `docs/templates/spec.md`:
the template is the structure (front matter, headings, table headers, fixed sentences), so editing
it changes the generated files with no code change. `{{name}}` is replaced by a value the code
supplies and `{# ... #}` is a comment for editors (each template lists its placeholders). A
placeholder the code does not supply is an error. Do not name a protected path in the fixed text of
`spec.md`: the router reads that file, and a mention forces a human decision on every ticket (a test
guards this). Adding a new field still needs a code change to supply its value.

Each reviewer lives in `src/sifu/prompts/<name>/` as `persona.md`, `rubric.md` and `fewshot.json`.
Model output is validated with Pydantic, retried with the error, then replaced by a fallback that
blocks the spec instead of passing it.

```bash
uv sync --extra agents
uv run python -m sifu.demo --ticket clean      # needs Ollama and the models in config.yaml
```

The same call is in `notebooks/sifu_demo.ipynb`. Seed tickets are in `seed/tickets.yaml`.

## Router, pause and resume

After the Design stage drafts `spec.md`, two assessor models (`models.risk_assessors`) rate it and
`router.decide` applies the floor rules: a protected path named in the ticket or spec, a conflict with a
requirement, or a failed assessor all force a human, and the models can only raise the tier. The decision goes to
the `decisions` table through the `record_decision` MCP tool. Tier HUMAN (or open questions) pauses the graph with a
LangGraph `interrupt`; the checkpoint is in SQLite, so `resume_ticket` works after a restart.
`failed -> intent` is the one way out of `failed`: `reopen_ticket` uses it to run a failed ticket again on
a fresh thread (a ticket with a pull request cannot be reopened).

```python
from sifu.demo import open_demo, run_router_demo, answer_request

deps, project_id = open_demo(db_path="data/sifu.db")
run_router_demo(deps, project_id)  # one ticket auto-approved, one paused
answer_request(deps, 1, "approve")  # or "close", or any text as feedback
```

A rejection with feedback re-runs the Design stage (at most `router.max_design_rounds` times) and appends a lesson
to `data/artifacts/conventions.proposed.md`; the approved `conventions.md` in the product repo is never edited
directly. Each ticket also stops after `router.max_tokens_per_ticket` model tokens.

## Tools and the implementer

`sifu.tools` is a FastMCP server (`get_ticket`, `list_glossary_terms`, `record_decision`). Agents reach it through
`ToolGateway`, which allow-lists tools per actor (reviewers and assessors get none) and logs every call. 
`sifu.implementer` runs `claude -p` in a per-ticket git worktree with the allow-list, turn cap and timeout from
`config.yaml`, a minimal environment (the GitHub token is never passed) and a check of the changed files against the
protected paths. Try it once with `uv run python scripts/implementer_spike.py --repo <tripplanner-ios>`.

Plan step (off by default, `planner.enabled` in `config.yaml`): after the spec is accepted, the author model reads the
repository's file list and excerpts of the spec's files (no tools) and writes `plan.md`. Code then checks it: a
protected file, a file outside the spec's `affected_paths`, or an unusable plan sends it to a human (decision type
`accept_plan`); otherwise the router accepts it and Build may start. Build never starts without an accepted plan.
The implementer receives the spec and the plan together.

Review stage (verify): after Build, Sifu runs the checks itself (`verify.steps` in `config.yaml`: `swift build`
with warnings as errors, SwiftLint, `swift test`), cheapest first, stopping at the first failing step. Two free diff
checks run before them: no added `swiftlint:disable`, and no changed file outside the spec's `affected_paths`
(plus `verify.extra_allowed_paths`). Findings go back to the implementer by resuming the same `claude -p` session,
up to `router.max_build_iterations` rounds. The loop stops early if two rounds give the same findings, and the ticket
then moves to Needs Human. Try the checks on a worktree with `scripts/verify_spike.py --worktree <path>`.

Switching the implementer: set `implementer.backend` in `config.yaml` to `cloud` (Claude Code with your
Anthropic login) or `ollama` (Claude Code pointed at the local Ollama server, using `implementer.ollama_model`).
For one run, use `SIFU_IMPLEMENTER_BACKEND=ollama` or `--backend ollama` on the spike script. `--compare` runs the
same ticket on both backends in separate worktrees (`ticket-<id>-cloud`, `ticket-<id>-ollama`) and prints a table
of turns, time, cost and changed files. Every implementer result records its backend and model.

## Pull request and merge

When the verify stage passes and a GitHub client is configured, Sifu publishes the work:

1. `open_pr` commits whatever the implementer left in the worktree (as `github.commit_name`), pushes
   `sifu/ticket-<id>` and opens a pull request against `github.base_branch`. The PR body carries the
   spec, the changed files and the checks Sifu ran. The token is read through
   `sifu.credentials.get_github_token`, passed to git as an environment config header (never on the
   command line) and scrubbed from every error message.
2. `merge_decision` waits for the required check (`github.required_check`, up to
   `ci_timeout_seconds`), then asks the router for a `merge` decision. The floor rules apply in every
   `router.merge_mode`: a red or pending check, a protected path, or the iteration cap forces a human.
   `manual` always asks a human, `risk_based` adds the two model assessors, and `auto` skips them
   (floor rules only). The tier, the rules that fired and each assessor's rationale are posted as a PR
   comment.
3. Tier `auto` goes straight to `merge` (re-checks CI, then merges with `github.merge_method`, pinned
   to the pushed sha). Any other tier pauses the ticket with the PR URL; approving resumes at `merge`,
   anything else closes the ticket and leaves the PR open.

Failures: a push or PR error marks the ticket Failed (`pr_failed`); a check that turns red between
approval and merge sends it to Needs Human (`merge_blocked`); a refused merge is Failed
(`merge_failed`). Tests use a fake GitHub client and a local bare remote, so they need no network.

When the verify loop gives up (`verify_cap` or `verify_no_progress`) and GitHub is configured, Sifu
pushes the branch and opens a **draft** pull request titled `[Draft] ...` with a comment listing the
unresolved findings, so a human can pick the work up on GitHub. The ticket stays in Needs Human. If
the push or draft PR fails, the error is logged and the ticket still waits for a human.

## Critic pass

With `critic.enabled: true`, once every deterministic check has passed, three narrow critics (edge
cases, contract against the acceptance criteria, security) read the change. They run
in parallel on `models.code_reviewer`, a different family from the author, each with its own persona,
rubric and few-shot examples (`prompts/critic_*`). A critic can add findings but never clear one:

- The critics and the checker see each change with `critic.context_lines` unchanged lines around it,
  read from the worktree and marked as context, because the behaviour a claim says is missing is often
  on the next, unchanged line.
- Every finding must cite a line the change added and give a concrete failing scenario. Code drops
  the rest (`critic_result` events list kept and dropped findings with the reason).
- Each blocker gets a second look before it counts: a Claim Checker prompt on `models.author` (a
  different family from the critics, or `critic.confirm_model`) sees the claim and the added lines
  around it and may refute it. Refuted blockers are dropped and logged; if the checker gives no valid
  answer the blocker stands. Only blockers are checked, so the cost is one call per blocker.
- Findings on the same file and line are merged into one; the highest severity gives the headline
  and every other distinct message is kept (`also`), so no claim is lost.
- Blockers go back to the implementer through the same loop as the checks, within
  `max_build_iterations`; the same blocker twice stops with `critic_no_progress`, the cap with
  `critic_cap`, and either opens a draft PR.
- Major and minor findings are posted as one PR comment and do not block the merge decision.
- A critic that gives no valid answer adds nothing and is named in that comment.

`scripts/critic_spike.py` runs the critics on a worktree with the real model and prints what was kept
and dropped.

## Evaluation

Two golden sets, one for each half of the pipeline, both compared with a baseline or twin.

**Design stage** (`golden/tickets.yaml`, 23 tickets): five clean tickets, then seeded ambiguity (4),
scope (3), contradiction with a requirement (3), prompt injection (3), a minor glossary slip (1), an
out-of-scope feature (2) and a protected path (2). Each ticket has `expect` (spec or questions),
`route` (auto or human) and `by` (who can catch it: `checks` for deterministic code alone, `models`,
or `floor` for a router rule), so the report can separate what the models add beyond the code. The
injection and ambiguity tickets include ones the regex and the Given/When/Then check cannot catch.

```
uv run python scripts/run_eval.py --check      # labels vs the deterministic code, no model needed
uv run python scripts/run_eval.py              # swarm and single-prompt baseline, one run each
uv run python scripts/run_eval.py --repeats 3  # also reports how stable the outcomes are
uv run python scripts/run_eval.py --system swarm --label alt \
    --set models.spec_reviewer=gemma4:31b-mlx --set models.author=gemma4:31b-mlx
```

The swarm runs the real graph (planner, critic and Build off) and never resumes a pause, so "human"
means the ticket paused. The baseline (`baseline.py`) is one prompt with the same glossary, PRD and
conventions, the same untrusted-ticket note and the same retry, but no personas, few-shot examples,
parallel reviewers or code rules; it also picks the route itself. Results go to
`data/artifacts/eval/design-<label>-<time>.json` and print as a markdown table: detection rate by
defect type, false positives, false-auto, over-escalation, schema-valid and first-try rates, tokens
and seconds per ticket. In a notebook: `sifu.evalrun.load_results(path)` and
`sifu.evalset.format_markdown(results["summaries"])`.

**Critics** (`golden/code`, 10 cases against tripplanner-ios at `ebe6b67`): five diffs with three
or four seeded flaws each (16 in all) and a correct twin of each. `uv run python scripts/golden_critics.py` applies every
patch to a fresh clone, runs the critic pass and scores the kept findings. The summary adds
`clean_first_pass_rate`, the share of correct changes that pass without a blocker. The patches are
checked to apply and to hold their quotes, but not compiled; run `swift build` on one if in doubt.

Four more cases (`heldout_*` and `clean_heldout_*`: rename a Trip, remove a Stop) were written after the
first critic run and are skipped by default, so no prompt is tuned on them. Tune on the main set, then run
`uv run python scripts/golden_critics.py --heldout` once and report both. Scoring counts a finding for a
flaw when the flaw's quote is on the cited line, or within `LINE_SLACK` lines of it when the finding hits
no flaw exactly. The summary gives both: `detection_rate` with the window and `detection_rate_exact`
without it; report them side by side.

## Streamlit UI

The app is the main way to run Sifu: from a draft to a merged pull request, and everything around
it.

```bash
uv run --extra agents --extra ui streamlit run src/sifu/ui.py
```

Run it from the repo root so `.streamlit/config.toml` is picked up. The server listens on
`localhost` only and has **no login**: it can start builds and merge pull requests, so do not
expose it. Run only one Streamlit server per database.

**Sidebar.** Pick a database from `data/` (set `SIFU_UI_DB` to open one first) and a project. The
build-tools line says **Build and merge: ready** when the setup checks for Build all pass;
otherwise it says **Design only** and lists what is missing, and the app stops at the Design stage.
Looking at a tab only reads the database; sending a chat message, starting a ticket, answering a
request and changing a setting write to it. Create, every answer and "Continue to Build" are queued
and run one at a time in the background, so the page stays usable; it refreshes itself when a job
starts or ends.

- **Board:** tickets by status, each with its pull request link. "New ticket" swaps the board for
  a draft: the chat is on the left, where you talk the change through with the model
  (`models.author`) before a ticket exists, and the preview is on the right, with the title and
  description as they would be filed. The model rewrites the preview as you talk; edit it
  freely, and the next reply builds on your edits. Captions under the preview list what the
  intent template still lacks (a Given / When / Then line, a REQ id, `OPEN:` lines). "Cancel"
  goes back to the board. "Create" files the draft as a ticket and queues its run: `intent.md`
  is rendered from the draft, then the reviewers write a spec or questions. A low-risk spec is
  accepted by the router; anything else stops for you. With the build tools present a low-risk
  ticket then runs on to the merge decision under `router.merge_mode` and `planner.enabled`
  (this database's run options, else `config.yaml`); in design mode an accepted spec waits before
  Build. A card shows "Running…" while the ticket has a job.
- **Ticket:** one ticket's timeline. Router decisions show the floor and final tier, the rules
  that fired and each assessor's rationale. "Key events only" hides tool calls and status changes.
  Above it: the status and running job (with Cancel while it is queued), why the last run failed,
  and **Continue to Build**, **Reopen** or **Resume** where they apply; token use against
  `router.max_tokens_per_ticket`; a **Files** view of `intent.md`, `spec.md`, `plan.md` and the
  chat logs, with download; and a **Diff** of the ticket's worktree. A ticket stopped at a spec or
  plan decision (or at questions) also shows the document beside a second chat and a reply box the
  chat drafts for you: `approve`, `close` or send the reply, which is added to the ticket and the
  stage runs again (up to `router.max_design_rounds`).
- **Inbox:** open human requests, answered there (`approve`, `close`, `retry`, or a typed reply
  except at a merge or retry request). The ticket resumes from its checkpoint in the background. A
  plan or merge decision needs the build tools; without them the app shows what is missing and the
  `scripts/live_run.py --answer` command instead.
- **Settings:** *Setup checks* (Ollama, the models, the product repo, Claude Code, the GitHub
  token, a clean clone, the verify tools and the worktrees folder; the app shows whether a token
  exists, never its value, and has no field to enter one). *Run options* for this database only:
  `router.merge_mode`, `planner.enabled`, `critic.enabled` and `implementer.backend`; models, caps
  and protected paths stay in `config.yaml` and are shown read-only, and risk floor rules apply in
  every mode. *Lessons* waiting for `conventions.md`: edit them, preview the change and the router's
  decision, then open the pull request as a job (Sifu never merges it). *Cleanup* of the worktrees
  and branches of done or failed tickets. *New database*: a name of lowercase letters, digits and
  hyphens, created in `data/` and selected.

`scripts/live_run.py` and `scripts/lessons_pr.py` remain as command-line equivalents of the app's
run and lessons features. The evaluation and probe scripts are terminal-only.

Chats are kept. Every message is stored in the `chat_messages` table as it is sent, with the
draft as it stood after each reply. When a ticket starts, its intent chat is linked to it and
written to `data/artifacts/<database>/<ticket>/chat-intent.md` beside `intent.md`; a spec chat is
written to `chat-spec-<request>.md` when you answer. Both appear on the ticket's timeline
(`chat_saved`) and under "Chat log" on the Ticket tab, with a download button. An intent chat's id
is kept in the page URL, so reloading the page reopens it with its draft.

The chat model has no tools: it cannot file, approve or change anything, and what it drafts is
ticket text like any other, so it goes through the same checks, reviewers, assessors and risk
floor rules.

## Recorded runs in the notebook

`sifu.evidence.build_test_evidence(path)` renders one recorded database (`data/live.db`,
`data/probe-once.db`, `data/probe-always.db`) as Markdown: verify rounds, router decisions and
pull request events. It opens the file read-only and calls neither a model nor GitHub. The
databases are git-ignored, so keep a copy: they are the only local record of those live runs.

`data/live.db` (PR #2) was lost that way. `build_test_evidence(path, snapshot)` therefore takes
an optional saved GitHub record, used only when the database is missing:
`docs/recorded/pr-2.json` holds the pull request, its required check, the merge and the decision
comment Sifu posted, read from GitHub once with `gh pr view`. It cannot show that run's verify
rounds or decision rows.
