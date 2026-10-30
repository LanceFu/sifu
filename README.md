# Sifu

Sifu moves software tickets through **Plan, Design, Build, Test** using a LangGraph agent pipeline
on local Ollama models. A separate risk router decides which decisions are automatic and which need a human.
Course project for Agentic AI (University of Waterloo). The target product is the fictional
[`tripplanner-ios`](https://github.com/LanceFu/tripplanner-ios) repo, so no real data is involved.

## Status

| Piece | State |
| --- | --- |
| Config, models, SQLite schema, state machine, risk router floor rules | Done |
| LangGraph ticket graph and Design stage (deterministic checks, three parallel reviewers, synthesizer) | Done; tested with a fake model, real Ollama run pending |
| Risk router wiring (two assessors), pause and resume, lessons, FastMCP tools, caps, headless implementer | Done; tested with a fake model, real runs pending |
| FastMCP server, GitHub PR flow, Claude Code implementer node | Later |
| Evaluation harness (golden set vs single-prompt baseline), Streamlit UI | Later |

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
config.yaml          model names, implementer backend, router and GitHub settings
src/sifu/models.py   enums and Pydantic models for findings, assessments and decisions
src/sifu/state.py    ticket state machine and stage gates
src/sifu/router.py   floor rules and the final tier
src/sifu/db.py       SQLite tables: projects, tickets, events, decisions, human_requests, runs
tests/               pytest suite for the deterministic parts
notebooks/           demo notebook for the course submission
```

## Design stage

`sifu.graph.run_ticket` runs one ticket: load, then in parallel the deterministic checks (glossary,
out-of-scope words, Given / When / Then format, prompt-injection phrases) and three reviewers
(ambiguity, scope, policy), then the synthesizer. Code decides the outcome: any `major` or `blocker`
finding, or a reviewer that gave no valid answer, sends the ticket to `needs_human` with questions;
otherwise the model drafts `spec.md`. Artifacts go to `workspace.artifacts_dir/<ticket id>/`
(`intent.md`, `spec.md` or `questions.md`). Every agent output and status change is an `events` row.

Each reviewer lives in `src/sifu/prompts/<name>/` as `persona.md`, `rubric.md` and `fewshot.json`.
Model output is validated with Pydantic, retried with the error, then replaced by a fallback that
blocks the spec instead of passing it.

```bash
uv sync --extra agents
uv run python -m sifu.demo --ticket clean      # needs Ollama and the models in config.yaml
```

The same call is in `notebooks/design_stage_demo.ipynb`. Seed tickets are in `seed/tickets.yaml`.

## Router, pause and resume

After the Design stage drafts `spec.md`, two assessor models (`models.risk_assessors`) rate it and
`router.decide` applies the floor rules: a protected path named in the ticket or spec, a conflict with a
requirement, or a failed assessor all force a human, and the models can only raise the tier. The decision goes to
the `decisions` table through the `record_decision` MCP tool. Tier HUMAN (or open questions) pauses the graph with a
LangGraph `interrupt`; the checkpoint is in SQLite, so `resume_ticket` works after a restart.

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
