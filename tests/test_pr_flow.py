"""verify -> open PR -> merge decision -> merge, with a fake GitHub and a fake publisher."""

import json
import subprocess

import pytest

pytest.importorskip("langgraph")

from pydantic import SecretStr  # noqa: E402

from sifu import db  # noqa: E402
from sifu.config import GithubConfig  # noqa: E402
from sifu.github import GitHubError  # noqa: E402
from sifu.graph import is_paused, resume_ticket, run_ticket  # noqa: E402
from sifu.implementer import ImplementerResult  # noqa: E402
from sifu.pr_flow import PublishError, publish_branch, render_findings, render_pr_body  # noqa: E402
from sifu.verify import Finding, VerifyResult  # noqa: E402
from tests.fakes import FakeLLM  # noqa: E402

BODY = "Given a Traveller saves a Trip, When the name is blank, Then nameBlank is thrown."


class Implementer:
    def __init__(self, files=("Sources/Trip.swift",)):
        self.files = list(files)

    def __call__(self, ticket_id, spec_text, conventions, feedback=None, session_id=None):
        return ImplementerResult(
            ok=True,
            changed_files=self.files,
            session_id="s1",
            branch=f"sifu/ticket-{ticket_id}",
            worktree="/tmp/wt",
        )


class FakeGitHub:
    def __init__(self, ci=("success",), fail_on=None, merged=True):
        self.ci = list(ci)
        self.fail_on = fail_on
        self.merged = merged
        self.comments, self.merges, self.polls = [], [], 0

    def _maybe_fail(self, what):
        if self.fail_on == what:
            raise GitHubError(f"{what} failed")

    def create_pr(self, *, head, base, title, body, draft=False):
        self._maybe_fail("create_pr")
        self.pr = {"head": head, "base": base, "title": title, "body": body, "draft": draft}
        return 7, "https://github.com/o/r/pull/7"

    def comment(self, number, body):
        self.comments.append((number, body))

    def ci_status(self, sha):
        self._maybe_fail("ci_status")
        self.polls += 1
        return self.ci.pop(0) if len(self.ci) > 1 else self.ci[0]

    def merge(self, number, method, sha):
        self._maybe_fail("merge")
        self.merges.append((number, method, sha))
        return self.merged


def publisher(worktree, branch, message):
    return "abc123"


def run(make_deps, make_ticket, github, files=("Sources/Trip.swift",), llm=None, pub=publisher):
    deps = make_deps(llm or FakeLLM())
    deps.implementer, deps.verifier = Implementer(files), None
    deps.github, deps.publisher, deps.sleep = github, pub, lambda s: None
    ticket = make_ticket("T", BODY)
    return deps, ticket, run_ticket(deps, ticket, facts={"plan_accepted": True})


def kinds(conn, ticket):
    return [e["kind"] for e in db.list_events(conn, ticket)]


def test_a_green_small_change_is_merged_automatically(conn, make_deps, make_ticket):
    gh = FakeGitHub()
    _, ticket, state = run(make_deps, make_ticket, gh)
    assert state["status"] == "done"
    assert gh.merges == [(7, "squash", "abc123")]
    assert gh.pr["head"] == f"sifu/ticket-{ticket}" and gh.pr["base"] == "main"
    assert "pr_opened" in kinds(conn, ticket) and "pr_merged" in kinds(conn, ticket)
    assert "tier auto" in gh.comments[0][1]
    assert db.get_ticket(conn, ticket)["status"] == "done"


def test_a_red_check_is_a_floor_rule_and_asks_a_human(conn, make_deps, make_ticket):
    gh = FakeGitHub(ci=("failure",))
    _, ticket, state = run(make_deps, make_ticket, gh)
    assert is_paused(state) and gh.merges == []
    assert "ci_not_green" in gh.comments[0][1]
    assert "pull/7" in json.dumps(state["__interrupt__"][0].value)
    assert db.get_ticket(conn, ticket)["status"] != "done"


def test_a_protected_file_never_merges_by_itself(conn, make_deps, make_ticket):
    gh = FakeGitHub()
    _, _, state = run(make_deps, make_ticket, gh, files=(".github/workflows/ci.yml",))
    assert is_paused(state) and gh.merges == []
    assert "touches_protected_path" in gh.comments[0][1]


