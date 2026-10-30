# Sifu

Sifu moves software tickets through **Plan, Design, Build, Test** using a LangGraph agent pipeline
on local Ollama models. A separate risk router decides which decisions are automatic and which need a human.
Course project for Agentic AI (University of Waterloo). The target product is the fictional
[`tripplanner-ios`](https://github.com/LanceFu/tripplanner-ios) repo, so no real data is involved.

## Status

| Piece | State |
| --- | --- |
| Config, models, SQLite schema, state machine, risk router floor rules | Done, 37 tests |
| LangGraph ticket graph, Design-stage reviewers, human interrupt and resume | Next |
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
