"""GitHubClient against a fake PyGithub repository."""

from types import SimpleNamespace as NS

import pytest

from sifu.github import GitHubClient, GitHubError


class Repo:
    def __init__(self, runs=(), merge_ok=True, boom=None):
        self.runs, self.merge_ok, self.boom = list(runs), merge_ok, boom
        self.comments, self.merged_with = [], None

    def _check(self):
        if self.boom:
            raise RuntimeError(self.boom)

    def create_pull(self, **kw):
        self._check()
        self.created = kw
        return NS(number=3, html_url="https://github.com/o/r/pull/3")

    def get_pull(self, number):
        self._check()
        return NS(
            create_issue_comment=self.comments.append,
            merge=lambda **kw: setattr(self, "merged_with", kw) or NS(merged=self.merge_ok),
        )

    def get_commit(self, sha):
        self._check()
        return NS(get_check_runs=lambda: self.runs)


def run(name, status="completed", conclusion="success"):
    return NS(name=name, status=status, conclusion=conclusion)


def client(repo):
    return GitHubClient(repo, "test")


def test_create_pr_returns_number_and_url():
    assert client(Repo()).create_pr(head="h", base="main", title="t", body="b") == (
        3,
        "https://github.com/o/r/pull/3",
    )


@pytest.mark.parametrize(
    "runs,expected",
    [
        ([run("test")], "success"),
        ([run("test", conclusion="failure")], "failure"),
        ([run("test", status="in_progress", conclusion=None)], "pending"),
        ([], "pending"),
        ([run("lint", conclusion="failure")], "pending"),  # only the required check counts
    ],
)
def test_ci_status_reads_the_required_check_only(runs, expected):
    assert client(Repo(runs)).ci_status("sha") == expected


def test_merge_passes_method_and_sha():
    repo = Repo()
    assert client(repo).merge(3, "squash", "abc") is True
    assert repo.merged_with == {"merge_method": "squash", "sha": "abc"}


def test_comment_is_posted():
    repo = Repo()
    client(repo).comment(3, "hi")
    assert repo.comments == ["hi"]


def test_any_failure_becomes_a_github_error():
    with pytest.raises(GitHubError):
        client(Repo(boom="rate limited")).ci_status("sha")


def test_a_draft_flag_reaches_github():
    repo = Repo()
    client(repo).create_pr(head="h", base="main", title="t", body="b", draft=True)
    assert repo.created["draft"] is True
