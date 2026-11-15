"""The real Build wiring: which tools it needs and what it puts in Deps."""

import pytest

pytest.importorskip("langgraph")

from pydantic import SecretStr  # noqa: E402

from sifu import live  # noqa: E402
from sifu.implementer import Implementer  # noqa: E402
from sifu.preflight import Check  # noqa: E402
from sifu.verify import Verifier  # noqa: E402
from tests.fakes import FakeLLM  # noqa: E402


@pytest.fixture()
def product(tmp_path, cfg):
    """A product repo folder with the files ProductContext.load needs, and a .git folder."""
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "docs" / "glossary.yaml").write_text("terms: []\nout_of_scope: []\n")
    (repo / "docs" / "PRD.md").write_text("PRD")
    cfg.workspace.product_repo_path = str(repo)
    return repo


@pytest.fixture()
def tools(monkeypatch):
    """A token and a GitHub client, as open_live needs them."""
    monkeypatch.setattr(live, "get_github_token", lambda github_cfg: SecretStr("t0ken"))
    monkeypatch.setattr(live.GitHubClient, "from_config", classmethod(lambda cls, c: "github"))


def test_open_live_wires_the_implementer_verifier_and_github(tmp_path, cfg, product, tools):
    deps, project = live.open_live(tmp_path / "d.db", cfg=cfg, llm=FakeLLM())
    assert isinstance(deps.implementer, Implementer) and isinstance(deps.verifier, Verifier)
    assert deps.github == "github" and callable(deps.publisher) and project == 1
    assert deps.implementer.tag == "d"  # not the default database, so branches are tagged
    assert deps.verifier.tag == "d"


def test_open_live_overrides_only_what_it_is_given(tmp_path, cfg, product, tools):
    cfg.router.merge_mode, cfg.planner.enabled = "risk_based", True
    deps, _ = live.open_live(tmp_path / "d.db", cfg=cfg, llm=FakeLLM())
    assert (deps.cfg.router.merge_mode, deps.cfg.planner.enabled) == ("risk_based", True)
    deps, _ = live.open_live(
        tmp_path / "e.db", cfg=cfg, llm=FakeLLM(), merge_mode="manual", planner=False
    )
    assert (deps.cfg.router.merge_mode, deps.cfg.planner.enabled) == ("manual", False)
    assert (cfg.router.merge_mode, cfg.planner.enabled) == ("risk_based", True)  # caller's copy


def test_the_default_database_is_not_tagged():
    assert live.tag_for(live.DEFAULT_DB) is None and live.tag_for("/x/data/design.db") == "design"


def test_missing_tools_is_empty_when_every_build_check_passes(cfg, monkeypatch):
    checks = [
        Check("Ollama", False, "not reachable", "design"),  # a design check never blocks Build
        Check("Claude Code", True, "found", "build"),
    ]
    monkeypatch.setattr(live, "run_checks", lambda cfg: checks)
    assert live.missing_tools(cfg) == []


def test_missing_tools_lists_each_failed_build_check_as_name_and_detail(cfg, monkeypatch):
    checks = [
        Check("Claude Code", False, "no `claude` on PATH", "build"),
        Check("GitHub token", False, "No GitHub token found.", "build"),
        Check("Clean clone", True, "clean", "build"),
    ]
    monkeypatch.setattr(live, "run_checks", lambda cfg: checks)
    assert live.missing_tools(cfg) == [
        "Claude Code: no `claude` on PATH",
        "GitHub token: No GitHub token found.",
    ]