def test_approving_a_paused_merge_merges_the_pull_request(conn, make_deps, make_ticket):
    gh = FakeGitHub(ci=("failure", "success"))  # the human re-ran the check before approving
    deps, ticket, state = run(make_deps, make_ticket, gh)
    assert is_paused(state)
    final = resume_ticket(deps, ticket, "approve")
    assert final["status"] == "done" and len(gh.merges) == 1
    assert db.get_ticket(conn, ticket)["status"] == "done"


def test_approving_while_the_check_is_still_red_blocks_the_merge(conn, make_deps, make_ticket):
    gh = FakeGitHub(ci=("failure",))
    deps, ticket, state = run(make_deps, make_ticket, gh)
    final = resume_ticket(deps, ticket, "approve")
    assert final["outcome"] == "merge_blocked" and gh.merges == []
    assert db.get_ticket(conn, ticket)["status"] == "needs_human"


def test_a_human_who_does_not_approve_closes_the_ticket_and_leaves_the_pr(
    conn, make_deps, make_ticket
):
    gh = FakeGitHub(ci=("failure",))
    deps, ticket, _ = run(make_deps, make_ticket, gh)
    final = resume_ticket(deps, ticket, "needs more tests")
    assert gh.merges == [] and final["status"] != "done"


def test_manual_merge_mode_always_asks_a_human(conn, cfg, make_deps, make_ticket):
    cfg.router.merge_mode = "manual"
    gh = FakeGitHub()
    _, _, state = run(make_deps, make_ticket, gh)
    assert is_paused(state) and gh.merges == []
    assert "merge_mode_manual" in gh.comments[0][1]


def test_auto_mode_merges_without_asking_the_models(conn, cfg, make_deps, make_ticket):
    cfg.router.merge_mode = "auto"
    llm = FakeLLM()
    gh = FakeGitHub()
    deps = make_deps(llm)
    deps.implementer, deps.verifier = Implementer(), None
    deps.github, deps.publisher, deps.sleep = gh, publisher, lambda s: None
    ticket = make_ticket("T", BODY)
    before = len(llm.calls)
    state = run_ticket(deps, ticket, facts={"plan_accepted": True})
    risk_calls = [c for c in llm.calls[before:] if c["key"] == "Risk Assessor"]
    merge_risk = [c for c in risk_calls if "Changed files" in c["user"]]
    assert state["status"] == "done" and merge_risk == []


def test_auto_mode_still_obeys_the_floor(conn, cfg, make_deps, make_ticket):
    cfg.router.merge_mode = "auto"
    gh = FakeGitHub(ci=("failure",))
    _, _, state = run(make_deps, make_ticket, gh)
    assert is_paused(state) and gh.merges == []


def test_a_pending_check_that_never_finishes_asks_a_human(conn, cfg, make_deps, make_ticket):
    cfg.github.ci_timeout_seconds, cfg.github.ci_poll_seconds = 30, 10
    gh = FakeGitHub(ci=("pending",))
    _, _, state = run(make_deps, make_ticket, gh)
    assert is_paused(state) and gh.merges == []
    assert gh.polls == 4  # at 0, 10, 20 and 30 seconds
    assert "ci_not_green" in gh.comments[0][1]


def test_a_failed_pull_request_fails_the_ticket(conn, make_deps, make_ticket):
    _, ticket, state = run(make_deps, make_ticket, FakeGitHub(fail_on="create_pr"))
    assert state["status"] == "failed" and state["outcome"] == "pr_failed"
    assert db.get_ticket(conn, ticket)["status"] == "failed"


def test_a_failed_push_fails_the_ticket(conn, make_deps, make_ticket):
    def broken(worktree, branch, message):
        raise PublishError("git push failed: denied")

    gh = FakeGitHub()
    _, ticket, state = run(make_deps, make_ticket, gh, pub=broken)
    assert state["outcome"] == "pr_failed" and not hasattr(gh, "pr")
    assert db.get_ticket(conn, ticket)["status"] == "failed"


