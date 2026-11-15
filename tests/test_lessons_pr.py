"""Lessons go to conventions.md only through a PR the router sends to a human, and never merged."""

import subprocess

import pytest

from sifu import db, lessons_pr
from sifu.config import GithubConfig, RouterConfig
from sifu.implementer import git_output
from sifu.lessons_pr import Lesson, load_lessons, prepare, publish, render_decision
from sifu.models import RiskTier


def git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "product"
    git("init", "-q", "-b", "main", str(path), cwd=tmp_path)
    git("config", "user.email", "t@example.com", cwd=path)
    git("config", "user.name", "t", cwd=path)
    (path / "conventions.md").write_text("# Conventions\n\n## Lessons\n\n- Keep it short.\n")
    git("add", ".", cwd=path)
    git("commit", "-q", "-m", "init", cwd=path)
    return path


ROUTER = RouterConfig(protected_paths=["conventions.md", ".github/"])
LESSON = Lesson(lesson="Change only the files the spec lists.", evidence="PR #3 took three rounds")


class FakeGitHub:
    def __init__(self):
        self.prs, self.comments = [], []

    def create_pr(self, *, head, base, title, body, draft=False):
        self.prs.append({"head": head, "base": base, "title": title, "body": body})
        return 9, "https://github.com/o/r/pull/9"

    def comment(self, number, body):
        self.comments.append((number, body))


def test_lessons_are_added_and_the_router_sends_the_change_to_a_human(repo, tmp_path):
    old = Lesson(lesson="Keep it short.", evidence="already there")
    change = prepare(repo, tmp_path / "wt", [LESSON, old], ROUTER, "lessons-1")
    assert change.branch == "sifu/lessons-1" and change.touched == ["conventions.md"]
    assert change.added == [LESSON] and change.skipped == [old]
    assert "+- Change only the files the spec lists." in change.diff
    assert change.decision.final_tier == RiskTier.HUMAN
    assert "touches_protected_path" in change.decision.rules_fired
    # the product checkout itself is untouched
    assert "Change only" not in (repo / "conventions.md").read_text()


def test_nothing_new_is_refused_and_the_worktree_is_removed(repo, tmp_path):
    old = Lesson(lesson="Keep it short.", evidence="x")
    with pytest.raises(ValueError):
        prepare(repo, tmp_path / "wt", [old], ROUTER, "lessons-2")
    assert not (tmp_path / "wt" / "lessons-2").exists()
    assert "sifu/lessons-2" not in git("branch", cwd=repo)


def test_it_refuses_when_conventions_is_no_longer_protected(repo, tmp_path):
    open_router = RouterConfig(protected_paths=[".github/"], merge_mode="auto")
    with pytest.raises(RuntimeError, match="touches_protected_path"):
        prepare(repo, tmp_path / "wt", [LESSON], open_router, "lessons-3")
    assert not (tmp_path / "wt" / "lessons-3").exists()


def test_publish_pushes_opens_the_pr_and_comments_the_decision_but_never_merges(repo, tmp_path):
    remote = tmp_path / "remote.git"
    git("init", "--bare", "-q", str(remote), cwd=tmp_path)
    change = prepare(repo, tmp_path / "wt", [LESSON], ROUTER, "lessons-4")
    github = FakeGitHub()
    out = publish(change, GithubConfig(repo="o/r"), github, None, remote=str(remote))
    assert out["pr_number"] == 9 and out["head_sha"] == git(
        "rev-parse", "sifu/lessons-4", cwd=remote
    )
    pr = github.prs[0]
    assert pr["base"] == "main" and pr["head"] == "sifu/lessons-4"
    assert "PR #3 took three rounds" in pr["body"] and pr["title"].startswith("Add 1 lesson ")
    assert github.comments == [(9, render_decision(change.decision))]
    assert "HUMAN" in github.comments[0][1]
    assert not hasattr(github, "merge")  # the client it is given cannot merge, and is not asked to


def test_lessons_load_from_yaml(tmp_path):
    path = tmp_path / "l.yaml"
    path.write_text('- lesson: A rule.\n  evidence: "PR #4"\n')  # quoted: # starts a comment
    assert load_lessons(path) == [Lesson(lesson="A rule.", evidence="PR #4")]


@pytest.fixture
def set_up(cfg, repo, tmp_path):
    """The shared config pointed at the temporary product repo, with conventions.md protected."""
    cfg.workspace.product_repo_path = str(repo)
    cfg.workspace.worktrees_dir = str(tmp_path / "wt-run")
    cfg.router.protected_paths = ["conventions.md", ".github/"]
    return cfg


def test_run_dry_returns_the_diff_and_decision_and_leaves_nothing_behind(repo, set_up):
    out = lessons_pr.run(set_up, [Lesson(lesson="Trim names.", evidence="ticket 4")], push=False)
    assert "+- Trim names." in out["diff"] and out["tier"] == "HUMAN"
    assert out["pr_url"] is None and out["added"] == ["Trim names."]
    assert git_output(repo, "branch", "--list", "sifu/lessons-*").strip() == ""


def test_run_with_nothing_new_raises_and_leaves_nothing_behind(repo, set_up):
    with pytest.raises(ValueError, match="already in conventions.md"):
        lessons_pr.run(set_up, [Lesson(lesson="Keep it short.", evidence="e")], push=False)
    assert git_output(repo, "branch", "--list", "sifu/lessons-*").strip() == ""


def test_run_push_opens_the_pr_and_records_it_as_a_ticket(repo, set_up, tmp_path):
    remote = tmp_path / "remote.git"
    git("init", "--bare", "-q", str(remote), cwd=tmp_path)
    out = lessons_pr.run(
        set_up,
        [Lesson(lesson="Trim names.", evidence="ticket 4")],
        push=True,
        db_path=tmp_path / "l.db",
        project="TripPlanner",
        client=FakeGitHub(),
        token=None,
        remote=str(remote),
    )
    assert out["pr_url"]
    conn = db.connect(tmp_path / "l.db")
    [ticket] = conn.execute("SELECT * FROM tickets").fetchall()
    assert ticket["title"] == "Lessons for conventions.md (1)"
    assert "pr_opened" in [e["kind"] for e in db.list_events(conn, ticket["id"])]
