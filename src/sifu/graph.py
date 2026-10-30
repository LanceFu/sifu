"""The LangGraph ticket graph.

Design stage: load the ticket, then the deterministic checks and three reviewers run in parallel and
a synthesizer joins them. Code (``synthesizer.decide``) chooses between spec.md and questions.

Router: a drafted spec goes to the risk router. Two assessor models rate it, ``router.decide``
applies the floor rules (the models can only raise the tier), and the decision is logged. Tier
AUTO or AUTO_WITH_VETO moves on; tier HUMAN, or any open questions, pauses the graph with a
LangGraph ``interrupt`` until ``resume_ticket`` supplies the human's answer. ``db.set_status``
checks every transition against the state machine. Build, Test and Done are stubs (Build can call
a real implementer) and only run when the gate facts allow it.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from sifu import checks, db, router
from sifu.assessors import run_assessor
from sifu.config import Config
from sifu.context import ProductContext
from sifu.glossary import Glossary, check_text
from sifu.lessons import propose_lesson
from sifu.llm import LLM, StructuredOutcome
from sifu.models import (
    DecisionContext,
    DecisionType,
    Finding,
    ReviewerReport,
    RiskTier,
    Spec,
    TicketStatus,
)
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
from sifu.tools import ToolGateway, build_server

CONFLICT_CATEGORIES = {"requirement_conflict", "constraint_violation", "contradiction"}


class BudgetExceeded(RuntimeError):
    """Raised when a ticket has used more model tokens than ``router.max_tokens_per_ticket``."""


def merge_reports(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reducer for parallel reviewer reports. A ``{"reset": True}`` item starts a new round."""
    merged = list(old or [])
    for item in new or []:
        if item.get("reset"):
            merged = []
        else:
            merged.append(item)
    return merged


class TicketState(TypedDict, total=False):
    ticket_id: int
    title: str
    body: str
    facts: dict[str, bool]
    round: int
    reports: Annotated[list[dict[str, Any]], merge_reports]
    glossary_findings: list[dict[str, Any]]
    outcome: str
    spec: dict[str, Any]
    spec_path: str
    questions_path: str
    tier: int
    decision_id: int
    wait_kind: str
    request_id: int
    answer: str
    status: str


@dataclass
class Deps:
    """Everything the nodes need. The lock keeps one SQLite connection safe across threads."""

    conn: sqlite3.Connection
    llm: LLM
    cfg: Config
    ctx: ProductContext
    artifacts_dir: Path
    checkpointer: Any = None
    implementer: Callable[[int, str, str], Any] | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)
    gateway: ToolGateway | None = None

    def __post_init__(self) -> None:
        self.artifacts_dir = Path(self.artifacts_dir)
        if self.checkpointer is None:
            self.checkpointer = InMemorySaver()
        if self.gateway is None:
            self.gateway = ToolGateway(build_server(self.conn, self.ctx, self.lock), self._tool_log)

    @property
    def proposed_conventions(self) -> Path:
        return self.artifacts_dir / "conventions.proposed.md"

    @property
    def approved_conventions(self) -> Path:
        return Path(self.cfg.workspace.product_repo_path).expanduser() / "conventions.md"

    def _tool_log(self, kind: str, payload: dict[str, Any]) -> None:
        ticket_id = payload.get("ticket_id")
        if ticket_id is not None:
            self.log(ticket_id, kind, payload["actor"], payload)

    def tool(self, actor: str, name: str, *, log_for: int | None = None, **arguments: Any) -> Any:
        assert self.gateway is not None
        return self.gateway.call(actor, name, for_ticket=log_for, **arguments)

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

    def check_budget(self, ticket_id: int) -> None:
        """Stop a ticket that has spent its token budget. Called before every model call."""
        with self.lock:
            used = self.conn.execute(
                "SELECT COALESCE(SUM(COALESCE(prompt_tokens, 0)"
                " + COALESCE(completion_tokens, 0)), 0) FROM runs WHERE ticket_id = ?",
                (ticket_id,),
            ).fetchone()[0]
        limit = self.cfg.router.max_tokens_per_ticket
        if used >= limit:
            self.log(ticket_id, "budget_exceeded", "supervisor", {"used": used, "limit": limit})
            raise BudgetExceeded(f"ticket {ticket_id} used {used} tokens (limit {limit})")

    def move(
        self, ticket_id: int, status: TicketStatus, facts: dict[str, bool] | None = None
    ) -> None:
        with self.lock:
            db.set_status(self.conn, ticket_id, status, facts)


