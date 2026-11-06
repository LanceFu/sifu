"""The deterministic review stage: parsers, diff checks, step order and the feedback loop."""

import json
import subprocess

import pytest

from sifu.config import VerifyStep
from sifu.verify import (
    Finding,
    VerifyResult,
    parse_compiler,
    parse_swiftlint_json,
    run_verify,
    tail_finding,
)


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture()
def worktree(tmp_path):
    path = tmp_path / "wt"
    (path / "Sources").mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "t")
    (path / "Sources" / "Trip.swift").write_text("struct Trip {}\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


def fake_runner(results, seen=None):
    """results: step command[0] -> (exit code, stdout). Records the commands that ran."""

    def runner(cmd, env, cwd, timeout):
        if seen is not None:
            seen.append(cmd)
        code, out = results.get(cmd[0] + (":" + cmd[1] if cmd[0] == "swift" else ""), (0, ""))
        return subprocess.CompletedProcess(cmd, code, out, "")

    return runner


@pytest.fixture()
def vcfg(cfg):
    cfg.verify.steps = [
        VerifyStep(name="build", command=["swift", "build"], parser="compiler"),
        VerifyStep(name="swiftlint", command=["swiftlint"], parser="swiftlint_json"),
        VerifyStep(name="test", command=["swift", "test"], parser="compiler"),
    ]
    return cfg


def test_compiler_parser_reads_errors_and_warnings(tmp_path):
    out = (
        f"{tmp_path}/Sources/Trip.swift:12:5: error: cannot find 'x' in scope\n"
        "Sources/Trip.swift:3:1: warning: unused variable 'y'\n"
        "note: something else\n"
    )
    found = parse_compiler("build", out, tmp_path)
    assert [(f.file, f.line, f.rule) for f in found] == [
        ("Sources/Trip.swift", 12, "error"),
        ("Sources/Trip.swift", 3, "warning"),
    ]
    assert found[0].message == "cannot find 'x' in scope"


def test_swiftlint_json_parser_and_bad_json(tmp_path):
    data = json.dumps(
        [
            {
                "file": f"{tmp_path}/Sources/A.swift",
                "line": 4,
                "rule_id": "line_length",
                "reason": "Too long",
            }
        ]
    )
    [one] = parse_swiftlint_json("swiftlint", data, tmp_path)
    assert (one.file, one.line, one.rule, one.message) == (
        "Sources/A.swift",
        4,
        "line_length",
        "Too long",
    )
    assert parse_swiftlint_json("swiftlint", "not json", tmp_path) == []


def test_tail_finding_keeps_the_last_lines():
    out = "\n".join(f"line {i}" for i in range(40))
    finding = tail_finding("test", out, 2)
    assert (
        finding.rule == "exit 2"
        and "line 39" in finding.message
        and "line 5" not in finding.message
    )


def test_clean_run_passes_and_runs_every_step_in_order(vcfg, worktree):
    seen = []
    result = run_verify(vcfg, worktree, [], fake_runner({}, seen))
    assert result.ok and result.findings == []
    assert [c[0] for c in seen] == ["swift", "swiftlint", "swift"]
    assert [s["name"] for s in result.steps] == ["diff", "build", "swiftlint", "test"]


def test_first_failing_step_stops_the_run(vcfg, worktree):
    seen = []
    lint = json.dumps([{"file": "Sources/Trip.swift", "line": 1, "rule_id": "r", "reason": "bad"}])
    result = run_verify(vcfg, worktree, [], fake_runner({"swiftlint": (2, lint)}, seen))
    assert not result.ok and [f.source for f in result.findings] == ["swiftlint"]
    assert ["swift", "test"] not in seen  # swift test never ran


def test_unparsed_failure_falls_back_to_the_output_tail(vcfg, worktree):
    result = run_verify(vcfg, worktree, [], fake_runner({"swift:build": (1, "boom")}))
    assert result.findings[0].rule == "exit 1" and "boom" in result.findings[0].message


def test_missing_tool_and_timeout_are_findings_not_crashes(vcfg, worktree):
    def missing(cmd, env, cwd, timeout):
        raise FileNotFoundError(cmd[0])

    result = run_verify(vcfg, worktree, [], missing)
    assert not result.ok and result.findings[0].rule == "missing"

    def slow(cmd, env, cwd, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout)

    assert run_verify(vcfg, worktree, [], slow).findings[0].rule == "timeout"


def test_added_swiftlint_disable_is_flagged_in_tracked_and_new_files(vcfg, worktree):
    (worktree / "Sources" / "Trip.swift").write_text(
        "struct Trip {}\n// swiftlint:disable line_length\n"
    )
    (worktree / "Sources" / "New.swift").write_text("// swiftlint:disable:next x\nlet a = 1\n")
    result = run_verify(vcfg, worktree, ["Sources/"], fake_runner({}))
    flagged = {(f.file, f.line) for f in result.findings if f.rule == "swiftlint_disable"}
    assert flagged == {("Sources/Trip.swift", 2), ("Sources/New.swift", 1)}
    assert [s["name"] for s in result.steps] == ["diff"]  # no tool ran


def test_a_disable_that_was_already_there_is_not_blamed_on_the_implementer(vcfg, worktree):
    (worktree / "Sources" / "Old.swift").write_text("// swiftlint:disable all\n")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-q", "-m", "old")
    (worktree / "Sources" / "Old.swift").write_text("// swiftlint:disable all\nlet b = 2\n")
    assert run_verify(vcfg, worktree, ["Sources/"], fake_runner({})).ok


def test_files_outside_the_spec_are_flagged_except_allowed_extras(vcfg, worktree):
    (worktree / "Sources" / "Trip.swift").write_text("struct Trip { var a = 1 }\n")
    (worktree / "Tests").mkdir()
    (worktree / "Tests" / "TripTests.swift").write_text("// test\n")
    (worktree / "MEMORY.md").write_text("notes\n")
    result = run_verify(vcfg, worktree, ["Sources/Trip.swift"], fake_runner({}))
    assert [(f.file, f.rule) for f in result.findings] == [("MEMORY.md", "outside_spec")]
    # With no affected paths in the spec there is nothing to compare against.
    assert run_verify(vcfg, worktree, [], fake_runner({})).ok


def test_fingerprint_ignores_line_numbers_but_not_content():
    a = VerifyResult(False, [Finding("t", "msg", "A.swift", 3, "r")])
    b = VerifyResult(False, [Finding("t", "msg", "A.swift", 9, "r")])
    c = VerifyResult(False, [Finding("t", "other", "A.swift", 3, "r")])
    assert a.fingerprint == b.fingerprint != c.fingerprint
    assert a.to_payload(limit=0)["findings"] == [] and a.to_payload()["finding_count"] == 1


# Real output from `swift build` on a Mac (trimmed): colour codes, a code frame that repeats the
# error, and a very long failed-command line.
REAL_BUILD_FAILURE = (
    "Building for debugging...\n[2\u2009/\u20097] TripPlannerCore\n\n"
    "error: SwiftCompile normal arm64 failed with a nonzero exit code.\n"
    "{wt}/Sources/TripPlannerCore/Trip.swift:16:23: \x1b[1;31merror: \x1b[1;39m"
    "cannot convert value of type 'String' to specified type 'Int'\x1b[0;0m\n"
    '\x1b[0;36m16 |\x1b[0;0m     let broken: Int = \x1b[4;39m"x"\x1b[0;0m\n'
    "   \x1b[0;36m|\x1b[0;0m                       `- \x1b[1;31merror: \x1b[1;39m"
    "cannot convert value of type 'String' to specified type 'Int'\x1b[0;0m\n"
    "Failed frontend command:\n/Applications/Xcode.app/usr/bin/swift-frontend -frontend "
    + "-flag " * 200
    + "\nerror: Build failed"
)


def test_real_swift_build_output_gives_one_clean_finding(tmp_path):
    out = REAL_BUILD_FAILURE.format(wt=tmp_path)
    [one] = parse_compiler("build", out, tmp_path)
    assert (one.file, one.line, one.rule) == ("Sources/TripPlannerCore/Trip.swift", 16, "error")
    assert one.message == "cannot convert value of type 'String' to specified type 'Int'"
    assert "\x1b" not in one.message


def test_fallback_keeps_error_lines_and_drops_the_long_command_line():
    finding = tail_finding("build", REAL_BUILD_FAILURE.format(wt="/x"), 1)
    assert all(len(line) < 300 for line in finding.message.splitlines())
    assert "swift-frontend" not in finding.message and "Build failed" in finding.message
