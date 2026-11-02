import json
import subprocess
from pathlib import Path

import pytest

from sifu.implementer import (
    Implementer,
    build_command,
    build_env,
    changed_files,
    create_worktree,
)


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path):
    path = tmp_path / "product"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "t")
    (path / "README.md").write_text("hi\n")
    (path / "Package.swift").write_text("// manifest\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


def reply(**kw):
    data = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "done",
        "num_turns": 3,
        "total_cost_usd": 0.05,
        "session_id": "s1",
        "permission_denials": [],
    }
    data.update(kw)
    return json.dumps(data)


def make_runner(stdout, edit=None, code=0, raises=None):
    seen = {}

    def runner(cmd, env, cwd, timeout):
        seen.update(cmd=cmd, env=dict(env), cwd=cwd, timeout=timeout)
        if raises:
            raise raises
        if edit:
            for name, text in edit.items():
                (cwd / name).parent.mkdir(parents=True, exist_ok=True)
                (cwd / name).write_text(text)
        return subprocess.CompletedProcess(cmd, code, stdout, "")

    runner.seen = seen
    return runner


def implementer(cfg, repo, tmp_path, runner):
    cfg.workspace.product_repo_path = str(repo)
    cfg.workspace.worktrees_dir = str(tmp_path / "wt")
    return Implementer(cfg, runner)


def test_command_uses_config_limits_and_allow_list(cfg):
    cmd = build_command(cfg, "do it")
    assert cmd[:3] == ["claude", "-p", "do it"]
    assert cmd[cmd.index("--max-turns") + 1] == str(cfg.implementer.max_turns)
    tools = cmd[cmd.index("--allowedTools") + 1].split(",")
    assert "Edit" in tools and "Bash(swift test:*)" in tools and "Bash" not in tools


def test_environment_is_minimal_and_never_carries_the_github_token(cfg):
    base = {
        "PATH": "/bin",
        "HOME": "/h",
        "SIFU_GITHUB_TOKEN": "secret",
        "AWS_SECRET": "x",
        "ANTHROPIC_API_KEY": "k",
    }
    env = build_env(cfg, base)
    assert env == {"PATH": "/bin", "HOME": "/h", "ANTHROPIC_API_KEY": "k"}


def test_ollama_backend_sets_the_local_endpoint_and_model(cfg):
    cfg.implementer.backend = "ollama"
    cfg.implementer.ollama_model = "qwen3-coder:30b"
    env = build_env(cfg, {"PATH": "/bin", "ANTHROPIC_API_KEY": "cloud-key"})
    assert env["ANTHROPIC_BASE_URL"] == cfg.ollama.base_url
    assert (
        env["ANTHROPIC_API_KEY"] == ""
        and env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "qwen3-coder:30b"
    )
    assert build_command(cfg, "p")[-2:] == ["--model", "qwen3-coder:30b"]


def test_successful_run_records_result_and_changed_files(cfg, repo, tmp_path):
    runner = make_runner(reply(), edit={"Sources/Trip.swift": "struct Trip {}\n"})
    out = implementer(cfg, repo, tmp_path, runner)(7, "spec", "conventions")
    assert out.ok and out.result == "done" and out.num_turns == 3
    assert out.changed_files == ["Sources/Trip.swift"] and out.protected_touched == []
    assert out.branch == "sifu/ticket-7" and Path(out.worktree).name == "ticket-7"
    assert runner.seen["cwd"] == Path(out.worktree)
    assert (repo / "Sources").exists() is False  # the product checkout itself is untouched


def test_touching_a_protected_file_fails_the_run(cfg, repo, tmp_path):
    runner = make_runner(reply(), edit={"Package.swift": "// changed\n"})
    out = implementer(cfg, repo, tmp_path, runner)(8, "spec", "c")
    assert not out.ok and out.protected_touched == ["Package.swift"]


def test_timeout_and_missing_binary_do_not_raise(cfg, repo, tmp_path):
    out = implementer(
        cfg, repo, tmp_path, make_runner("", raises=subprocess.TimeoutExpired("c", 1))
    )(1, "s", "c")
    assert not out.ok and "timeout" in out.error
    out = implementer(cfg, repo, tmp_path, make_runner("", raises=FileNotFoundError("claude")))(
        2, "s", "c"
    )
    assert not out.ok and "could not start" in out.error


def test_error_result_and_bad_output_are_failures(cfg, repo, tmp_path):
    bad = implementer(
        cfg, repo, tmp_path, make_runner(reply(is_error=True, subtype="error_max_turns"), code=1)
    )(3, "s", "c")
    assert not bad.ok and "error_max_turns" in bad.error and bad.num_turns == 3
    junk = implementer(cfg, repo, tmp_path, make_runner("not json"))(4, "s", "c")
    assert not junk.ok and "no JSON" in junk.error


def test_denied_tools_are_recorded(cfg, repo, tmp_path):
    denial = [{"tool_name": "Bash", "tool_input": {"command": "curl x"}}]
    out = implementer(cfg, repo, tmp_path, make_runner(reply(permission_denials=denial)))(
        5, "s", "c"
    )
    assert out.ok and out.permission_denials == denial


def test_worktree_is_reused_and_status_lists_untracked_files(repo, tmp_path):
    path, _ = create_worktree(repo, tmp_path / "wt", 9)
    (path / "new.txt").write_text("x")
    assert create_worktree(repo, tmp_path / "wt", 9)[0] == path
    assert changed_files(path) == ["new.txt"]


def test_result_records_the_backend_and_model(cfg, repo, tmp_path):
    cfg.implementer.backend = "ollama"
    out = implementer(cfg, repo, tmp_path, make_runner(reply()))(10, "s", "c")
    assert out.backend == "ollama" and out.model == "qwen3-coder:30b"
    cfg.implementer.backend = "cloud"
    out = implementer(cfg, repo, tmp_path, make_runner(reply()))(11, "s", "c")
    assert out.backend == "cloud" and out.model == "claude-code default"


def test_tagged_runs_get_separate_worktrees_and_branches(cfg, repo, tmp_path):
    first = implementer(cfg, repo, tmp_path, make_runner(reply()))
    first.tag = "cloud"
    second = implementer(cfg, repo, tmp_path, make_runner(reply()))
    second.tag = "ollama"
    a, b = first(12, "s", "c"), second(12, "s", "c")
    assert a.branch == "sifu/ticket-12-cloud" and b.branch == "sifu/ticket-12-ollama"
    assert a.worktree != b.worktree
