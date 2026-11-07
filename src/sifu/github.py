"""GitHub API wrapper: open a pull request, comment, read CI status and merge.

It wraps a PyGithub ``Repository`` (or anything with the same methods, which is how the tests run).
The token is read through ``sifu.credentials`` as a ``SecretStr`` and is only handed to PyGithub.
"""

from __future__ import annotations

from typing import Any, Literal

from sifu.config import GithubConfig
from sifu.credentials import get_github_token

CIStatus = Literal["success", "failure", "pending"]


class GitHubError(RuntimeError):
    """Any failure talking to GitHub. The message never contains the token."""


class GitHubClient:
    def __init__(self, repo: Any, required_check: str = "test") -> None:
        self.repo = repo
        self.required_check = required_check

    @classmethod
    def from_config(cls, cfg: GithubConfig) -> GitHubClient:
        from github import Auth, Github  # imported here so the rest of Sifu works without it

        token = get_github_token(cfg)
        client = Github(auth=Auth.Token(token.get_secret_value()))
        return cls(client.get_repo(cfg.repo), cfg.required_check)

    def _call(self, what: str, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # PyGithub raises several types
            raise GitHubError(f"{what} failed: {type(exc).__name__}: {str(exc)[:200]}") from exc

    def create_pr(
        self, *, head: str, base: str, title: str, body: str, draft: bool = False
    ) -> tuple[int, str]:
        pr = self._call(
            "create pull request",
            self.repo.create_pull,
            title=title,
            body=body,
            head=head,
            base=base,
            draft=draft,
        )
        return int(pr.number), str(pr.html_url)

    def comment(self, number: int, body: str) -> None:
        pr = self._call("get pull request", self.repo.get_pull, number)
        self._call("comment", pr.create_issue_comment, body)

    def ci_status(self, sha: str) -> CIStatus:
        """The state of the required check on a commit. A missing check counts as pending."""
        commit = self._call("get commit", self.repo.get_commit, sha)
        runs = self._call("list check runs", lambda: list(commit.get_check_runs()))
        mine = [r for r in runs if r.name == self.required_check]
        if not mine or any(r.status != "completed" for r in mine):
            return "pending"
        return "success" if all(r.conclusion == "success" for r in mine) else "failure"

    def merge(self, number: int, method: str, sha: str) -> bool:
        pr = self._call("get pull request", self.repo.get_pull, number)
        result = self._call("merge", pr.merge, merge_method=method, sha=sha)
        return bool(getattr(result, "merged", False))
