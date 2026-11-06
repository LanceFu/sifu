"""Publish a ticket's work: commit what the implementer left, push the branch, describe the PR."""

from __future__ import annotations

import base64
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from sifu.config import GithubConfig
from sifu.implementer import git_output


class PublishError(RuntimeError):
    """Committing or pushing failed."""


def _push_env(token: SecretStr | None) -> dict[str, str]:
    """A minimal environment. The token travels as a git config env var, never in argv."""
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR") if k in os.environ}
    env["GIT_TERMINAL_PROMPT"] = "0"
    if token is not None:
        basic = base64.b64encode(f"x-access-token:{token.get_secret_value()}".encode()).decode()
        env.update(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="http.extraheader",
            GIT_CONFIG_VALUE_0=f"Authorization: Basic {basic}",
        )
    return env


def publish_branch(
    cfg: GithubConfig,
    worktree: Path | str,
    branch: str,
    message: str,
    token: SecretStr | None = None,
    remote: str | None = None,
) -> str:
    """Commit any uncommitted work, push ``branch`` and return the head commit sha."""
    tree = Path(worktree)
    identity = ["-c", f"user.name={cfg.commit_name}", "-c", f"user.email={cfg.commit_email}"]
    try:
        git_output(tree, "add", "-A")
        if git_output(tree, "status", "--porcelain").strip():
            git_output(tree, *identity, "commit", "-q", "-m", message)
        url = remote or f"https://github.com/{cfg.repo}.git"
        done = subprocess.run(
            ["git", "-C", str(tree), "push", url, f"{branch}:{branch}"],
            env=_push_env(token),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if done.returncode != 0:
            raise PublishError(f"git push failed: {_scrub(done.stderr, token)}")
        return git_output(tree, "rev-parse", "HEAD").strip()
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        if isinstance(exc, PublishError):
            raise
        raise PublishError(_scrub(str(exc), token)) from exc


def _scrub(text: str, token: SecretStr | None) -> str:
    text = text.strip()[:300]
    return text.replace(token.get_secret_value(), "***") if token is not None else text


def render_pr_body(
    ticket_id: int,
    spec_text: str,
    changed_files: list[str],
    verify: Mapping[str, Any] | None,
) -> str:
    steps = ", ".join(
        f"{s['name']} {'ok' if s.get('ok') else 'failed'}" for s in (verify or {}).get("steps", [])
    )
    files = "\n".join(f"- `{f}`" for f in changed_files) or "- none"
    return (
        f"Opened by Sifu for ticket {ticket_id}.\n\n## Specification\n\n{spec_text.strip()}\n\n"
        f"## Changed files\n\n{files}\n\n## Checks run by Sifu\n\n{steps or 'not run'}\n"
    )
