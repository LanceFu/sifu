from pathlib import Path

import pytest
from pydantic import ValidationError

from sifu.config import ImplementerConfig, load_config

ROOT = Path(__file__).resolve().parents[1]


def test_shipped_config_is_valid(monkeypatch):
    monkeypatch.delenv("SIFU_IMPLEMENTER_BACKEND", raising=False)
    cfg = load_config(ROOT / "config.yaml")
    assert cfg.implementer.backend == "cloud"
    assert len(cfg.models.risk_assessors) >= 2
    assert cfg.models.code_reviewer != cfg.models.author


def test_ollama_backend_requires_a_model():
    with pytest.raises(ValidationError):
        ImplementerConfig(backend="ollama", ollama_model="")


def test_each_backend_keeps_its_own_model():
    cloud = ImplementerConfig(cloud_model="claude-sonnet-4-5")
    assert cloud.model == "claude-sonnet-4-5"
    local = cloud.using("ollama")
    assert local.backend == "ollama" and local.model == "qwen3-coder:30b"
    assert cloud.backend == "cloud"  # using() returns a copy


def test_env_var_switches_the_backend_for_one_run(monkeypatch):
    monkeypatch.setenv("SIFU_IMPLEMENTER_BACKEND", "ollama")
    cfg = load_config(ROOT / "config.yaml")
    assert cfg.implementer.backend == "ollama" and cfg.implementer.model == "qwen3-coder:30b"
    monkeypatch.setenv("SIFU_IMPLEMENTER_BACKEND", "lmstudio")
    with pytest.raises(ValueError):
        load_config(ROOT / "config.yaml")
