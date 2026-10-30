"""Secret lookup. The GitHub token comes from the environment, then the macOS Keychain.

The token is only ever handled as a ``SecretStr`` so it does not appear in reprs, logs or
notebook output. The ``security`` command is given the Keychain item's name, never the secret.
"""

from __future__ import annotations

import os
import subprocess

from pydantic import SecretStr

from sifu.config import GithubConfig


class SecretNotFound(RuntimeError):
    """Raised when a secret is in neither the environment nor the Keychain."""


def keychain_lookup(service: str) -> str | None:
    """Return a generic password from the macOS Keychain, or None if it is unavailable."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def get_github_token(cfg: GithubConfig) -> SecretStr:
    """Return the GitHub token: the env var if set, otherwise the Keychain item."""
    value = os.environ.get(cfg.token_env) or keychain_lookup(cfg.keychain_service)
    if not value:
        raise SecretNotFound(
            f"No GitHub token found. Set ${cfg.token_env} or add the Keychain item "
            f"'{cfg.keychain_service}'."
        )
    return SecretStr(value)
