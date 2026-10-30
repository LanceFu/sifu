from pathlib import Path

import pytest
from pydantic import ValidationError

from sifu.config import ImplementerConfig, load_config

ROOT = Path(__file__).resolve().parents[1]


def test_shipped_config_is_valid():
    cfg = load_config(ROOT / "config.yaml")
    assert cfg.implementer.backend == "cloud"
    assert len(cfg.models.risk_assessors) >= 2
    assert cfg.models.code_reviewer != cfg.models.author


def test_ollama_backend_requires_a_model():
    with pytest.raises(ValidationError):
        ImplementerConfig(backend="ollama")
    assert ImplementerConfig(backend="ollama", model="qwen3-coder:30b").model == "qwen3-coder:30b"
