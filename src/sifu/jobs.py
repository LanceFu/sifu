"""Background jobs: one worker thread runs ticket operations and records each in ``jobs``.

The page never waits for a job: it submits one and reads the table. A job left ``queued`` or
``running`` by a process that has since died is marked ``interrupted`` by ``recover``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from sifu import db, ops
from sifu.evidence import open_recorded

_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sifu-job")
_RECOVERED: set[str] = set()

HANDLERS: dict[str, Callable[..., str]] = {
    "run": ops.run,
    "answer": ops.answer,
    "continue": ops.continue_to_build,
    "reopen": ops.reopen,
    "resume": ops.resume,
    "lessons": ops.send_lessons,
}


def _key(db_path: str | Path) -> str:
    return str(Path(db_path).resolve())


def submit(
    db_path: str | Path,
    kind: str,
    payload: Mapping[str, Any],
    ticket_id: int | None = None,
) -> int:
    """Queue a job and hand it to the worker; return its id.

    Raises ``db.JobConflict`` when the ticket already has a job queued or running, and
    ``ValueError`` for an unknown kind (before anything is stored).
    """
    if kind not in HANDLERS:
        raise ValueError(f"unknown job kind {kind!r}")
    recover(db_path)  # first, so this process's own rows are never mistaken for stale ones
    _RECOVERED.add(_key(db_path))
    conn = db.connect(db_path)
    try:
        db.init_db(conn)  # adds the table to a database recorded before jobs existed
        job_id = db.create_job(conn, kind, payload, ticket_id)
    finally:
        conn.close()
    _EXECUTOR.submit(run_job, str(db_path), job_id)
    return job_id


def run_job(db_path: str, job_id: int) -> None:
    """Run one queued job and record how it ended. Never raises."""
    conn = db.connect(db_path)
    try:
        if not db.start_job(conn, job_id):
            return  # cancelled before it started
        row = next(r for r in db.list_jobs(conn) if r["id"] == job_id)
        try:
            result = HANDLERS[row["kind"]](db_path, **json.loads(row["payload"]))
            db.finish_job(conn, job_id, result=str(result))
        except Exception as exc:  # noqa: BLE001 - any failure is recorded, the worker lives on
            db.finish_job(conn, job_id, error=f"{type(exc).__name__}: {exc}"[:500])
    finally:
        conn.close()


def recover(db_path: str | Path) -> int:
    """Mark jobs left by a dead process interrupted, once per database per process.

    Looking never writes: the check is read-only, and a database with no stale job (or no jobs
    table at all) is left untouched. Returns how many jobs were interrupted.
    """
    key = _key(db_path)
    if key in _RECOVERED:
        return 0
    _RECOVERED.add(key)
    try:
        probe = open_recorded(db_path)
    except FileNotFoundError:
        return 0
    try:
        count = probe.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running')"
        ).fetchone()[0]
    except sqlite3.OperationalError:  # no jobs table
        return 0
    finally:
        probe.close()
    if not count:
        return 0
    conn = db.connect(db_path)
    try:
        return db.interrupt_stale_jobs(conn)
    finally:
        conn.close()


def cancel(db_path: str | Path, job_id: int) -> bool:
    """Cancel a job that has not started; False when it already started or ended."""
    conn = db.connect(db_path)
    try:
        return db.cancel_job(conn, job_id)
    finally:
        conn.close()
