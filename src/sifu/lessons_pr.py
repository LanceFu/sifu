"""Propose lessons for the product's ``conventions.md`` as a pull request that a human must approve.

``conventions.md`` is a protected path, so the router's floor rule sends any change to it to a
human. This module relies on that and checks it: it refuses to go on unless the router's decision
is the human tier *because of* the protected-path rule. It opens the PR and comments the decision;
it never merges.

Each lesson carries the evidence it came from (a PR, a ticket, an eval run). The bullet added to
``conventions.md`` is the rule alone; the evidence goes in the PR description for the reviewer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel, SecretStr

from sifu.config import GithubConfig, RouterConfig
from sifu.implementer import changed_files, git_output
from sifu.lessons import append_lesson
from sifu.models import DecisionContext, DecisionResult, DecisionType, RiskTier
from sifu.pr_flow import publish_branch
from sifu.router import decide

CONVENTIONS = "conventions.md"
PROTECTED_RULE = "touches_protected_path"


class Lesson(BaseModel):
    """One lesson to add to conventions.md, with the evidence behind it."""

    lesson: str  # the rule, one sentence, as it will read in conventions.md
    evidence: str  # where it was learned, for the PR description


def load_lessons(path: str | Path) -> list[Lesson]:
    """Read a YAML list of ``{lesson, evidence}`` entries."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    return [Lesson.model_validate(item) for item in raw]


class PullRequests(Protocol):
    """The pull request calls the lessons flow needs from a GitHub client."""

    def create_pr(
        self, *, head: str, base: str, title: str, body: str, draft: bool = False
    ) -> tuple[int, str]:
        """Open a pull request and return its number and URL."""

    def comment(self, number: int, body: str) -> None:
        """Post a comment on a pull request."""


@dataclass
class LessonsChange:
    """A prepared conventions.md change and the router's decision on it."""

    worktree: Path
    branch: str
    added: list[Lesson]
    skipped: list[Lesson]  # already in conventions.md
    diff: str
    decision: DecisionResult
    touched: list[str] = field(default_factory=list)


def prepare(
    repo: str | Path,
    worktrees_dir: str | Path,
    lessons: list[Lesson],
    router_cfg: RouterConfig,
    name: str,
    base: str = "HEAD",
) -> LessonsChange:
    """Add the lessons to conventions.md in a new worktree and route the change.

    Raises ``ValueError`` when no lesson is new, and ``RuntimeError`` when the router would not
    send the change to a human through the protected-path rule. Either way the worktree is removed.
    """
    repo = Path(repo).expanduser()
    path = Path(worktrees_dir).expanduser() / name
    branch = f"sifu/{name}"
    path.parent.mkdir(parents=True, exist_ok=True)
    git_output(repo, "worktree", "add", "-b", branch, str(path), base)
    try:
        added, skipped = [], []
        for item in lessons:
            (added if append_lesson(path / CONVENTIONS, item.lesson) else skipped).append(item)
        if not added:
            raise ValueError("every lesson is already in conventions.md")
        touched = changed_files(path)
        context = DecisionContext(decision_type=DecisionType.MERGE, touched_paths=touched)
        decision = decide(context, [], router_cfg)
        if decision.final_tier != RiskTier.HUMAN or PROTECTED_RULE not in decision.rules_fired:
            raise RuntimeError(
                f"the router did not send {CONVENTIONS} to a human through {PROTECTED_RULE}"
                f" (tier {decision.final_tier.name}, rules {decision.rules_fired}); is it still"
                " in router.protected_paths?"
            )
        diff = git_output(path, "diff")
    except Exception:
        discard(repo, path, branch)
        raise
    return LessonsChange(path, branch, added, skipped, diff, decision, touched)


def discard(repo: str | Path, worktree: str | Path, branch: str) -> None:
    """Remove a worktree and its branch, for a dry run or a refused change."""
    repo = Path(repo).expanduser()
    git_output(repo, "worktree", "remove", "--force", str(worktree))
    git_output(repo, "branch", "-D", branch)


def render_body(change: LessonsChange) -> str:
    """Render the pull request description for a lessons change."""
    rows = "\n".join(f"- **{item.lesson}**\n  Evidence: {item.evidence}" for item in change.added)
    return (
        "Opened by Sifu. It adds lessons to `conventions.md` under `## Lessons`, learned from "
        "real failures.\n\n"
        f"## Lessons\n\n{rows}\n\n"
        "`conventions.md` is a protected file, so the router sends this PR to a human. "
        "Sifu does not merge it.\n"
    )


def render_decision(decision: DecisionResult) -> str:
    """Render the router's decision as a pull request comment."""
    rules = ", ".join(f"`{r}`" for r in decision.rules_fired) or "none"
    return (
        f"**Sifu merge decision:** tier {decision.final_tier.name} "
        f"(floor {decision.floor_tier.name}).\n\nRules fired: {rules}.\n\n"
        "A human must review and merge this PR."
    )


def publish(
    change: LessonsChange,
    github_cfg: GithubConfig,
    client: PullRequests,
    token: SecretStr | None,
    remote: str | None = None,
) -> dict[str, Any]:
    """Push the branch, open the PR and comment the router's decision. Never merges."""
    count = len(change.added)
    title = f"Add {count} lesson{'s' if count != 1 else ''} to conventions.md"
    sha = publish_branch(github_cfg, change.worktree, change.branch, title, token, remote)
    number, url = client.create_pr(
        head=change.branch, base=github_cfg.base_branch, title=title, body=render_body(change)
    )
    client.comment(number, render_decision(change.decision))
    return {"pr_number": number, "pr_url": url, "head_sha": sha, "branch": change.branch}
