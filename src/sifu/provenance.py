"""A git stamp for eval results, so every number traces back to the code that produced it.

Take ``git_stamp`` when a run starts and ``finish_stamp`` when it ends. The fingerprint covers
uncommitted edits and the contents of untracked files, so a prompt edited while a run is going
shows up as ``changed_during_run`` even when the commit stays the same. Ignored files (data/) are
not part of it.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any


def _git(repo: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout


def git_stamp(repo: str | Path) -> dict[str, Any]:
    """The repo's HEAD commit, whether the tree is dirty, which files, and a content fingerprint."""
    repo = Path(repo).expanduser()
    head = _git(repo, "rev-parse", "HEAD")
    if head is None:
        return {"commit": None, "dirty": None, "dirty_files": [], "fingerprint": None}
    status = _git(repo, "status", "--porcelain", "--untracked-files=all") or ""
    files = sorted(line[3:] for line in status.splitlines() if line)
    digest = hashlib.sha256((_git(repo, "diff", "HEAD") or "").encode())
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard") or ""
    for name in sorted(untracked.splitlines()):
        digest.update(name.encode())
        try:
            digest.update((repo / name).read_bytes())
        except OSError:
            pass
    return {
        "commit": head.strip(),
        "dirty": bool(files),
        "dirty_files": files,
        "fingerprint": digest.hexdigest(),
    }


def finish_stamp(start: dict[str, Any], repo: str | Path) -> dict[str, Any]:
    """The start stamp plus whether the commit or any uncommitted file changed during the run."""
    end = git_stamp(repo)
    changed = None
    if start["commit"] is not None and end["commit"] is not None:
        changed = (start["commit"], start["fingerprint"]) != (end["commit"], end["fingerprint"])
    return {**start, "changed_during_run": changed, "end_commit": end["commit"]}


def print_provenance(provenance: dict[str, dict[str, Any]]) -> None:
    """One line per repo, with a warning when its files changed while the run was going."""
    for name, stamp in provenance.items():
        commit = (stamp.get("commit") or "no git")[:10]
        dirty = f", {len(stamp['dirty_files'])} uncommitted files" if stamp.get("dirty") else ""
        print(f"{name}: {commit}{dirty}")
        if stamp.get("changed_during_run"):
            print(f"  WARNING: {name} changed during the run; these results mix two versions")
