"""Typed configuration loaded from config.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class OllamaConfig(BaseModel):
    base_url: str = "http://localhost:11434"
    num_ctx: int = 65536


class ModelsConfig(BaseModel):
    author: str
    spec_reviewer: str
    code_reviewer: str
    risk_assessors: list[str] = Field(min_length=1)


class ImplementerConfig(BaseModel):
    backend: Literal["cloud", "ollama"] = "cloud"
    model: str | None = None
    max_turns: int = 30
    timeout_seconds: int = 1800
    allowed_tools: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ollama_needs_model(self) -> ImplementerConfig:
        if self.backend == "ollama" and not self.model:
            raise ValueError("implementer.model is required when backend is 'ollama'")
        return self


class RouterConfig(BaseModel):
    merge_mode: Literal["manual", "risk_based", "auto"] = "risk_based"
    max_build_iterations: int = 3
    protected_paths: list[str] = Field(default_factory=list)


class GithubConfig(BaseModel):
    repo: str
    token_env: str = "SIFU_GITHUB_TOKEN"
    keychain_service: str = "sifu-github-token"


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
    router: RouterConfig = Field(default_factory=RouterConfig)
    github: GithubConfig
    workspace: WorkspaceConfig
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)


def load_config(path: str | Path = "config.yaml") -> Config:
    """Read and validate the YAML configuration file."""
    with open(path, encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    return Config.model_validate(raw)
