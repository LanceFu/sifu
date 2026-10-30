import subprocess

import pytest

from sifu import credentials
from sifu.config import GithubConfig
from sifu.credentials import SecretNotFound, get_github_token, keychain_lookup

CFG = GithubConfig(repo="owner/repo", token_env="SIFU_TEST_TOKEN", keychain_service="svc")


def completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


def test_env_var_overrides_the_keychain(monkeypatch):
    monkeypatch.setenv("SIFU_TEST_TOKEN", "from-env")
    monkeypatch.setattr(credentials, "keychain_lookup", lambda service: "from-keychain")
    assert get_github_token(CFG).get_secret_value() == "from-env"


def test_falls_back_to_the_keychain_item_named_in_config(monkeypatch):
    monkeypatch.delenv("SIFU_TEST_TOKEN", raising=False)
    monkeypatch.setattr(
        credentials,
        "keychain_lookup",
        lambda service: "from-keychain" if service == "svc" else None,
    )
    assert get_github_token(CFG).get_secret_value() == "from-keychain"


def test_missing_token_raises_a_message_that_names_both_sources(monkeypatch):
    monkeypatch.delenv("SIFU_TEST_TOKEN", raising=False)
    monkeypatch.setattr(credentials, "keychain_lookup", lambda service: None)
    with pytest.raises(SecretNotFound, match="SIFU_TEST_TOKEN.*svc"):
        get_github_token(CFG)


def test_token_is_hidden_from_repr_and_str(monkeypatch):
    monkeypatch.setenv("SIFU_TEST_TOKEN", "github_pat_supersecret")
    token = get_github_token(CFG)
    assert "supersecret" not in repr(token)
    assert "supersecret" not in str(token)
    assert "supersecret" not in f"{token}"


def test_keychain_lookup_returns_the_trimmed_password(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: completed("tok\n"))
    assert keychain_lookup("svc") == "tok"


@pytest.mark.parametrize("result", [completed("", 44), completed("   \n", 0)])
def test_keychain_lookup_returns_none_for_a_missing_item(monkeypatch, result):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: result)
    assert keychain_lookup("svc") is None


def test_keychain_lookup_returns_none_when_security_is_not_installed(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", missing)
    assert keychain_lookup("svc") is None


def test_keychain_command_contains_only_the_item_name(monkeypatch):
    seen = {}

    def capture(args, **kwargs):
        seen["args"] = args
        return completed("tok")

    monkeypatch.setattr(subprocess, "run", capture)
    keychain_lookup("svc")
    assert seen["args"] == ["security", "find-generic-password", "-s", "svc", "-w"]
