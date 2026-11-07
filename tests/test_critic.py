"""The critic pass: citation checks, the feedback loop, PR notes and the limits on a critic."""

import json
import subprocess

import pytest

pytest.importorskip("langgraph")

from sifu import db  # noqa: E402
from sifu.critic import (  # noqa: E402
    added_index,
    check_citations,
    confirm_blocker,
    context_lines,
    merge_findings,
    render_diff,
    run_critic,
    to_implementer_findings,
)
from sifu.graph import is_paused, run_ticket  # noqa: E402
from sifu.implementer import ImplementerResult  # noqa: E402
from sifu.models import LLMCriticFinding  # noqa: E402
from sifu.verify import Finding, VerifyResult  # noqa: E402
from tests.fakes import FakeLLM  # noqa: E402

BODY = "Given a Day Plan has 10 Stops, When another is added, Then tooManyStops is thrown."
FILE = "Sources/DayPlan.swift"
SCENARIO = "With 10 Stops in the plan, adding an 11th passes the check and the plan holds 11."


def finding(severity="blocker", line=2, scenario=SCENARIO, file=FILE, quote="count > 10"):
    return {
        "severity": severity,
        "category": "off_by_one",
        "file": file,
        "line": line,
        "quote": quote,
        "message": "The limit check allows one Stop too many.",
        "scenario": scenario,
    }


