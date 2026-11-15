"""Background jobs: submitted to one worker, recorded in jobs, recovered after a restart."""

import json

import pytest

pytest.importorskip("langgraph")

from sifu import db, jobs  # noqa: E402


class Inline:
    """Runs a submitted job at once, on the calling thread."""

    def submit(self, fn, *args):
        fn(*args)


class Held:
    """Keeps submitted jobs until the test runs them."""

    def __init__(self):
        self.waiting = []

    def submit(self, fn, *args):
        self.waiting.append((fn, args))

    def run_all(self):
        while self.waiting:
            fn, args = self.waiting.pop(0)
            fn(*args)


@pytest.fixture()
def path(tmp_path):
    file = tmp_path / "j.db"
    conn = db.connect(file)
    db.init_db(conn)
    project = db.create_project(conn, "TripPlanner", "/repo", "o/r")
    db.create_ticket(conn, project, "First")
    db.create_ticket(conn, project, "Second")
    conn.close()
    return str(file)


def rows(path):
    conn = db.connect(path)
    try:
        return [dict(r) for r in db.list_jobs(conn)]
    finally:
        conn.close()


def test_submit_runs_the_handler_with_the_payload_and_records_the_result(path, monkeypatch):
    calls = []
    monkeypatch.setattr(jobs, "_EXECUTOR", Inline())
    monkeypatch.setitem(
        jobs.HANDLERS, "run", lambda db_path, **kw: calls.append((db_path, kw)) or "spec"
    )
    job = jobs.submit(path, "run", {"ticket_id": 1, "live": False}, ticket_id=1)
    assert calls == [(path, {"ticket_id": 1, "live": False})]
    [row] = rows(path)
    assert (row["id"], row["status"], row["result"], row["ticket_id"]) == (job, "done", "spec", 1)


def test_a_handler_that_raises_fails_the_job_and_the_next_job_still_runs(path, monkeypatch):
    monkeypatch.setattr(jobs, "_EXECUTOR", Inline())

    def boom(db_path, **kw):
        raise ConnectionError("ollama is down")

    monkeypatch.setitem(jobs.HANDLERS, "run", boom)
    monkeypatch.setitem(jobs.HANDLERS, "continue", lambda db_path, **kw: "done")
    jobs.submit(path, "run", {"ticket_id": 1, "live": False}, ticket_id=1)
    jobs.submit(path, "continue", {"ticket_id": 2}, ticket_id=2)
    second, first = rows(path)
    assert (first["status"], first["error"]) == ("failed", "ConnectionError: ollama is down")
    assert (second["status"], second["result"]) == ("done", "done")


def test_a_second_job_for_a_busy_ticket_is_refused_and_nothing_extra_runs(path, monkeypatch):
    held = Held()
    calls = []
    monkeypatch.setattr(jobs, "_EXECUTOR", held)
    monkeypatch.setitem(jobs.HANDLERS, "answer", lambda db_path, **kw: calls.append(kw) or "spec")
    payload = {"request_id": 1, "answer": "approve", "live": False}
    jobs.submit(path, "answer", payload, ticket_id=1)
    with pytest.raises(db.JobConflict):
        jobs.submit(path, "answer", payload, ticket_id=1)
    held.run_all()
    assert len(calls) == 1 and len(rows(path)) == 1


def test_a_cancelled_job_never_runs(path, monkeypatch):
    held = Held()
    calls = []
    monkeypatch.setattr(jobs, "_EXECUTOR", held)
    monkeypatch.setitem(jobs.HANDLERS, "run", lambda db_path, **kw: calls.append(kw) or "spec")
    job = jobs.submit(path, "run", {"ticket_id": 1, "live": False}, ticket_id=1)
    assert jobs.cancel(path, job) is True
    held.run_all()
    assert calls == [] and rows(path)[0]["status"] == "cancelled"


def test_an_unknown_kind_is_refused_before_anything_is_stored(path):
    with pytest.raises(ValueError):
        jobs.submit(path, "merge_everything", {})
    assert rows(path) == []


def test_recover_marks_jobs_left_by_a_dead_process_once_per_database(path, monkeypatch):
    monkeypatch.setattr(jobs, "_RECOVERED", set())
    conn = db.connect(path)
    stale = db.create_job(conn, "run", {}, 1)
    db.start_job(conn, stale)
    conn.close()
    assert jobs.recover(path) == 1 and rows(path)[0]["status"] == "interrupted"
    monkeypatch.setattr(jobs, "_EXECUTOR", Held())
    monkeypatch.setitem(jobs.HANDLERS, "run", lambda db_path, **kw: "spec")
    jobs.submit(path, "run", {"ticket_id": 1, "live": False}, ticket_id=1)
    assert jobs.recover(path) == 0  # this process's own queued job is left alone
    assert rows(path)[0]["status"] == "queued"


def test_recover_does_not_touch_a_database_without_a_jobs_table(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "_RECOVERED", set())
    old = tmp_path / "old.db"
    conn = db.connect(old)
    db.init_db(conn)
    conn.execute("DROP INDEX jobs_one_active_per_ticket")
    conn.execute("DROP TABLE jobs")
    conn.commit()
    conn.close()
    before = old.read_bytes()
    assert jobs.recover(old) == 0 and old.read_bytes() == before


def test_the_job_payload_is_stored_as_given(path, monkeypatch):
    monkeypatch.setattr(jobs, "_EXECUTOR", Held())
    jobs.submit(path, "answer", {"request_id": 3, "answer": "Trim it.", "live": True}, ticket_id=1)
    assert json.loads(rows(path)[0]["payload"]) == {
        "request_id": 3,
        "answer": "Trim it.",
        "live": True,
    }
