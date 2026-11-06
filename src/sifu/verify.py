"""Deterministic review stage: Sifu runs the build, linter and tests itself and turns their output
into structured findings. Code decides pass or fail; no model is involved.

Cheap checks come first. The diff checks (no ``swiftlint:disable``, no files outside the spec) cost
nothing, then each configured step runs in order and the first failing step ends the run, so the
implementer gets one focused set of findings per round.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sifu.config import Config, VerifyStep
from sifu.implementer import _PASS_THROUGH, Runner, changed_files, default_runner, git_output

_COMPILER_LINE = re.compile(
    r"^(?P<file>[^:\s][^:]*):(?P<line>\d+):(?:\d+:)?\s*(?P<sev>error|warning):\s*(?P<msg>.*)$"
)
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")
_LINT_DISABLE = re.compile(r"swiftlint:(disable|disable:next|disable:this|disable:previous)")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_TAIL_LINES = 15
_MAX_LINE = 240  # swift prints whole compiler command lines on failure; keep findings readable


def strip_ansi(text: str) -> str:
    """Tools colour their output even when piped; the colour codes would hide the file:line."""
    return _ANSI.sub("", text)


def _short(line: str) -> str:
    return line if len(line) <= _MAX_LINE else line[:_MAX_LINE] + " ..."


@dataclass(frozen=True)
class Finding:
    """One problem found by a tool or a diff check."""

    source: str
    message: str
    file: str = ""
    line: int | None = None
    rule: str = ""

    def key(self) -> tuple[str, str, str, str]:
        """Identity for the no-progress check. The line is left out because fixes shift lines."""
        return (self.source, self.file, self.rule, self.message)


@dataclass
class VerifyResult:
    ok: bool
    findings: list[Finding] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: int = 0

    @property
    def fingerprint(self) -> str:
        keys = sorted(f.key() for f in self.findings)
        return hashlib.sha256(json.dumps(keys).encode()).hexdigest()[:16]

    def to_payload(self, limit: int | None = None) -> dict[str, Any]:
        shown = self.findings if limit is None else self.findings[:limit]
        return {
            "ok": self.ok,
            "steps": self.steps,
            "finding_count": len(self.findings),
            "findings": [asdict(f) for f in shown],
            "fingerprint": self.fingerprint,
            "duration_ms": self.duration_ms,
        }


# ---- parsers ----------------------------------------------------------------------------------


def _relative(path: str, worktree: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(worktree.resolve()))
    except (ValueError, OSError):
        return path


def parse_compiler(step: str, output: str, worktree: Path) -> list[Finding]:
    """``file:line:col: error|warning: message`` lines, as printed by swiftc and XCTest."""
    found: list[Finding] = []
    for raw in strip_ansi(output).splitlines():
        match = _COMPILER_LINE.match(raw.strip())
        if match:
            finding = Finding(
                source=step,
                file=_relative(match["file"], worktree),
                line=int(match["line"]),
                rule=match["sev"],
                message=match["msg"].strip(),
            )
            if finding not in found:  # the same diagnostic is often printed twice
                found.append(finding)
    return found


def parse_swiftlint_json(step: str, output: str, worktree: Path) -> list[Finding]:
    """SwiftLint's ``--reporter json`` output: a list of violations."""
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return []
    found = []
    for item in data if isinstance(data, list) else []:
        found.append(
            Finding(
                source=step,
                file=_relative(str(item.get("file", "")), worktree),
                line=item.get("line"),
                rule=str(item.get("rule_id", "")),
                message=str(item.get("reason", "")),
            )
        )
    return found


def tail_finding(step: str, output: str, code: int) -> Finding:
    """Fallback when a step failed but its output had nothing the parser understood."""
    lines = [_short(line) for line in strip_ansi(output).strip().splitlines()]
    errors = [line for line in lines if "error:" in line]
    shown = errors[:_TAIL_LINES] if errors else lines[-_TAIL_LINES:]
    return Finding(source=step, rule=f"exit {code}", message="\n".join(shown) or "no output")


PARSERS: dict[str, Callable[[str, str, Path], list[Finding]]] = {
    "compiler": parse_compiler,
    "swiftlint_json": parse_swiftlint_json,
}


# ---- diff checks ------------------------------------------------------------------------------


def added_lines(worktree: Path) -> list[tuple[str, int, str]]:
    """Lines the implementer added: the ``+`` lines of tracked diffs and all lines of new files."""
    rows: list[tuple[str, int, str]] = []
    current, number = "", 0
    for line in git_output(worktree, "diff", "HEAD", "-U0", "--no-color").splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif (hunk := _HUNK.match(line)) and current:
            number = int(hunk["start"])
        elif line.startswith("+") and not line.startswith("+++") and current:
            rows.append((current, number, line[1:]))
            number += 1
    untracked = git_output(worktree, "ls-files", "--others", "--exclude-standard").splitlines()
    for name in untracked:
        path = worktree / name
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            rows.extend((name, i + 1, row) for i, row in enumerate(text.splitlines()))
    return rows


