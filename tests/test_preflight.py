"""Setup checks: what is missing for the Design stage and for Build."""

import json

from pydantic import SecretStr

from sifu import preflight
from sifu.credentials import SecretNotFound


def tags(*names):
    return lambda url: json.dumps({"models": [{"name": n} for n in names]}).encode()


def all_models(cfg):
    return {cfg.models.author, cfg.models.spec_reviewer, cfg.models.code_reviewer,
            *cfg.models.risk_assessors}  # fmt: skip


def good_repo(tmp_path, cfg):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "docs" / "glossary.yaml").write_text("terms: []\n")
    (repo / "docs" / "PRD.md").write_text("PRD")
    cfg.workspace.product_repo_path = str(repo)
    cfg.workspace.worktrees_dir = str(tmp_path / "wt")


def run(cfg, **overrides):
    kw = {
        "fetch": tags(*all_models(cfg)),
        "which": lambda name: f"/bin/{name}",
        "token": lambda github_cfg: SecretStr("t0ken-value"),
        "git": lambda repo, *args: "",
    }
    kw.update(overrides)
    return {c.name: c for c in preflight.run_checks(cfg, **kw)}


def test_everything_present_passes_in_the_documented_order(tmp_path, cfg):
    good_repo(tmp_path, cfg)
    checks = preflight.run_checks(
        cfg,
        fetch=tags(*all_models(cfg)),
        which=lambda name: f"/bin/{name}",
        token=lambda github_cfg: SecretStr("t0ken-value"),
        git=lambda repo, *args: "",
    )
    assert [c.name for c in checks] == [
        "Ollama", "Models", "Product repo", "Claude Code", "GitHub token", "Clean clone",
        "Verify tools", "Worktrees folder",
    ]  # fmt: skip
    assert all(c.ok for c in checks)
    assert [c.needed_for for c in checks] == ["design"] * 3 + ["build"] * 5
    assert not any("t0ken-value" in c.detail for c in checks)


def test_ollama_down_fails_both_model_checks_without_raising(tmp_path, cfg):
    good_repo(tmp_path, cfg)

    def down(url):
        raise OSError("connection refused")

    checks = run(cfg, fetch=down)
    assert not checks["Ollama"].ok and cfg.ollama.base_url in checks["Ollama"].detail
    assert not checks["Models"].ok and checks["Models"].detail == "not checked"


def test_a_missing_model_tag_is_named(tmp_path, cfg):
    good_repo(tmp_path, cfg)
    present = all_models(cfg) - {cfg.models.code_reviewer}
    checks = run(cfg, fetch=tags(*present))
    assert not checks["Models"].ok and cfg.models.code_reviewer in checks["Models"].detail


def test_the_local_implementer_model_is_checked_only_for_the_ollama_backend(tmp_path, cfg):
    good_repo(tmp_path, cfg)
    cfg.implementer.backend = "cloud"
    assert run(cfg)["Models"].ok
    cfg.implementer.backend = "ollama"
    assert cfg.implementer.ollama_model in run(cfg)["Models"].detail


def test_each_missing_build_tool_fails_its_own_check(tmp_path, cfg):
    good_repo(tmp_path, cfg)

    def no_token(github_cfg):
        raise SecretNotFound("No GitHub token found. Set $SIFU_GITHUB_TOKEN.")

    checks = run(
        cfg,
        which=lambda name: None,
        token=no_token,
        git=lambda repo, *args: " M Sources/Trip.swift\n",
    )
    assert not checks["Claude Code"].ok
    assert checks["GitHub token"].detail == "No GitHub token found. Set $SIFU_GITHUB_TOKEN."
    assert checks["Clean clone"].detail == "uncommitted changes in the product repo"
    assert "swift" in checks["Verify tools"].detail
    assert checks["Ollama"].ok and checks["Product repo"].ok  # design checks are unaffected


def test_a_missing_product_repo_skips_the_clean_clone_check(tmp_path, cfg):
    cfg.workspace.product_repo_path = str(tmp_path / "nowhere")
    checks = run(cfg)
    assert not checks["Product repo"].ok
    assert (checks["Clean clone"].ok, checks["Clean clone"].detail) == (False, "not checked")


def test_a_check_that_raises_becomes_a_failed_check(tmp_path, cfg):
    good_repo(tmp_path, cfg)

    def boom(repo, *args):
        raise RuntimeError("git exploded")

    checks = run(cfg, git=boom)
    assert not checks["Clean clone"].ok and "git exploded" in checks["Clean clone"].detail
