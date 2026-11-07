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

import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from sifu import checks, db, router
from sifu import critic as critic_pass
from sifu.assessors import run_assessor
from sifu.config import Config
from sifu.context import ProductContext
from sifu.github import GitHubError
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
from sifu.planner import draft_plan, outside_spec, render_plan_md, repo_context
from sifu.pr_flow import PublishError, render_findings, render_pr_body
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
from sifu.verify import added_lines

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
    stage: str
    plan_path: str
    branch: str
    worktree: str
    changed_files: list[str]
    pr_number: int
    pr_url: str
    head_sha: str
    findings: list[dict[str, Any]]
    critic_notes: list[dict[str, Any]]
    critic_degraded: list[str]
    fingerprint: str
    build_round: int
    session_id: str


@dataclass
class Deps:
    """Everything the nodes need. The lock keeps one SQLite connection safe across threads."""

    conn: sqlite3.Connection
    llm: LLM
    cfg: Config
    ctx: ProductContext
    artifacts_dir: Path
    checkpointer: Any = None
    implementer: Callable[..., Any] | None = None
    verifier: Callable[[int, dict[str, Any]], Any] | None = None
    github: Any = None  # a sifu.github.GitHubClient; with a publisher it turns on the PR flow
    publisher: Callable[[str, str, str], str] | None = None  # (worktree, branch, message) -> sha
    sleep: Callable[[float], None] = time.sleep
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
        return {"tier": int(result.final_tier), "decision_id": decision_id, "stage": "spec"}

    def after_route(state: TicketState) -> str:
        return "escalate_decision" if state["tier"] >= RiskTier.HUMAN else "accept"

    def needs_plan(state: TicketState) -> bool:
        facts = state.get("facts", {})
        return cfg.planner.enabled and not facts.get("plan_accepted", False)

    def after_accept(state: TicketState) -> str:
        return "plan" if needs_plan(state) else "plan_gate"

    def plan(state: TicketState) -> TicketState:
        """Write plan.md with the author model, then let the router decide who accepts it."""
        tid = state["ticket_id"]
        spec = Spec.model_validate(state["spec"])
        spec_text = Path(state["spec_path"]).read_text(encoding="utf-8")
        listing, excerpts = repo_context(
            Path(cfg.workspace.product_repo_path).expanduser(), spec.affected_paths, cfg.planner
        )
        persona = (prompts_dir / "planner" / "persona.md").read_text(encoding="utf-8")
        deps.check_budget(tid)
        model = cfg.models.author
        outcome = draft_plan(
            deps.llm,
            model=model,
            spec=spec,
            spec_text=spec_text,
            conventions=deps.ctx.conventions_text,
            listing=listing,
            excerpts=excerpts,
            persona=persona,
        )
        deps.log_run(tid, "planner", model, outcome)
        the_plan = outcome.value
        path = write_artifact(deps.artifacts_dir, tid, "plan.md", render_plan_md(tid, the_plan))
        deps.log(tid, "agent_output", "planner", {"plan": the_plan.model_dump(), "path": str(path)})
        outside = outside_spec(the_plan, spec, cfg)
        context = DecisionContext(
            decision_type=DecisionType.ACCEPT_PLAN,
            touched_paths=the_plan.files_to_change,
            outside_spec_paths=outside,
        )
        result = router.decide(context, [], cfg.router)
        rules = list(result.rules_fired)
        tier = result.final_tier
        if the_plan.degraded or not the_plan.files_to_change:  # code distrusts an empty plan
            rules.append("plan_unusable")
            tier = RiskTier.HUMAN
        decision_id = deps.tool(
            "supervisor",
            "record_decision",
            log_for=tid,
            ticket_id=tid,
            decision_type=DecisionType.ACCEPT_PLAN.value,
            final_tier=int(tier),
            floor_tier=int(result.floor_tier),
            rules_fired=rules,
            assessments=[],
        )
        deps.log(
            tid,
            "router_decision",
            "router",
            {"decision_id": decision_id, "tier": int(tier), "rules": rules, "stage": "plan"},
        )
        update: TicketState = {
            "tier": int(tier),
            "decision_id": decision_id,
            "stage": "plan",
            "plan_path": str(path),
        }
        if tier < RiskTier.HUMAN:
            deps.log(tid, "plan_accepted", "router", {"decision_id": decision_id})
            update["facts"] = {**state.get("facts", {}), "plan_accepted": True}
        return update

    def after_plan(state: TicketState) -> str:
        return "escalate_decision" if state["tier"] >= RiskTier.HUMAN else "plan_gate"

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
        read_path = {
            "plan": state.get("plan_path"),
            "merge": state.get("pr_url"),
        }.get(state.get("stage", "spec"), state.get("spec_path"))
        with deps.lock:
            row = deps.conn.execute(
                "SELECT rules_fired FROM decisions WHERE id = ?", (state["decision_id"],)
            ).fetchone()
            request_id = db.open_human_request(
                deps.conn,
                tid,
                f"The router needs a person to approve this {state.get('stage', 'spec')} "
                f"(rules fired: {row['rules_fired']}). "
                f"Read {read_path}.",
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
        if (
            state.get("stage") == "merge"
        ):  # feedback on a pull request is not a design clarification
            return "close_ticket"
        return "revise"

    def approve_decision(state: TicketState) -> TicketState:
        tid = state["ticket_id"]
        with deps.lock:
            db.resolve_decision(deps.conn, state["decision_id"], "human_approved", "human")
        stage = state.get("stage", "spec")
        if stage == "merge":
            deps.move(tid, TicketStatus.TEST, state.get("facts", {}))
        else:
            deps.move(tid, TicketStatus.SPEC)
        deps.log(tid, f"{stage}_accepted", "human", {"decision_id": state["decision_id"]})
        if stage == "merge":
            return {"status": "test"}
        fact = "plan_accepted" if stage == "plan" else "spec_accepted"
        return {"facts": {**state.get("facts", {}), fact: True}, "status": "spec"}

    def after_approve(state: TicketState) -> str:
        stage = state.get("stage", "spec")
        if stage == "merge":
            return "merge"
        return "plan" if stage == "spec" and needs_plan(state) else "plan_gate"

    def revise(state: TicketState) -> TicketState:
        tid = state["ticket_id"]
        feedback = state["answer"].strip()
        next_round = state.get("round", 0) + 1
        if state["wait_kind"] == "decision":
            with deps.lock:
                db.resolve_decision(deps.conn, state["decision_id"], "human_rejected", "human")
            stage = state.get("stage", "spec")
            lesson = f"A human rejected the {stage} for '{state['title']}': {feedback}"
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
        if state.get("plan_path"):
            spec_text += "\n\n" + Path(state["plan_path"]).read_text(encoding="utf-8")
        feedback = state.get("findings") or []
        if feedback:
            result = deps.implementer(
                tid,
                spec_text,
                deps.ctx.conventions_text,
                feedback=feedback,
                session_id=state.get("session_id"),
            )
        else:
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
        return {
            "facts": {**facts, "has_diff": bool(result.changed_files)},
            "status": "build",
            "session_id": result.session_id or state.get("session_id") or "",
            "branch": result.branch,
            "worktree": result.worktree,
            "changed_files": list(result.changed_files),
        }

    def after_build(state: TicketState) -> str:
        return END if state.get("status") == "failed" else "verify"

    def verify(state: TicketState) -> TicketState:
        """Run the deterministic checks. A failure goes back to build until the caps are hit."""
        tid = state["ticket_id"]
        deps.move(tid, TicketStatus.TEST, state.get("facts"))
        if deps.verifier is None:  # stub until a verifier is configured
            deps.log(tid, "stub_ran", "test", {})
            facts = {**state.get("facts", {}), "ci_green": True, "review_passed": True}
            return {"facts": facts, "status": "test", "findings": []}
        result = deps.verifier(tid, state.get("spec", {}))
        limit = cfg.verify.max_findings
        deps.log(tid, "verify_result", "verify", result.to_payload(limit))
        if result.ok:
            facts = {**state.get("facts", {}), "ci_green": True, "review_passed": True}
            return {"facts": facts, "status": "test", "findings": []}
        rounds = state.get("build_round", 0) + 1
        reason = ""
        if rounds >= cfg.router.max_build_iterations:
            reason = "verify_cap"
        elif result.fingerprint == state.get("fingerprint"):
            reason = "verify_no_progress"
        if reason:
            deps.log(tid, reason, "supervisor", {"rounds": rounds})
            deps.move(tid, TicketStatus.NEEDS_HUMAN)
            kept = [asdict(f) for f in result.findings[:limit]]
            return {
                "status": "needs_human",
                "outcome": reason,
                "build_round": rounds,
                "findings": kept,
            }
        findings = [asdict(f) for f in result.findings[:limit]]
        return {
            "status": "test",
            "findings": findings,
            "fingerprint": result.fingerprint,
            "build_round": rounds,
        }

    def critic_on(state: TicketState) -> bool:
        worktree = state.get("worktree")
        return bool(cfg.critic.enabled and worktree and Path(worktree).is_dir())

    def after_verify(state: TicketState) -> str:
        can_publish = deps.github is not None and deps.publisher is not None and state.get("branch")
        if state.get("status") == "needs_human":
            return "draft_pr" if can_publish else END
        if state.get("findings"):
            return "build"
        if critic_on(state):
            return "critic"
        return "open_pr" if can_publish else "done"

    def critic(state: TicketState) -> TicketState:
        """LLM critics read the diff once every deterministic check has passed."""
        tid = state["ticket_id"]
        spec_text = Path(state["spec_path"]).read_text(encoding="utf-8")
        index = critic_pass.added_index(added_lines(Path(state["worktree"])))
        steps = [e for e in db.list_events(deps.conn, tid) if e["kind"] == "verify_result"]
        names = [s["name"] for s in json.loads(steps[-1]["payload"])["steps"]] if steps else []
        model = cfg.models.code_reviewer

        def one(name: str):
            deps.check_budget(tid)
            report, outcome = critic_pass.run_critic(
                name,
                spec_text=spec_text,
                index=index,
                checks=", ".join(names) or "none",
                llm=deps.llm,
                model=model,
                max_diff_chars=cfg.critic.max_diff_chars,
                min_scenario_words=cfg.critic.min_scenario_words,
                prompts_dir=prompts_dir,
            )
            deps.log_run(tid, f"critic:{name}", model, outcome)
            return report

        with ThreadPoolExecutor(max_workers=len(cfg.critic.names)) as pool:
            reports = list(pool.map(one, cfg.critic.names))
        kept = critic_pass.merge_findings([f for r in reports for f in r.kept])
        dropped = [f for r in reports for f in r.dropped]
        degraded = [r.critic for r in reports if r.degraded]
        deps.log(
            tid,
            "critic_result",
            "critic",
            {"kept": kept, "dropped": dropped, "degraded": degraded},
        )
        blockers = critic_pass.to_implementer_findings(kept)
        notes = [f for f in kept if f["severity"] != "blocker"]
        if not blockers:
            return {"findings": [], "critic_notes": notes, "critic_degraded": degraded}
        rounds = state.get("build_round", 0) + 1
        keys = sorted((b["source"], b["file"], b["rule"]) for b in blockers)
        fingerprint = hashlib.sha256(json.dumps(keys).encode()).hexdigest()[:16]
        reason = ""
        if rounds >= cfg.router.max_build_iterations:
            reason = "critic_cap"
        elif fingerprint == state.get("fingerprint"):
            reason = "critic_no_progress"
        if reason:
            deps.log(tid, reason, "supervisor", {"rounds": rounds})
            deps.move(tid, TicketStatus.NEEDS_HUMAN)
            return {
                "status": "needs_human",
                "outcome": reason,
                "build_round": rounds,
                "findings": blockers,
                "critic_notes": notes,
                "critic_degraded": degraded,
            }
        return {
            "status": "test",
            "findings": blockers,
            "fingerprint": fingerprint,
            "build_round": rounds,
            "critic_notes": notes,
            "critic_degraded": degraded,
        }

    def after_critic(state: TicketState) -> str:
        can_publish = deps.github is not None and deps.publisher is not None and state.get("branch")
        if state.get("status") == "needs_human":
            return "draft_pr" if can_publish else END
        if state.get("findings"):
            return "build"
        return "open_pr" if can_publish else "done"

    def post_critic_notes(state: TicketState, number: int) -> None:
        notes = state.get("critic_notes", [])
        if not notes and not state.get("critic_degraded"):
            return
        comment = critic_pass.render_pr_comment(notes, state.get("critic_degraded", []))
        try:
            deps.github.comment(number, comment)
        except GitHubError as exc:
            deps.log(
                state["ticket_id"],
                "error",
                "supervisor",
                {"type": "GitHubError", "message": str(exc)},
            )

    # ---- pull request and merge --------------------------------------------------------------
    def fail(tid: int, reason: str, exc: Exception) -> TicketState:
        deps.log(tid, "error", "supervisor", {"type": type(exc).__name__, "message": str(exc)})
        deps.move(tid, TicketStatus.FAILED)
        return {"status": "failed", "outcome": reason}

    def open_pr(state: TicketState) -> TicketState:
        """Commit and push the branch, then open the pull request."""
        tid = state["ticket_id"]
        assert deps.github is not None and deps.publisher is not None
        spec_text = Path(state["spec_path"]).read_text(encoding="utf-8")
        verify_event = [e for e in db.list_events(deps.conn, tid) if e["kind"] == "verify_result"]
        verify = json.loads(verify_event[-1]["payload"]) if verify_event else None
        try:
            sha = deps.publisher(state["worktree"], state["branch"], f"{state['title']} (#{tid})")
            number, url = deps.github.create_pr(
                head=state["branch"],
                base=cfg.github.base_branch,
                title=f"{state['title']} (Sifu ticket {tid})",
                body=render_pr_body(tid, spec_text, state.get("changed_files", []), verify),
            )
        except (GitHubError, PublishError) as exc:
            return fail(tid, "pr_failed", exc)
        deps.log(tid, "pr_opened", "supervisor", {"number": number, "url": url, "sha": sha})
        post_critic_notes(state, number)
        return {"pr_number": number, "pr_url": url, "head_sha": sha}

    def draft_pr(state: TicketState) -> TicketState:
        """Verify gave up: push the branch as a draft PR so a human can see what is left."""
        tid = state["ticket_id"]
        assert deps.github is not None and deps.publisher is not None
        findings = state.get("findings", [])
        reason = state.get("outcome", "")
        try:
            sha = deps.publisher(state["worktree"], state["branch"], f"{state['title']} (#{tid})")
            number, url = deps.github.create_pr(
                head=state["branch"],
                base=cfg.github.base_branch,
                title=f"[Draft] {state['title']} (Sifu ticket {tid})",
                body=render_pr_body(
                    tid,
                    Path(state["spec_path"]).read_text(encoding="utf-8"),
                    state.get("changed_files", []),
                    None,
                ),
                draft=True,
            )
            deps.github.comment(
                number, render_findings(reason, state.get("build_round", 0), findings)
            )
        except (GitHubError, PublishError) as exc:  # the ticket is already waiting for a human
            deps.log(tid, "error", "supervisor", {"type": type(exc).__name__, "message": str(exc)})
            return {}
        deps.log(tid, "draft_pr_opened", "supervisor", {"number": number, "url": url, "sha": sha})
        post_critic_notes(state, number)
        return {"pr_number": number, "pr_url": url, "head_sha": sha}

    def after_pr(state: TicketState) -> str:
        return END if state.get("status") == "failed" else "merge_decision"

    def wait_for_ci(sha: str) -> bool | None:
        """True/False once the required check finishes; None if still pending at timeout."""
        waited = 0
        while True:
            status = deps.github.ci_status(sha)
            if status != "pending":
                return status == "success"
            if waited >= cfg.github.ci_timeout_seconds:
                return None
            deps.sleep(cfg.github.ci_poll_seconds)
            waited += cfg.github.ci_poll_seconds

    def merge_decision(state: TicketState) -> TicketState:
        """The router decides who merges: floor rules, plus assessors unless merge_mode is auto."""
        tid = state["ticket_id"]
        try:
            ci = wait_for_ci(state["head_sha"])
        except GitHubError as exc:
            return fail(tid, "ci_check_failed", exc)
        files = state.get("changed_files", [])
        context = DecisionContext(
            decision_type=DecisionType.MERGE,
            touched_paths=files,
            iteration=state.get("build_round", 0),
            ci_green=ci,
        )
        spec_text = Path(state["spec_path"]).read_text(encoding="utf-8")
        merge_text = (
            f"{spec_text}\n\nChanged files: {', '.join(files) or 'none'}\n"
            f"Required check ({cfg.github.required_check}): "
            f"{ {True: 'passed', False: 'failed', None: 'pending'}[ci] }"
        )
        assessments = []
        if cfg.router.merge_mode != "auto":

            def assess(model: str):
                deps.check_budget(tid)
                assessment, outcome = run_assessor(
                    model,
                    decision_type=DecisionType.MERGE,
                    title=state["title"],
                    spec_text=merge_text,
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
            decision_type=DecisionType.MERGE.value,
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
                "rules": result.rules_fired,
                "stage": "merge",
                "ci_green": ci,
            },
        )
        comment = (
            f"Sifu merge decision: tier {result.final_tier.name.lower()}. "
            f"Rules fired: {', '.join(result.rules_fired) or 'none'}. "
            f"Required check: {merge_text.rsplit(': ', 1)[-1]}."
        )
        if assessments:
            comment += "\n\n" + "\n".join(f"- {a.assessor}: {a.rationale}" for a in assessments)
        try:
            deps.github.comment(state["pr_number"], comment)
        except GitHubError as exc:
            deps.log(tid, "error", "supervisor", {"type": "GitHubError", "message": str(exc)})
        facts = {**state.get("facts", {}), "ci_green": bool(ci)}
        return {
            "tier": int(result.final_tier),
            "decision_id": decision_id,
            "stage": "merge",
            "facts": facts,
        }

    def after_merge_decision(state: TicketState) -> str:
        return "merge" if state["tier"] == RiskTier.AUTO else "escalate_decision"

    def merge(state: TicketState) -> TicketState:
        """Merge the pull request, unless the required check is not green by now."""
        tid = state["ticket_id"]
        try:
            if deps.github.ci_status(state["head_sha"]) != "success":
                deps.log(tid, "merge_blocked", "supervisor", {"reason": "ci_not_green"})
                deps.move(tid, TicketStatus.NEEDS_HUMAN)
                return {"status": "needs_human", "outcome": "merge_blocked"}
            merged = deps.github.merge(
                state["pr_number"], cfg.github.merge_method, state["head_sha"]
            )
        except GitHubError as exc:
            return fail(tid, "merge_failed", exc)
        if not merged:
            return fail(tid, "merge_failed", RuntimeError("GitHub did not merge the pull request"))
        facts = {**state.get("facts", {}), "ci_green": True}
        deps.move(tid, TicketStatus.DONE, facts)
        deps.log(tid, "pr_merged", "supervisor", {"number": state["pr_number"]})
        return {"facts": facts, "status": "done"}

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
        "plan": plan,
        "escalate_questions": escalate_questions,
        "escalate_decision": escalate_decision,
        "await_human": await_human,
        "approve_decision": approve_decision,
        "revise": revise,
        "close_ticket": close_ticket,
        "plan_gate": plan_gate,
        "build": build,
        "verify": verify,
        "open_pr": open_pr,
        "draft_pr": draft_pr,
        "critic": critic,
        "merge_decision": merge_decision,
        "merge": merge,
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
    graph.add_conditional_edges("accept", after_accept, ["plan", "plan_gate"])
    graph.add_conditional_edges("plan", after_plan, ["plan_gate", "escalate_decision"])
    graph.add_edge("escalate_questions", "await_human")
    graph.add_edge("escalate_decision", "await_human")
    graph.add_conditional_edges(
        "await_human", after_answer, ["close_ticket", "approve_decision", "revise"]
    )
    graph.add_conditional_edges("revise", after_revise, ["load", END])
    graph.add_edge("close_ticket", END)
    graph.add_conditional_edges("plan_gate", after_gate, ["build", END])
    graph.add_conditional_edges("build", after_build, ["verify", END])
    graph.add_conditional_edges(
        "verify", after_verify, ["build", "critic", "done", "open_pr", "draft_pr", END]
    )
    graph.add_edge("draft_pr", END)
    graph.add_conditional_edges(
        "critic", after_critic, ["build", "open_pr", "draft_pr", "done", END]
    )
    graph.add_conditional_edges("open_pr", after_pr, ["merge_decision", END])
    graph.add_conditional_edges(
        "merge_decision", after_merge_decision, ["merge", "escalate_decision"]
    )
    graph.add_conditional_edges("approve_decision", after_approve, ["plan", "plan_gate", "merge"])
    graph.add_edge("merge", END)
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
    # Without a pending interrupt LangGraph would restart the graph from empty state.
    if not build_graph(deps, prompts_dir).get_state(_thread(ticket_id)).interrupts:
        raise ValueError(f"ticket {ticket_id} has no paused checkpoint to resume")
    return _invoke(deps, ticket_id, Command(resume=answer), prompts_dir)


def is_paused(state: dict[str, Any]) -> bool:
    return bool(state.get("__interrupt__"))