def _in_scope(name: str, allowed: list[str]) -> bool:
    for entry in allowed:
        base = entry.rstrip("/")
        if name == base or name.startswith(base + "/"):
            return True
    return False


def diff_findings(
    worktree: Path, affected_paths: list[str], extra_allowed: list[str]
) -> list[Finding]:
    """Free checks on the diff itself."""
    found = []
    for name, number, text in added_lines(worktree):
        if _LINT_DISABLE.search(text):
            found.append(
                Finding(
                    "diff",
                    "Lint rules must be fixed, not disabled.",
                    file=name,
                    line=number,
                    rule="swiftlint_disable",
                )
            )
    if affected_paths:
        allowed = [*affected_paths, *extra_allowed]
        for name in changed_files(worktree):
            if not _in_scope(name, allowed):
                found.append(
                    Finding(
                        "diff",
                        "This file is not in the spec's affected paths. Remove the change, or "
                        "keep it only if the acceptance criteria need it.",
                        file=name,
                        rule="outside_spec",
                    )
                )
    return found


# ---- runner -----------------------------------------------------------------------------------


def _env(base: Mapping[str, str] | None) -> dict[str, str]:
    import os

    source = os.environ if base is None else base
    return {k: source[k] for k in _PASS_THROUGH if k in source}


def run_step(
    step: VerifyStep, worktree: Path, timeout: int, runner: Runner, env: Mapping[str, str]
) -> tuple[list[Finding], dict[str, Any]]:
    started = time.monotonic()
    info: dict[str, Any] = {"name": step.name, "command": " ".join(step.command)}
    try:
        done = runner(step.command, env, worktree, timeout)
    except subprocess.TimeoutExpired:
        info.update(ok=False, exit=None, error=f"timeout after {timeout}s")
        findings = [Finding(step.name, f"timeout after {timeout}s", rule="timeout")]
    except OSError as exc:  # the tool is not installed
        info.update(ok=False, exit=None, error=str(exc))
        findings = [Finding(step.name, f"could not run {step.command[0]}: {exc}", rule="missing")]
    else:
        output = (done.stdout or "") + "\n" + (done.stderr or "")
        ok = done.returncode == 0
        info.update(ok=ok, exit=done.returncode)
        findings = []
        if not ok:
            parser = PARSERS.get(step.parser)
            # SwiftLint's JSON is on stdout alone; compilers print diagnostics to either stream.
            text = (done.stdout or "") if step.parser == "swiftlint_json" else output
            findings = parser(step.name, text, worktree) if parser else []
            if not findings:
                findings = [tail_finding(step.name, output, done.returncode)]
    info["seconds"] = round(time.monotonic() - started, 1)
    return findings, info


def run_verify(
    cfg: Config,
    worktree: Path,
    affected_paths: list[str],
    runner: Runner = default_runner,
    base_env: Mapping[str, str] | None = None,
) -> VerifyResult:
    """Run the diff checks, then the configured steps until one fails."""
    started = time.monotonic()
    result = VerifyResult(ok=True)
    result.findings = diff_findings(worktree, affected_paths, cfg.verify.extra_allowed_paths)
    if result.findings:
        result.ok = False
        result.steps.append({"name": "diff", "ok": False})
    else:
        result.steps.append({"name": "diff", "ok": True})
        env = _env(base_env)
        for step in cfg.verify.steps:
            findings, info = run_step(step, worktree, cfg.verify.timeout_seconds, runner, env)
            result.steps.append(info)
            if findings:
                result.findings, result.ok = findings, False
                break
    result.duration_ms = int((time.monotonic() - started) * 1000)
    return result


class Verifier:
    """The callable stored in ``Deps.verifier``: finds the ticket's worktree and runs the checks."""

    def __init__(
        self, cfg: Config, runner: Runner = default_runner, tag: str | None = None
    ) -> None:
        self.cfg = cfg
        self.runner = runner
        self.tag = tag

    def __call__(self, ticket_id: int, spec: Mapping[str, Any]) -> VerifyResult:
        name = f"ticket-{ticket_id}" + (f"-{self.tag}" if self.tag else "")
        worktree = Path(self.cfg.workspace.worktrees_dir).expanduser() / name
        return run_verify(self.cfg, worktree, list(spec.get("affected_paths", [])), self.runner)