def test_a_failed_merge_call_fails_the_ticket(conn, make_deps, make_ticket):
    gh = FakeGitHub(merged=False)
    _, _, state = run(make_deps, make_ticket, gh)
    assert state["outcome"] == "merge_failed"


def test_without_github_the_ticket_ends_at_done_as_before(conn, make_deps, make_ticket):
    deps = make_deps(FakeLLM())
    deps.implementer, deps.verifier = Implementer(), None
    ticket = make_ticket("T", BODY)
    assert run_ticket(deps, ticket, facts={"plan_accepted": True})["status"] == "done"


def test_a_verify_cap_opens_a_draft_pr_with_the_findings(conn, cfg, make_deps, make_ticket):
    cfg.router.max_build_iterations = 2
    deps = make_deps(FakeLLM())
    deps.implementer = Implementer()
    counter = iter(range(100))
    deps.verifier = lambda tid, spec: VerifyResult(
        False, [Finding("swiftlint", f"Too long {next(counter)}", "Sources/Trip.swift", 4, "len")]
    )
    gh = FakeGitHub()
    deps.github, deps.publisher, deps.sleep = gh, publisher, lambda s: None
    ticket = make_ticket("T", BODY)
    state = run_ticket(deps, ticket, facts={"plan_accepted": True})
    assert state["outcome"] == "verify_cap" and gh.merges == []
    assert gh.pr["draft"] is True and gh.pr["title"].startswith("[Draft]")
    assert "swiftlint [len] Sources/Trip.swift:4" in gh.comments[0][1]
    assert "draft_pr_opened" in kinds(conn, ticket)
    assert db.get_ticket(conn, ticket)["status"] == "needs_human"


def test_a_draft_pr_failure_leaves_the_ticket_waiting_for_a_human(
    conn, cfg, make_deps, make_ticket
):
    cfg.router.max_build_iterations = 1
    deps = make_deps(FakeLLM())
    deps.implementer = Implementer()
    deps.verifier = lambda tid, spec: VerifyResult(False, [Finding("build", "boom", "a.swift", 1)])
    deps.github, deps.publisher = FakeGitHub(fail_on="create_pr"), publisher
    ticket = make_ticket("T", BODY)
    state = run_ticket(deps, ticket, facts={"plan_accepted": True})
    assert state["outcome"] == "verify_cap"
    assert db.get_ticket(conn, ticket)["status"] == "needs_human"


def test_findings_comment_names_the_reason():
    text = render_findings("verify_no_progress", 2, [{"source": "build", "message": "bad"}])
    assert "same findings came back" in text and "- build: bad" in text


# ---- publish_branch against a local bare remote -------------------------------------------


def git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_publish_branch_commits_and_pushes(tmp_path):
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    git("init", "--bare", "-q", str(remote), cwd=tmp_path)
    git("init", "-q", "-b", "feature", str(work), cwd=tmp_path)
    (work / "a.txt").write_text("hello")
    cfg = GithubConfig(repo="o/r")
    sha = publish_branch(cfg, work, "feature", "Add a", remote=str(remote))
    assert sha == git("rev-parse", "feature", cwd=remote)
    assert (
        git("log", "-1", "--format=%an <%ae>", cwd=work) == "Sifu <sifu@users.noreply.github.com>"
    )
    assert publish_branch(cfg, work, "feature", "again", remote=str(remote)) == sha  # nothing new


def test_publish_branch_reports_a_failed_push_without_the_token(tmp_path):
    work = tmp_path / "work"
    git("init", "-q", "-b", "feature", str(work), cwd=tmp_path)
    (work / "a.txt").write_text("x")
    token = SecretStr("ghp_supersecret")
    with pytest.raises(PublishError) as err:
        publish_branch(
            GithubConfig(repo="o/r"), work, "feature", "m", token, remote=str(tmp_path / "nope")
        )
    assert "ghp_supersecret" not in str(err.value)


def test_the_pr_body_lists_files_and_checks():
    body = render_pr_body(
        5,
        "Spec text",
        ["Sources/Trip.swift"],
        {"steps": [{"name": "build", "ok": True}, {"name": "test", "ok": False}]},
    )
    assert "ticket 5" in body and "`Sources/Trip.swift`" in body
    assert "build ok, test failed" in body
