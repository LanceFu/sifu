"""The LangGraph ticket graph.

Design stage: load the ticket, then the glossary checker and three reviewers run in parallel, and a
synthesizer joins them. Code (``synthesizer.decide``) chooses between spec.md and questions, and
``db.set_status`` checks every transition against the state machine. Build, Test and Done are
stubs until the Build stage exists; they only run when the gate facts in the state allow it.
"""

from __future__ import annotations

import operator
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from sifu import checks, db, glossary
from sifu.config import Config
from sifu.context import ProductContext
from sifu.llm import LLM, StructuredOutcome
from sifu.models import Finding, ReviewerReport, TicketStatus
from sifu.reviewers import PROMPTS_DIR, REVIEWER_NAMES, run_reviewer
from sifu.synthesizer import (
    Outcome,
    build_questions,
    decide,
    draft_spec,
    render_questions_md,
    render_spec_md,
    write_artifact,
)

REVIEWER_MODELS = {
    "ambiguity": "spec_reviewer",
    "scope": "spec_reviewer",
    "policy": "spec_reviewer",
}


class TicketState(TypedDict, total=False):
    ticket_id: int
    title: str
    body: str
    facts: dict[str, bool]
    reports: Annotated[list[dict[str, Any]], operator.add]
    glossary_findings: list[dict[str, Any]]
    outcome: str
    spec_path: str
    questions_path: str
    request_id: int
    status: str


@dataclass
class Deps:
    """Everything the nodes need. The lock keeps one SQLite connection safe across threads."""

    conn: sqlite3.Connection
    llm: LLM
    cfg: Config
    ctx: ProductContext
    artifacts_dir: Path
    lock: threading.RLock = field(default_factory=threading.RLock)

    def log(self, ticket_id: int, kind: str, actor: str, payload: dict[str, Any]) -> None:
        with self.lock:
            db.append_event(self.conn, ticket_id, kind, actor, payload)

    def log_run(
        self, ticket_id: int, agent: str, model: str, outcome: StructuredOutcome[Any]
    ) -> None:
        with self.lock:
            db.record_run(
                self.conn,
                agent,
                model,
                ticket_id,
                outcome.prompt_tokens or None,
                outcome.completion_tokens or None,
                outcome.latency_ms,
                outcome.ok,
            )

    def move(
        self, ticket_id: int, status: TicketStatus, facts: dict[str, bool] | None = None
    ) -> None:
        with self.lock:
            db.set_status(self.conn, ticket_id, status, facts)