def sqlite_checkpointer(path: str) -> SqliteSaver:
    """A LangGraph checkpointer in SQLite. Use the same file as the app database."""
    return SqliteSaver(sqlite3.connect(path, check_same_thread=False))


def build_graph(deps: Deps, prompts_dir: Path = PROMPTS_DIR):  # noqa: C901 - one flat node list
    """Compile the ticket graph for the given dependencies."""
    cfg = deps.cfg

    def load(state: TicketState) -> TicketState:
        tid = state["ticket_id"]
        ticket = deps.tool("supervisor", "get_ticket", log_for=tid, ticket_id=tid)
        deps.log(tid, "design_started", "supervisor", {"title": ticket["title"]})
        write_artifact(
            deps.artifacts_dir, tid, "intent.md", f"# {ticket['title']}\n\n{ticket['body']}\n"
        )
        return {
            "title": ticket["title"],
            "body": ticket["body"],
            "reports": [{"reset": True}],
            "round": state.get("round", 0),
        }

    def checks_node(state: TicketState) -> TicketState:
        tid = state["ticket_id"]
        glossary = Glossary.model_validate(
            deps.tool("supervisor", "list_glossary_terms", log_for=tid)
        )
        text = f"{state['title']}\n{state['body']}"
        found = check_text(glossary, text) + checks.run_checks(text)
        deps.log(
            tid,
            "agent_output",
            "deterministic_checks",
            {"findings": [f.model_dump() for f in found], "deterministic": True},
        )
        return {"glossary_findings": [f.model_dump() for f in found]}

    def reviewer_node(name: str):
        def node(state: TicketState) -> TicketState:
            tid = state["ticket_id"]
            deps.check_budget(tid)
            model = cfg.models.spec_reviewer
            report, outcome = run_reviewer(
                name,
                title=state["title"],
                body=state["body"],
                ctx=deps.ctx,
                llm=deps.llm,
                model=model,
                prompts_dir=prompts_dir,
            )
            deps.log_run(tid, f"reviewer:{name}", model, outcome)
            deps.log(
                tid,
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
            return _write_spec(state, outcome, reports, gloss)
        return _raise_questions(tid, reports, outcome)

    def _write_spec(
        state: TicketState, outcome: Outcome, reports: list[ReviewerReport], gloss: list[Finding]
    ) -> TicketState:
        tid = state["ticket_id"]
        deps.check_budget(tid)
        model = cfg.models.author
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
            return _raise_questions(tid, [], Outcome("questions", [blocker], outcome.notes))
        path = write_artifact(
            deps.artifacts_dir, tid, "spec.md", render_spec_md(tid, spec, outcome.notes)
        )
        deps.log(tid, "agent_output", "synthesizer", {"spec": spec.model_dump(), "path": str(path)})
        deps.move(tid, TicketStatus.SPEC)
        conflict = any(
            f.category in CONFLICT_CATEGORIES and f.severity != "info"
            for f in [*(f for r in reports for f in r.findings), *gloss]
        )
        facts = {**state.get("facts", {}), "conflicts_with_requirements": conflict}
        return {
            "outcome": "spec",
            "spec": spec.model_dump(),
            "spec_path": str(path),
            "facts": facts,
            "status": TicketStatus.SPEC.value,
        }

    def _raise_questions(tid: int, reports: list[ReviewerReport], outcome: Outcome) -> TicketState:
        questions = build_questions(reports, outcome)
        path = write_artifact(
            deps.artifacts_dir, tid, "questions.md", render_questions_md(tid, questions)
        )
        deps.log(
            tid, "agent_output", "synthesizer", {"questions": [q.model_dump() for q in questions]}
        )
        return {"outcome": "questions", "questions_path": str(path), "status": "questions"}

    def after_synthesis(state: TicketState) -> str:
        return "route" if state.get("outcome") == "spec" else "escalate_questions"

    # ---- risk router -------------------------------------------------------------------------
    def route(state: TicketState) -> TicketState:
        tid = state["ticket_id"]
        spec = Spec.model_validate(state["spec"])
        spec_text = Path(state["spec_path"]).read_text(encoding="utf-8")
        text = f"{state['title']}\n{state['body']}\n{spec_text}"
        touched = [
            *spec.affected_paths,
            *checks.protected_mentions(text, cfg.router.protected_paths),
        ]
        context = DecisionContext(
            decision_type=DecisionType.ACCEPT_SPEC,
            touched_paths=touched,
            iteration=0,
            conflicts_with_requirements=state.get("facts", {}).get(
                "conflicts_with_requirements", False
            ),
        )

        def assess(model: str):
            deps.check_budget(tid)
            assessment, outcome = run_assessor(
                model,
                decision_type=DecisionType.ACCEPT_SPEC,
                title=state["title"],
                spec_text=spec_text,
                ctx=deps.ctx,
                llm=deps.llm,
                prompts_dir=prompts_dir,
            )
            deps.log_run(tid, f"assessor:{model}", model, outcome)
            return assessment

        with ThreadPoolExecutor(max_workers=len(cfg.models.risk_assessors)) as pool:
            assessments = list(pool.map(assess, cfg.models.risk_assessors))
        result = router.decide(context, assessments, cfg.router)
        decision_id = deps.tool(
            "supervisor",
            "record_decision",
            log_for=tid,
            ticket_id=tid,
            decision_type=DecisionType.ACCEPT_SPEC.value,
            final_tier=int(result.final_tier),
            floor_tier=int(result.floor_tier),
            rules_fired=result.rules_fired,
            assessments=[a.model_dump(mode="json") for a in assessments],
        )
        deps.log(
            tid,
            "router_decision",
            "router",
            {
                "decision_id": decision_id,
                "tier": int(result.final_tier),
                "floor": int(result.floor_tier),
                "rules": result.rules_fired,
                "touched_paths": touched,
                "assessor_tiers": [int(t) for t in result.assessor_tiers],
            },
        )
        return {"tier": int(result.final_tier), "decision_id": decision_id}

    def after_route(state: TicketState) -> str:
        return "escalate_decision" if state["tier"] >= RiskTier.HUMAN else "accept"

    def accept(state: TicketState) -> TicketState:
        tid = state["ticket_id"]
        auto = RiskTier(state["tier"]).name.lower()
        deps.log(tid, "spec_accepted", "router", {"by": auto, "decision_id": state["decision_id"]})
        return {"facts": {**state.get("facts", {}), "spec_accepted": True}}

    # ---- human in the loop -------------------------------------------------------------------
    def escalate_questions(state: TicketState) -> TicketState:
        tid = state["ticket_id"]
        text = Path(state["questions_path"]).read_text(encoding="utf-8")
        deps.move(tid, TicketStatus.NEEDS_HUMAN)
        with deps.lock:
            request_id = db.open_human_request(
                deps.conn,
                tid,
                f"The Design stage needs answers before it can write a spec:\n{text}",
                ["answer the questions", "close"],
            )
        return {"wait_kind": "questions", "request_id": request_id, "status": "needs_human"}

    def escalate_decision(state: TicketState) -> TicketState:
        tid = state["ticket_id"]
        deps.move(tid, TicketStatus.NEEDS_HUMAN)
        with deps.lock:
            row = deps.conn.execute(
                "SELECT rules_fired FROM decisions WHERE id = ?", (state["decision_id"],)
            ).fetchone()
            request_id = db.open_human_request(
                deps.conn,
                tid,
                "The router needs a person to approve this spec "
                f"(rules fired: {row['rules_fired']}). Read {state['spec_path']}.",
                ["approve", "close"],
                state["decision_id"],
            )
        return {"wait_kind": "decision", "request_id": request_id, "status": "needs_human"}

    def await_human(state: TicketState) -> TicketState:
        with deps.lock:
            request = deps.conn.execute(
                "SELECT question, options FROM human_requests WHERE id = ?", (state["request_id"],)
            ).fetchone()
        answer = interrupt(
            {
                "ticket_id": state["ticket_id"],
                "request_id": state["request_id"],
                "kind": state["wait_kind"],
                "question": request["question"],
                "options": request["options"],
            }
        )
        with deps.lock:
            db.answer_human_request(deps.conn, state["request_id"], str(answer))
        return {"answer": str(answer)}

    def after_answer(state: TicketState) -> str:
        answer = state["answer"].strip().lower()
        if answer.startswith("close"):
            return "close_ticket"
        if state["wait_kind"] == "decision" and answer.startswith("approve"):
            return "approve_decision"
        return "revise"

    def approve_decision(state: TicketState) -> TicketState:
        tid = state["ticket_id"]
        with deps.lock:
            db.resolve_decision(deps.conn, state["decision_id"], "human_approved", "human")
        deps.move(tid, TicketStatus.SPEC)
        deps.log(tid, "spec_accepted", "human", {"decision_id": state["decision_id"]})
        return {"facts": {**state.get("facts", {}), "spec_accepted": True}, "status": "spec"}

    def revise(state: TicketState) -> TicketState:
        tid = state["ticket_id"]
        feedback = state["answer"].strip()
        next_round = state.get("round", 0) + 1
        if state["wait_kind"] == "decision":
            with deps.lock:
                db.resolve_decision(deps.conn, state["decision_id"], "human_rejected", "human")
            lesson = f"A human rejected the spec for '{state['title']}': {feedback}"
            if propose_lesson(deps.approved_conventions, deps.proposed_conventions, lesson, tid):
                deps.log(tid, "lesson_proposed", "human", {"lesson": lesson})
        with deps.lock:
            db.append_to_ticket_body(
                deps.conn, tid, f"Clarification (round {next_round}): {feedback}"
            )
        if next_round > cfg.router.max_design_rounds:
            deps.log(tid, "design_round_cap", "supervisor", {"rounds": next_round})
            deps.move(tid, TicketStatus.FAILED)
            return {"status": "failed", "outcome": "failed", "round": next_round}
        deps.move(tid, TicketStatus.INTENT)
        return {"round": next_round, "status": "intent"}

    def after_revise(state: TicketState) -> str:
        return END if state.get("status") == "failed" else "load"

    def close_ticket(state: TicketState) -> TicketState:
        tid = state["ticket_id"]
        if state["wait_kind"] == "decision":
            with deps.lock:
                db.resolve_decision(deps.conn, state["decision_id"], "human_closed", "human")
        deps.move(tid, TicketStatus.FAILED)
        deps.log(tid, "ticket_closed", "human", {})
        return {"status": "failed", "outcome": "closed"}

    # ---- later stages (stubs until the Build stage exists) ------------------------------------
    def plan_gate(state: TicketState) -> TicketState:
        facts = state.get("facts", {})
        if not facts.get("plan_accepted", False):
            deps.log(state["ticket_id"], "awaiting_plan", "supervisor", {"facts": facts})
        return {}

    def after_gate(state: TicketState) -> str:
        facts = state.get("facts", {})
        return "build" if facts.get("spec_accepted") and facts.get("plan_accepted") else END

    def build(state: TicketState) -> TicketState:
        tid = state["ticket_id"]
        facts = state.get("facts", {})
        deps.move(tid, TicketStatus.BUILD, facts)
        if deps.implementer is None:
            deps.log(tid, "stub_ran", "build", {})
            return {"facts": {**facts, "has_diff": True}, "status": "build"}
        spec_text = Path(state["spec_path"]).read_text(encoding="utf-8")
        result = deps.implementer(tid, spec_text, deps.ctx.conventions_text)
        deps.log(tid, "implementer_result", "implementer", _result_payload(result))
        with deps.lock:
            db.record_run(
                deps.conn,
                "implementer",
                cfg.implementer.model or "claude-code",
                tid,
                None,
                None,
                result.duration_ms,
                result.ok,
            )
        if not result.ok:
            deps.move(tid, TicketStatus.FAILED)
            return {"status": "failed", "outcome": "implementer_failed"}
        return {"facts": {**facts, "has_diff": bool(result.changed_files)}, "status": "build"}

    def after_build(state: TicketState) -> str:
        return END if state.get("status") == "failed" else "test"

    def test(state: TicketState) -> TicketState:  # stub
        deps.move(state["ticket_id"], TicketStatus.TEST, state.get("facts"))
        deps.log(state["ticket_id"], "stub_ran", "test", {})
        facts = {**state.get("facts", {}), "ci_green": True, "review_passed": True}
        return {"facts": facts, "status": "test"}

    def done(state: TicketState) -> TicketState:  # stub
        deps.move(state["ticket_id"], TicketStatus.DONE, state.get("facts"))
        return {"status": "done"}

    graph = StateGraph(TicketState)
    for name, fn in {
        "load": load,
        "checks": checks_node,
        "synthesize": synthesize,
        "route": route,
        "accept": accept,
        "escalate_questions": escalate_questions,
        "escalate_decision": escalate_decision,
        "await_human": await_human,
        "approve_decision": approve_decision,
        "revise": revise,
        "close_ticket": close_ticket,
        "plan_gate": plan_gate,
        "build": build,
        "test": test,
        "done": done,
    }.items():
        graph.add_node(name, fn)
    for name in REVIEWER_NAMES:
        graph.add_node(f"review_{name}", reviewer_node(name))

    graph.add_edge(START, "load")
    parallel = ["checks", *[f"review_{n}" for n in REVIEWER_NAMES]]
    for node in parallel:
        graph.add_edge("load", node)
    graph.add_edge(parallel, "synthesize")
    graph.add_conditional_edges("synthesize", after_synthesis, ["route", "escalate_questions"])
    graph.add_conditional_edges("route", after_route, ["escalate_decision", "accept"])
    graph.add_edge("accept", "plan_gate")
    graph.add_edge("escalate_questions", "await_human")
    graph.add_edge("escalate_decision", "await_human")
    graph.add_conditional_edges(
        "await_human", after_answer, ["close_ticket", "approve_decision", "revise"]
    )
    graph.add_edge("approve_decision", "plan_gate")
    graph.add_conditional_edges("revise", after_revise, ["load", END])
    graph.add_edge("close_ticket", END)
    graph.add_conditional_edges("plan_gate", after_gate, ["build", END])
    graph.add_conditional_edges("build", after_build, ["test", END])
    graph.add_edge("test", "done")
    graph.add_edge("done", END)
    return graph.compile(checkpointer=deps.checkpointer)


def _result_payload(result: Any) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "error": result.error,
        "num_turns": result.num_turns,
        "cost_usd": result.cost_usd,
        "changed_files": result.changed_files,
        "protected_touched": result.protected_touched,
        "permission_denials": result.permission_denials,
        "branch": result.branch,
        "worktree": result.worktree,
        "output": result.result[:2000],
    }


