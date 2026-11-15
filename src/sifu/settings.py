"""Run options a person can override per database: four values, and nothing else.

Models, caps and protected paths stay in ``config.yaml``. An override only changes a value that
``router.decide`` already honours in every mode, so no floor rule is bypassed.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sifu import db
from sifu.config import Config
from sifu.evidence import open_recorded

OPTIONS: dict[str, tuple[Any, ...]] = {
    "router.merge_mode": ("manual", "risk_based", "auto"),
    "planner.enabled": (True, False),
    "critic.enabled": (True, False),
    "implementer.backend": ("cloud", "ollama"),
}


def load(db_path: str | Path) -> dict[str, Any]:
    """The database's saved overrides; read-only, and empty when the file or table is missing."""
    try:
        conn = open_recorded(db_path)
    except FileNotFoundError:
        return {}
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    except sqlite3.OperationalError:  # a database recorded before settings existed
        return {}
    finally:
        conn.close()
    return {r["key"]: json.loads(r["value"]) for r in rows if r["key"] in OPTIONS}


def save(db_path: str | Path, key: str, value: Any) -> None:
    """Save one override; ``ValueError`` for an unknown option or a value it does not allow."""
    if key not in OPTIONS:
        raise ValueError(f"{key!r} cannot be changed from the app")
    allowed = OPTIONS[key]
    if type(value) is not type(allowed[0]) or value not in allowed:
        raise ValueError(f"{value!r} is not a valid value for {key}")
    conn = db.connect(db_path)
    try:
        db.init_db(conn)
        with conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                " updated_at = datetime('now')",
                (key, json.dumps(value)),
            )
    finally:
        conn.close()


def clear(db_path: str | Path, key: str) -> None:
    """Remove one override, so the option goes back to the value in ``config.yaml``."""
    conn = db.connect(db_path)
    try:
        db.init_db(conn)
        with conn:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    finally:
        conn.close()


def apply(cfg: Config, overrides: Mapping[str, Any]) -> Config:
    """A copy of the config with the known overrides set; unknown keys are ignored."""
    copy = cfg.model_copy(deep=True)
    for key, value in overrides.items():
        if key in OPTIONS:
            section, field = key.split(".")
            setattr(getattr(copy, section), field, value)
    return copy