def build_graph(deps: Deps, checkpointer: Any = None, prompts_dir: Path = PROMPTS_DIR):
    """Compile the ticket graph for the given dependencies."""

    def load(state: TicketState) -> TicketState:
        with deps.lock:
            ticket = db.get_ticket(deps.conn, state["ticket_id"])
        title, body = ticket["title"], ticket["body"]
        deps.log(state["ticket_id"], "design_started", "supervisor", {"title": title})
        write_artifact(
            deps.artifacts_dir, state["ticket_id"], "intent.md", f"# {title}\n\n{body}\n"
        )
        return {"title": title, "body": body}

    def glossary_node(state: TicketState) -> TicketState:
        text = f"{state['title']}\n{state['body']}"
        found = glossary.check_text(deps.ctx.glossary, text) + checks.run_checks(text)
        deps.log(
            state["ticket_id"],
            "agent_output",
            "deterministic_checks",
            {"findings": [f.model_dump() for f in found], "deterministic": True},
        )
        return {"glossary_findings": [f.model_dump() for f in found]}

    def reviewer_node(name: str):
        def node(state: TicketState) -> TicketState:
            model = getattr(deps.cfg.models, REVIEWER_MODELS[name])
            report, outcome = run_reviewer(
                name,
                title=state["title"],
                body=state["body"],
                ctx=deps.ctx,
                llm=deps.llm,
                model=model,
                prompts_dir=prompts_dir,
            )
            deps.log_run(state["ticket_id"], f"reviewer:{name}", model, outcome)
            deps.log(
                state["ticket_id"],
                "agent_output",
                f"reviewer:{name}",
                {
                    "report": report.model_dump(),
                    "model": model,
                    "attempts": outcome.attempts,
                    "errors": outcome.errors,
                },
            )
            return {"reports": [report.model_dump()]}

        return node

    def synthesize(state: TicketState) -> TicketState:
        tid = state["ticket_id"]
        reports = [ReviewerReport.model_validate(r) for r in state.get("reports", [])]
        gloss = [Finding.model_validate(f) for f in state.get("glossary_findings", [])]
        outcome = decide(reports, gloss)
        if outcome.kind == "spec":
            return _write_spec(state, outcome)
        return _raise_questions(tid, reports, outcome)

    def _write_spec(state: TicketState, outcome: Outcome) -> TicketState:
        tid = state["ticket_id"]
        model = deps.cfg.models.author
        persona = (prompts_dir / "synthesizer" / "persona.md").read_text(encoding="utf-8")
        result = draft_spec(
            deps.llm,
            model=model,
            title=state["title"],
            body=state["body"],
            notes=outcome.notes,
            ctx=deps.ctx,
            persona=persona,
        )
        deps.log_run(tid, "synthesizer", model, result)
        spec = result.value
        if not spec.acceptance_criteria:
            blocker = Finding(
                reviewer="synthesizer",
                category="missing_acceptance_criteria",
                severity="major",
                message="No Given / When / Then acceptance criteria could be written.",
            )
            outcome = Outcome("questions", [blocker], outcome.notes)
            return _raise_questions(tid, [], outcome)
        text = render_spec_md(tid, spec, outcome.notes)
        path = write_artifact(deps.artifacts_dir, tid, "spec.md", text)
        deps.log(tid, "agent_output", "synthesizer", {"spec": spec.model_dump(), "path": str(path)})
        deps.move(tid, TicketStatus.SPEC)
        return {"outcome": "spec", "spec_path": str(path), "status": TicketStatus.SPEC.value}

    def _raise_questions(tid: int, reports: list[ReviewerReport], outcome: Outcome) -> TicketState:
        questions = build_questions(reports, outcome)
        path = write_artifact(
            deps.artifacts_dir, tid, "questions.md", render_questions_md(tid, questions)
        )
        deps.log(
            tid, "agent_output", "synthesizer", {"questions": [q.model_dump() for q in questions]}
        )
        deps.move(tid, TicketStatus.NEEDS_HUMAN)
        with deps.lock:
            request_id = db.open_human_request(
                deps.conn,
                tid,
                "The Design stage needs answers before it can write a spec:\n"
                + "\n".join(f"- {q.question}" for q in questions),
                ["answer and resume", "close ticket"],
            )
        return {
            "outcome": "questions",
            "questions_path": str(path),
            "request_id": request_id,
            "status": TicketStatus.NEEDS_HUMAN.value,
        }

    def after_synthesis(state: TicketState) -> str:
        return "plan_gate" if state.get("outcome") == "spec" else END

    def plan_gate(state: TicketState) -> TicketState:
        facts = state.get("facts", {})
        ready = facts.get("spec_accepted", False) and facts.get("plan_accepted", False)
        if not ready:
            deps.log(state["ticket_id"], "awaiting_acceptance", "supervisor", {"facts": facts})
        return {}

    def after_gate(state: TicketState) -> str:
        facts = state.get("facts", {})
        return "build" if facts.get("spec_accepted") and facts.get("plan_accepted") else END

    def build(state: TicketState) -> TicketState:  # stub until the Build stage exists
        deps.move(state["ticket_id"], TicketStatus.BUILD, state.get("facts"))
        deps.log(state["ticket_id"], "stub_ran", "build", {})
        return {"facts": {**state.get("facts", {}), "has_diff": True}, "status": "build"}

    def test(state: TicketState) -> TicketState:  # stub
        deps.move(state["ticket_id"], TicketStatus.TEST, state.get("facts"))
        deps.log(state["ticket_id"], "stub_ran", "test", {})
        facts = {**state.get("facts", {}), "ci_green": True, "review_passed": True}
        return {"facts": facts, "status": "test"}

    def done(state: TicketState) -> TicketState:  # stub
        deps.move(state["ticket_id"], TicketStatus.DONE, state.get("facts"))
        return {"status": "done"}

    graph = StateGraph(TicketState)
    graph.add_node("load", load)
    graph.add_node("glossary", glossary_node)
    for name in REVIEWER_NAMES:
        graph.add_node(f"review_{name}", reviewer_node(name))
    graph.add_node("synthesize", synthesize)
    graph.add_node("plan_gate", plan_gate)
    graph.add_node("build", build)
    graph.add_node("test", test)
    graph.add_node("done", done)

    graph.add_edge(START, "load")
    parallel = ["glossary", *[f"review_{n}" for n in REVIEWER_NAMES]]
    for node in parallel:
        graph.add_edge("load", node)
    graph.add_edge(parallel, "synthesize")
    graph.add_conditional_edges("synthesize", after_synthesis, ["plan_gate", END])
    graph.add_conditional_edges("plan_gate", after_gate, ["build", END])
    graph.add_edge("build", "test")
    graph.add_edge("test", "done")
    graph.add_edge("done", END)
    return graph.compile(checkpointer=checkpointer)


def run_ticket(
    deps: Deps,
    ticket_id: int,
    facts: dict[str, bool] | None = None,
    checkpointer: Any = None,
    prompts_dir: Path = PROMPTS_DIR,
) -> dict[str, Any]:
    """Run a ticket through the graph. An unexpected error marks the ticket Failed and re-raises."""
    app = build_graph(deps, checkpointer, prompts_dir)
    config = {"configurable": {"thread_id": f"ticket-{ticket_id}"}}
    try:
        return app.invoke({"ticket_id": ticket_id, "facts": facts or {}}, config)
    except Exception as exc:
        deps.log(
            ticket_id, "error", "supervisor", {"type": type(exc).__name__, "message": str(exc)}
        )
        current = TicketStatus(db.get_ticket(deps.conn, ticket_id)["status"])
        if current not in (TicketStatus.DONE, TicketStatus.FAILED):
            deps.move(ticket_id, TicketStatus.FAILED)
        raise
