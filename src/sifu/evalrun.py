"""Run the golden tickets through the swarm and the single-prompt baseline and record the results.

Both runners return one record per (ticket, repeat) in the same shape, so ``evalset.summarize``
scores them identically. The swarm runner drives the real LangGraph Design stage and risk router
(planner and critic off, Build left as the stub), then reads the run log in the database for
tokens and schema validity. It never resumes a paused ticket: a pause is the "human" route.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from sifu import db
from sifu.baseline import run_baseline_ticket
from sifu.config import Config
from sifu.context import ProductContext
from sifu.evalset import GoldenTicket, format_markdown, summarize
from sifu.graph import Deps, is_paused, run_ticket
from sifu.llm import LLM

BLOCKING = {"major", "blocker"}
Progress = Callable[[str], None]


def load_config_with_overrides(path: str | Path, overrides: list[str]) -> Config:
    """Load config.yaml and apply ``dotted.key=yaml_value`` overrides, for the alternative run."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    for item in overrides:
        key, sep, value = item.partition("=")
        if not sep:
            raise ValueError(f"override must look like models.spec_reviewer=NAME, got {item!r}")
        node = raw
        *parents, leaf = key.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = yaml.safe_load(value)
    return Config.model_validate(raw)


def eval_config(cfg: Config) -> Config:
    """A copy limited to the Design stage and router: no planner, no critic, no real Build."""
    cfg = cfg.model_copy(deep=True)
    cfg.planner.enabled = False
    cfg.critic.enabled = False
    return cfg


def _swarm_record(
    deps: Deps,
    ticket: GoldenTicket,
    repeat: int,
    state: dict[str, Any] | None,
    error: str | None,
    latency_ms: int,
    ticket_id: int,
) -> dict[str, Any]:
    conn = deps.conn
    runs = conn.execute(
        "SELECT ok, prompt_tokens, completion_tokens FROM runs WHERE ticket_id = ?", (ticket_id,)
    ).fetchall()
    attempts = []
    for event in db.list_events(conn, ticket_id):
        if event["kind"] == "agent_output":
            payload = json.loads(event["payload"])
            if "attempts" in payload:
                attempts.append(payload["attempts"])
    tier_row = conn.execute(
        "SELECT MAX(final_tier) AS tier FROM decisions WHERE ticket_id = ?", (ticket_id,)
    ).fetchone()

    findings: list[dict[str, Any]] = []
    if state:
        for report in state.get("reports", []):
            findings += report.get("findings", [])
        findings += state.get("glossary_findings", [])
    blocking = sorted({f["reviewer"] for f in findings if f["severity"] in BLOCKING})

    outcome = "error" if error or not state else state.get("outcome", "error")
    return {
        "key": ticket.key,
        "system": "swarm",
        "repeat": repeat,
        "outcome": outcome,
        "route": "human" if (error or (state and is_paused(state))) else "auto",
        "tier": tier_row["tier"] if tier_row else None,
        "flags": sorted({f["category"] for f in findings}),
        "blocking_sources": blocking,
        "findings": [_finding(f["reviewer"], f) for f in findings],
        "calls": len(runs),
        "calls_ok": sum(1 for r in runs if r["ok"]),
        "first_try_calls": sum(1 for a in attempts if a == 1),
        "first_try_total": len(attempts),
        "tokens": sum((r["prompt_tokens"] or 0) + (r["completion_tokens"] or 0) for r in runs),
        "latency_ms": latency_ms,
        "error": error,
    }


def run_swarm(
    tickets: list[GoldenTicket],
    cfg: Config,
    llm: LLM,
    ctx: ProductContext,
    *,
    artifacts_dir: str | Path,
    repeats: int = 1,
    progress: Progress = print,
) -> list[dict[str, Any]]:
    """Run each ticket through the Design stage and router, ``repeats`` times."""
    cfg = eval_config(cfg)
    conn = db.connect(":memory:")
    db.init_db(conn)
    project_id = db.get_or_create_project(
        conn, "TripPlanner", cfg.workspace.product_repo_path, cfg.github.repo
    )
    deps = Deps(conn=conn, llm=llm, cfg=cfg, ctx=ctx, artifacts_dir=Path(artifacts_dir))
    records = []
    for repeat in range(1, repeats + 1):
        for ticket in tickets:
            ticket_id = db.create_ticket(conn, project_id, ticket.title, ticket.body)
            started = time.monotonic()
            state, error = None, None
            try:
                state = run_ticket(deps, ticket_id)
            except Exception as exc:  # noqa: BLE001 - one bad ticket must not stop the run
                error = f"{type(exc).__name__}: {exc}"
            latency_ms = int((time.monotonic() - started) * 1000)
            record = _swarm_record(deps, ticket, repeat, state, error, latency_ms, ticket_id)
            records.append(record)
            progress(_line(record, ticket))
    return records


