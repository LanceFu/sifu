"""Typed configuration loaded from config.yaml."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

BACKEND_ENV = "SIFU_IMPLEMENTER_BACKEND"


class OllamaConfig(BaseModel):
    base_url: str = "http://localhost:11434"
    num_ctx: int = 65536


class ModelsConfig(BaseModel):
    author: str
    spec_reviewer: str
    code_reviewer: str
    risk_assessors: list[str] = Field(min_length=1)


class ImplementerConfig(BaseModel):
    """Which Claude Code backend runs the implementer.

    ``backend`` picks the one in use; each backend keeps its own model so switching is a one-word
    change (or the ``SIFU_IMPLEMENTER_BACKEND`` environment variable for a single run).
    """

    backend: Literal["cloud", "ollama"] = "cloud"
    cloud_model: str | None = None  # None: the Claude Code default
    ollama_model: str = "qwen3-coder:30b"
    max_turns: int = 30
    timeout_seconds: int = 1800
    allowed_tools: list[str] = Field(default_factory=list)

    @property
    def model(self) -> str | None:
        """The model for the active backend."""
        return self.ollama_model if self.backend == "ollama" else self.cloud_model

    @model_validator(mode="after")
    def _ollama_needs_model(self) -> ImplementerConfig:
        if self.backend == "ollama" and not self.ollama_model:
            raise ValueError("implementer.ollama_model is required when backend is 'ollama'")
        return self

    def using(self, backend: Literal["cloud", "ollama"]) -> ImplementerConfig:
        """A copy that uses another backend."""
        return self.model_copy(update={"backend": backend})


class VerifyStep(BaseModel):
    """One command the orchestrator runs itself after the implementer finishes."""

    name: str
    command: list[str]
    parser: Literal["compiler", "swiftlint_json", "generic"] = "generic"


class CriticConfig(BaseModel):
    """The LLM review pass that runs after the deterministic checks pass."""

    enabled: bool = False
    max_diff_chars: int = 24_000
    min_scenario_words: int = 6
    confirm_blockers: bool = True  # a second model re-reads each blocker against nearby lines
    confirm_model: str | None = None  # None: models.author, a different family from the critics
    context_lines: int = 6
    names: list[str] = ["edge_cases", "contract", "security"]


class VerifyConfig(BaseModel):
    """The deterministic review stage: tools run in order, cheapest first, stopping at the first
    failing step. An empty ``steps`` list turns the stage into a stub."""

    timeout_seconds: int = 900
    max_findings: int = 20
    extra_allowed_paths: list[str] = Field(default_factory=lambda: ["Tests/"])
    steps: list[VerifyStep] = Field(default_factory=list)


class PlannerConfig(BaseModel):
    """The Plan step between an accepted spec and Build. Off by default."""

    enabled: bool = False
    listing_cap: int = 150  # file names shown to the planner
    excerpt_chars: int = 3000  # per file
    total_chars: int = 15000  # all excerpts together


class RouterConfig(BaseModel):
    merge_mode: Literal["manual", "risk_based", "auto"] = "risk_based"
    max_build_iterations: int = 3
    max_design_rounds: int = 3
    max_tokens_per_ticket: int = 400_000
    protected_paths: list[str] = Field(default_factory=list)


class GithubConfig(BaseModel):
    repo: str
    token_env: str = "SIFU_GITHUB_TOKEN"
    keychain_service: str = "sifu-github-token"
    base_branch: str = "main"
    required_check: str = "test"  # the check run Sifu waits for before the merge decision
    ci_timeout_seconds: int = 600
    ci_poll_seconds: int = 15
    merge_method: Literal["squash", "merge", "rebase"] = "squash"
    commit_name: str = "Sifu"
    commit_email: str = "sifu@users.noreply.github.com"


class WorkspaceConfig(BaseModel):
    product_repo_path: str
    worktrees_dir: str
    artifacts_dir: str = "data/artifacts"


class DatabaseConfig(BaseModel):
    path: str = "data/sifu.db"


class Config(BaseModel):
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    models: ModelsConfig
    implementer: ImplementerConfig = Field(default_factory=ImplementerConfig)
    critic: CriticConfig = CriticConfig()
    verify: VerifyConfig = Field(default_factory=VerifyConfig)
    planner: PlannerConfig = Field(default_factory=PlannerConfig)
    router: RouterConfig = Field(default_factory=RouterConfig)
    github: GithubConfig
    workspace: WorkspaceConfig
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)


def load_config(path: str | Path = "config.yaml") -> Config:
    """Read and validate the YAML configuration file."""
    with open(path, encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    cfg = Config.model_validate(raw)
    override = os.environ.get(BACKEND_ENV)
    if override:
        if override not in ("cloud", "ollama"):
            raise ValueError(f"{BACKEND_ENV} must be 'cloud' or 'ollama', got {override!r}")
        cfg = cfg.model_copy(update={"implementer": cfg.implementer.using(override)})
    return cfg