def reply(*findings):
    return json.dumps({"findings": list(findings)})


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def worktree(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "README.md").write_text("x\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    (tmp_path / "Sources").mkdir()
    (tmp_path / FILE).write_text(
        "struct DayPlan {\n  func add() { if count > 10 { throw X } }\n}\n"
    )
    return tmp_path


class Implementer:
    def __init__(self, worktree):
        self.worktree, self.calls = worktree, []

    def __call__(self, ticket_id, spec_text, conventions, feedback=None, session_id=None):
        self.calls.append(feedback)
        return ImplementerResult(
            ok=True,
            changed_files=[FILE],
            session_id="s1",
            branch=f"sifu/ticket-{ticket_id}",
            worktree=str(self.worktree),
        )


def run(make_deps, make_ticket, cfg, worktree, replies, verifier=None):
    cfg.critic.enabled = True
    llm = FakeLLM(replies)
    deps = make_deps(llm)
    deps.implementer, deps.verifier = Implementer(worktree), verifier
    ticket = make_ticket("T", BODY)
    state = run_ticket(deps, ticket, facts={"plan_accepted": True})
    return deps, ticket, state, llm


# ---- the pieces ---------------------------------------------------------------------------


def test_citations_must_point_at_added_lines_and_name_a_scenario():
    index = added_index([(FILE, 2, "if count > 10"), (FILE, 3, "}")])

    def make(**kw):
        return LLMCriticFinding(**finding(**kw))

    good = make()
    kept, dropped = check_citations(
        [
            good,
            make(file="Other.swift"),
            make(line=9),
            make(scenario="it breaks"),
            make(quote="does not append"),
            make(quote="COUNT >  10"),
        ],
        index,
        6,
    )
    assert kept == [good]
    assert [why for _, why in dropped] == [
        "file not in the diff",
        "line was not added by the change",
        "no concrete failing scenario",
        "quote is not on the cited line",
        "quote is not on the cited line",
    ]


def test_the_diff_is_numbered_and_cut_at_the_limit():
    index = added_index([("A.swift", 2, "a" * 50), ("B.swift", 1, "b" * 50)])
    text, cut = render_diff(index, 80)
    assert cut and "### A.swift\n2: " in text and "[diff cut here]" in text
    assert render_diff(index, 10_000)[1] is False


def test_blockers_on_one_line_merge_and_keep_every_message():
    group = [
        {**finding("blocker"), "critic": "edge_cases", "message": "off by one"},
        {**finding("blocker"), "critic": "contract", "message": "breaks criterion 1"},
        {**finding("major", line=7), "critic": "contract"},
    ]
    merged = merge_findings(group)
    assert len(merged) == 2
    top = next(f for f in merged if f["line"] == 2)
    assert top["critic"] == "contract+edge_cases" and top["message"] == "off by one"
    assert top["also"] == [{"critic": "contract", "message": "breaks criterion 1"}]


def test_a_false_blocker_cannot_bury_a_real_major_on_the_same_line():
    false_blocker = {**finding("blocker"), "critic": "contract", "message": "never appends"}
    real_major = {**finding("major"), "critic": "security", "message": "logs coordinates"}
    merged = merge_findings([false_blocker, real_major])
    assert {(f["severity"], f["message"]) for f in merged} == {
        ("blocker", "never appends"),
        ("major", "logs coordinates"),
    }
    assert [f["message"] for f in to_implementer_findings(merged)] == [
        "never appends Failing scenario: " + SCENARIO
    ]


def test_on_equal_severity_the_first_critics_message_is_kept():
    first = {**finding("major"), "critic": "edge_cases", "message": "first"}
    second = {**finding("major"), "critic": "contract", "message": "second"}
    merged = merge_findings([first, second])
    assert len(merged) == 1 and merged[0]["message"] == "first"
    assert merged[0]["critic"] == "contract+edge_cases"


def test_only_blockers_go_back_to_the_implementer():
    kept = [
        {**finding("blocker"), "critic": "contract"},
        {**finding("major"), "critic": "security"},
    ]
    sent = to_implementer_findings(kept)
    assert len(sent) == 1 and sent[0]["source"] == "critic:contract"
    assert "Failing scenario" in sent[0]["message"]


def test_a_critic_that_never_answers_adds_nothing_and_is_marked_degraded():
    index = added_index([(FILE, 2, "x")])
    report, outcome = run_critic(
        "security",
        spec_text="s",
        index=index,
        checks="build",
        llm=FakeLLM({"Security Critic": "not json"}),
        model="m",
        max_diff_chars=1000,
        min_scenario_words=6,
    )
    assert report.degraded and report.kept == [] and not outcome.ok


# ---- through the graph --------------------------------------------------------------------


def test_a_blocker_goes_back_to_the_implementer_then_the_change_passes(
    conn, cfg, make_deps, make_ticket, worktree
):
    replies = {"Edge Case Critic": [reply(finding()), reply()]}
    deps, ticket, state, _ = run(make_deps, make_ticket, cfg, worktree, replies)
    assert state["status"] == "done"
    impl = deps.implementer
    assert len(impl.calls) == 2 and impl.calls[0] is None
    assert impl.calls[1][0]["source"] == "critic:edge_cases"
    assert impl.calls[1][0]["file"] == FILE and impl.calls[1][0]["line"] == 2


def test_an_invented_citation_is_dropped_and_blocks_nothing(
    conn, cfg, make_deps, make_ticket, worktree
):
    replies = {"Contract Critic": reply(finding(line=99), finding(file="Ghost.swift"))}
    deps, ticket, state, _ = run(make_deps, make_ticket, cfg, worktree, replies)
    assert state["status"] == "done" and len(deps.implementer.calls) == 1
    event = [e for e in db.list_events(conn, ticket) if e["kind"] == "critic_result"][-1]
    payload = json.loads(event["payload"])
    assert payload["kept"] == [] and len(payload["dropped"]) == 2


def test_the_same_blocker_twice_stops_for_lack_of_progress(
    conn, cfg, make_deps, make_ticket, worktree
):
    cfg.router.max_build_iterations = 5
    replies = {"Edge Case Critic": reply(finding())}
    deps, ticket, state, _ = run(make_deps, make_ticket, cfg, worktree, replies)
    assert state["outcome"] == "critic_no_progress" and len(deps.implementer.calls) == 2
    assert db.get_ticket(conn, ticket)["status"] == "needs_human"


def test_the_iteration_cap_applies_to_critic_blockers(conn, cfg, make_deps, make_ticket, worktree):
    cfg.router.max_build_iterations = 1
    replies = {"Edge Case Critic": reply(finding())}
    _, ticket, state, _ = run(make_deps, make_ticket, cfg, worktree, replies)
    assert state["outcome"] == "critic_cap"
    assert db.get_ticket(conn, ticket)["status"] == "needs_human"


def test_a_critic_never_runs_when_a_deterministic_check_failed(
    conn, cfg, make_deps, make_ticket, worktree
):
    cfg.router.max_build_iterations = 1
    verifier = lambda tid, spec: VerifyResult(False, [Finding("build", "bad", FILE, 1)])  # noqa: E731
    _, _, state, llm = run(make_deps, make_ticket, cfg, worktree, {}, verifier)
    assert state["outcome"] == "verify_cap"
    assert not [c for c in llm.calls if c["key"].endswith("Critic")]


def test_a_critic_that_finds_nothing_cannot_override_a_failed_check(
    conn, cfg, make_deps, make_ticket, worktree
):
    cfg.router.max_build_iterations = 1
    verifier = lambda tid, spec: VerifyResult(False, [Finding("test", "red", FILE, 1)])  # noqa: E731
    _, ticket, state, _ = run(make_deps, make_ticket, cfg, worktree, {}, verifier)
    assert state["status"] == "needs_human"


class Hub:
    def __init__(self):
        self.comments = []

    def create_pr(self, **kw):
        return 4, "https://github.com/o/r/pull/4"

    def comment(self, number, body):
        self.comments.append(body)

    def ci_status(self, sha):
        return "success"

    def merge(self, number, method, sha):
        return True


def test_major_and_minor_findings_become_a_pr_comment(conn, cfg, make_deps, make_ticket, worktree):
    cfg.critic.enabled = True
    replies = {"Security Critic": reply(finding("major"), finding("minor", line=3, quote="}"))}
    deps = make_deps(FakeLLM(replies))
    hub = Hub()
    deps.implementer = Implementer(worktree)
    deps.github, deps.publisher, deps.sleep = hub, lambda w, b, m: "abc", lambda s: None
    ticket = make_ticket("T", BODY)
    state = run_ticket(deps, ticket, facts={"plan_accepted": True})
    assert is_paused(state) or state["status"] == "done"
    notes = [c for c in hub.comments if c.startswith("Sifu critic notes")]
    assert len(notes) == 1 and "**major**" in notes[0] and "**minor**" in notes[0]
    assert len(deps.implementer.calls) == 1  # nothing blocked


# ---- the few-shot examples must be self-consistent --------------------------------------------


@pytest.mark.parametrize("name", ["edge_cases", "contract", "security"])
def test_every_example_cites_a_numbered_line_that_holds_its_quote(name):
    from sifu.critic import load_critic

    _, examples = load_critic(name)
    for ex in examples:
        numbered = {}
        current = ""
        for row in ex["change"].splitlines():
            if row.startswith("### "):
                current = row[4:]
            elif ": " in row and row.split(": ", 1)[0].isdigit():
                number, text = row.split(": ", 1)
                numbered[(current, int(number))] = text
        for f in ex["output"]["findings"]:
            text = numbered[(f["file"], f["line"])]
            assert " ".join(f["quote"].split()) in " ".join(text.split())
            assert len(f["scenario"].split()) >= 6


# ---- the confirmation pass --------------------------------------------------------------


REFUTED = json.dumps({"verdict": "refuted", "reason": "line 3 does it"})
INDEX = added_index([(FILE, n, f"line {n}") for n in range(1, 30)])


def test_the_checker_sees_only_the_lines_near_the_cited_one():
    text = context_lines(INDEX, FILE, 10, 2)
    assert text.splitlines() == [
        "8: line 8",
        "9: line 9",
        "10: line 10",
        "11: line 11",
        "12: line 12",
    ]


def test_a_refuted_blocker_is_not_confirmed_and_a_dead_checker_keeps_it():
    f = {**finding(), "critic": "contract"}
    kw = {"index": INDEX, "model": "m", "radius": 3}
    ok, reason, _ = confirm_blocker(f, llm=FakeLLM({"Claim Checker": REFUTED}), **kw)
    assert not ok and "line 3" in reason
    ok, reason, outcome = confirm_blocker(f, llm=FakeLLM({"Claim Checker": "garbage"}), **kw)
    assert ok and not outcome.ok and "stands" in reason


def test_a_refuted_blocker_is_dropped_and_never_reaches_the_implementer(
    conn, cfg, make_deps, make_ticket, worktree
):
    replies = {"Contract Critic": reply(finding()), "Claim Checker": REFUTED}
    deps, ticket, state, llm = run(make_deps, make_ticket, cfg, worktree, replies)
    assert state["status"] == "done" and len(deps.implementer.calls) == 1
    event = [e for e in db.list_events(conn, ticket) if e["kind"] == "critic_result"][-1]
    payload = json.loads(event["payload"])
    assert payload["kept"] == []
    assert payload["dropped"][0]["why"].startswith("refuted on confirmation")
    checker_calls = [c for c in llm.calls if c["key"] == "Claim Checker"]
    assert [c["model"] for c in checker_calls] == [cfg.models.author]  # not the critics' model


def test_a_confirmed_blocker_still_goes_back_to_the_implementer(
    conn, cfg, make_deps, make_ticket, worktree
):
    replies = {"Edge Case Critic": [reply(finding()), reply()]}
    deps, _, state, llm = run(make_deps, make_ticket, cfg, worktree, replies)
    assert len(deps.implementer.calls) == 2
    assert len([c for c in llm.calls if c["key"] == "Claim Checker"]) == 1


def test_only_blockers_are_checked_and_the_check_can_be_turned_off(
    conn, cfg, make_deps, make_ticket, worktree
):
    replies = {"Security Critic": reply(finding("major"), finding("minor", line=3, quote="}"))}
    _, _, _, llm = run(make_deps, make_ticket, cfg, worktree, replies)
    assert not [c for c in llm.calls if c["key"] == "Claim Checker"]
    cfg.critic.confirm_blockers = False
    deps, _, _, llm = run(
        make_deps, make_ticket, cfg, worktree, {"Edge Case Critic": reply(finding())}
    )
    assert not [c for c in llm.calls if c["key"] == "Claim Checker"]