def run_baseline(
    tickets: list[GoldenTicket],
    cfg: Config,
    llm: LLM,
    ctx: ProductContext,
    *,
    repeats: int = 1,
    progress: Progress = print,
) -> list[dict[str, Any]]:
    """Run each ticket through the one-prompt baseline on the author model."""
    records = []
    for repeat in range(1, repeats + 1):
        for ticket in tickets:
            result = run_baseline_ticket(
                title=ticket.title, body=ticket.body, ctx=ctx, llm=llm, model=cfg.models.author
            )
            value = result.value
            record = {
                "key": ticket.key,
                "system": "baseline",
                "repeat": repeat,
                "outcome": value.verdict,
                "route": value.route,
                "tier": None,
                "flags": sorted({i.category for i in value.issues}),
                "blocking_sources": ["baseline"]
                if any(i.severity in BLOCKING for i in value.issues)
                else [],
                "findings": [_finding("baseline", i.model_dump()) for i in value.issues],
                "calls": 1,
                "calls_ok": int(result.ok),
                "first_try_calls": int(result.ok and result.attempts == 1),
                "first_try_total": 1,
                "tokens": result.prompt_tokens + result.completion_tokens,
                "latency_ms": result.latency_ms,
                "error": None if result.ok else "; ".join(result.errors)[:300],
            }
            records.append(record)
            progress(_line(record, ticket))
    return records


def _finding(source: str, finding: dict[str, Any]) -> dict[str, str]:
    """A finding trimmed for the results file: who raised it, what kind, how bad, and why."""
    return {
        "source": source,
        "category": finding["category"],
        "severity": finding["severity"],
        "message": str(finding["message"])[:300],
    }


def _line(record: dict[str, Any], ticket: GoldenTicket) -> str:
    ok = (
        "ok  " if record["outcome"] == ticket.expect and record["route"] == ticket.route else "DIFF"
    )
    line = (
        f"{ok} {record['system']:8} #{record['repeat']} {ticket.key:26} "
        f"expect {ticket.expect}/{ticket.route:5} got {record['outcome']}/{record['route']}"
        + (f" ERROR {record['error']}" if record["error"] else "")
    )
    if ok == "DIFF":  # say why, so a wrong label is told apart from a wrong answer
        for f in record.get("findings", []):
            if f["severity"] in BLOCKING:
                line += (
                    f"\n       {f['source']} {f['severity']} {f['category']}: {f['message'][:200]}"
                )
    return line


def models_snapshot(cfg: Config) -> dict[str, Any]:
    """The configuration facts a results file needs to be reproducible."""
    return {
        "models": cfg.models.model_dump(),
        "num_ctx": cfg.ollama.num_ctx,
        "protected_paths": cfg.router.protected_paths,
    }


def write_results(
    out_dir: str | Path,
    label: str,
    tickets: list[GoldenTicket],
    per_system: dict[str, list[dict[str, Any]]],
    cfg: Config,
) -> tuple[Path, str]:
    """Summarize each system, write one JSON file and return its path and the markdown table."""
    summaries, scores = {}, {}
    for name, records in per_system.items():
        summaries[name], scores[name] = summarize(tickets, records)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"design-{label}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    payload = {
        "label": label,
        "config": models_snapshot(cfg),
        "summaries": summaries,
        "records": per_system,
        "scores": {n: [vars(s) for s in v] for n, v in scores.items()},
    }
    path.write_text(json.dumps(deepcopy(payload), indent=2), encoding="utf-8")
    return path, format_markdown(summaries)


def load_results(path: str | Path) -> dict[str, Any]:
    """Read a results file back, for the notebook table."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def check_labels(tickets: list[GoldenTicket], ctx: ProductContext, cfg: Config) -> list[str]:
    """Compare each ticket's labels with what the deterministic code does, with no model.

    Returns one line per problem, so a wrong label or a ticket that cites a missing requirement is
    fixed before an hour of model time is spent. An empty list means the labels agree with code.
    """
    import re

    from sifu.checks import protected_mentions, run_checks
    from sifu.glossary import check_text

    known = set(re.findall(r"REQ-\d+", ctx.prd_text))
    problems = []
    for t in tickets:
        found = check_text(ctx.glossary, t.text) + run_checks(t.text)
        blocks = [f for f in found if f.severity in BLOCKING]
        categories = {f.category for f in found}
        protected = protected_mentions(t.text, cfg.router.protected_paths)
        unknown = sorted(set(re.findall(r"REQ-\d+", t.text)) - known)
        if unknown:
            problems.append(f"{t.key}: cites {unknown}, which the PRD does not define")
        if t.defect == "clean" and found:
            problems.append(
                f"{t.key}: a clean ticket gets findings from code: {sorted(categories)}"
            )
        if t.expect == "questions" and t.by == "checks" and not blocks:
            problems.append(f"{t.key}: labelled 'checks' but code alone does not block it")
        if t.expect == "questions" and t.by == "models" and blocks:
            problems.append(f"{t.key}: labelled 'models' but code already blocks it: {categories}")
        if t.flag and t.flag not in categories:
            problems.append(f"{t.key}: code never raises the expected flag {t.flag!r}")
        if t.expect == "spec" and blocks:
            problems.append(f"{t.key}: expects a spec but code blocks it: {sorted(categories)}")
        if t.by == "floor" and not protected:
            problems.append(f"{t.key}: labelled 'floor' but names no protected path")
        if protected and t.route == "auto":
            problems.append(f"{t.key}: names {protected}, so the floor forces a human, not auto")
    return problems