def _thread(ticket_id: int) -> dict[str, Any]:
    return {"configurable": {"thread_id": f"ticket-{ticket_id}"}}


def _invoke(deps: Deps, ticket_id: int, payload: Any, prompts_dir: Path) -> dict[str, Any]:
    try:
        return build_graph(deps, prompts_dir).invoke(payload, _thread(ticket_id))
    except Exception as exc:
        deps.log(
            ticket_id, "error", "supervisor", {"type": type(exc).__name__, "message": str(exc)}
        )
        current = TicketStatus(db.get_ticket(deps.conn, ticket_id)["status"])
        if current not in (TicketStatus.DONE, TicketStatus.FAILED):
            deps.move(ticket_id, TicketStatus.FAILED)
        raise


def run_ticket(
    deps: Deps,
    ticket_id: int,
    facts: dict[str, bool] | None = None,
    prompts_dir: Path = PROMPTS_DIR,
) -> dict[str, Any]:
    """Run a ticket until it finishes or pauses for a human (``"__interrupt__"`` in the result).

    An unexpected error marks the ticket Failed and re-raises.
    """
    return _invoke(deps, ticket_id, {"ticket_id": ticket_id, "facts": facts or {}}, prompts_dir)


def resume_ticket(
    deps: Deps, ticket_id: int, answer: str, prompts_dir: Path = PROMPTS_DIR
) -> dict[str, Any]:
    """Give a paused ticket the human's answer and continue it from its checkpoint."""
    return _invoke(deps, ticket_id, Command(resume=answer), prompts_dir)


def is_paused(state: dict[str, Any]) -> bool:
    return bool(state.get("__interrupt__"))
