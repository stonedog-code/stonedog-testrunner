"""What is true of ONE backend, and of the wiring that chooses between them.

Everything a store must do regardless of backend lives in `tests/conformance/`.
This file is the complement: which backend a DSN selects, and what SQLite does
that Postgres does not — its single-writer lock, and the refusal that must come
out of it instead of a 500.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from slack_runtests.store import (
    Job, StoreBusy, StoreUnavailable, backend_for, open_store, sqlite_path,
)
from slack_runtests.store.sqlite_backend import SqliteStore

pytestmark = pytest.mark.unit


def a_job(job_id: str = "job-1") -> Job:
    return Job(id=job_id, product="webapp", server="staging", select_expr=None,
               marker=None, slack_channel="#testing", slack_user="U1")


# ── which backend a DSN selects ──────────────────────────────────────────────

@pytest.mark.parametrize(
    "dsn,expected",
    [
        ("data/edge.db", "sqlite"),
        ("/var/lib/edge/edge.db", "sqlite"),
        ("sqlite:///var/lib/edge/edge.db", "sqlite"),
        ("postgres://u:p@host/db", "postgres"),
        ("postgresql://u:p@host/db", "postgres"),
        ("POSTGRESQL://u:p@host/db", "postgres"),
    ],
)
def test_a_dsn_selects_its_backend(dsn: str, expected: str) -> None:
    assert backend_for(dsn) == expected


def test_a_bare_path_is_sqlite_because_that_is_the_default() -> None:
    """The zero-config claim, asserted.

    A standalone runner is meant to need nothing but this repo and a file. If a
    bare path ever stopped meaning SQLite, every existing `EDGE_DB_PATH` would
    start meaning something else — silently, because a path is a valid-looking
    value for almost anything.
    """
    assert backend_for("") == "sqlite"
    assert backend_for("data/edge.db") == "sqlite"


@pytest.mark.parametrize(
    "dsn,expected",
    [
        ("data/edge.db", "data/edge.db"),
        ("sqlite:///abs/path.db", "/abs/path.db"),
        ("sqlite://./rel.db", "rel.db"),
    ],
)
def test_a_sqlite_dsn_resolves_to_the_file_it_names(dsn: str, expected: str) -> None:
    assert str(sqlite_path(dsn)) == expected


def test_an_empty_dsn_is_refused_with_something_actionable() -> None:
    """Not a stack trace naming a driver. The fix is one environment variable."""
    with pytest.raises(StoreUnavailable) as caught:
        open_store("")
    assert "EDGE_DB_PATH" in str(caught.value)


def test_the_backend_names_itself(tmp_path) -> None:
    """Printed at startup, and the parameter id the conformance suite runs under."""
    store = open_store(str(tmp_path / "edge.db"))
    try:
        assert store.backend == "sqlite"
    finally:
        store.close()


# ── SQLite's one writer, and the honest refusal ──────────────────────────────

def test_a_locked_database_is_a_refusal_and_not_a_driver_error(tmp_path) -> None:
    """`sqlite3.OperationalError: database is locked` must never reach a handler.

    That exception becomes a 500, and a 500 makes Slack show its own generic
    failure — which reads to the user as the bot being down. It is not: it is
    load, it is bounded, and the honest answer is "the runner is busy".

    The lock here is real: a second connection holds an EXCLUSIVE transaction
    for longer than the store's whole busy timeout.
    """
    path = tmp_path / "edge.db"
    store = SqliteStore(path, busy_timeout=0.2)
    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        blocker = sqlite3.connect(path, isolation_level=None)
        blocker.execute("BEGIN EXCLUSIVE")
        holding.set()
        release.wait(timeout=10)
        blocker.execute("ROLLBACK")
        blocker.close()

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert holding.wait(timeout=10)
        with pytest.raises(StoreBusy):
            store.enqueue(a_job())
    finally:
        release.set()
        thread.join(timeout=10)
        store.close()


def test_the_busy_timeout_is_bounded_rather_than_an_indefinite_wait(tmp_path) -> None:
    """Slack's handler has three seconds. A store that waits longer produces a
    retry, and the retry is what the idempotency key exists to absorb."""
    store = SqliteStore(tmp_path / "edge.db", busy_timeout=0.25)
    assert store.busy_timeout == 0.25


def test_an_existing_database_gains_the_new_column_without_an_operator(tmp_path) -> None:
    """An upgrade that hard-crashes on the database somebody already has is not
    an upgrade. `dispatch_mode` arrived after the first release."""
    path = tmp_path / "edge.db"
    legacy = sqlite3.connect(path, isolation_level=None)
    legacy.executescript(
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, product TEXT NOT NULL, "
        "server TEXT NOT NULL, select_expr TEXT, marker TEXT, "
        "slack_channel TEXT NOT NULL, slack_user TEXT NOT NULL, "
        "created_at REAL NOT NULL, state TEXT NOT NULL, runner_id TEXT, "
        "lease_expires REAL, attempts INTEGER NOT NULL DEFAULT 0, "
        "started_at REAL, finished_at REAL, exit_code INTEGER, passed INTEGER, "
        "failed INTEGER, skipped INTEGER, duration REAL, summary TEXT);"
        "INSERT INTO jobs (id, product, server, slack_channel, slack_user, "
        "created_at, state) VALUES ('old','webapp','staging','#testing','U1',1.0,'queued');"
    )
    legacy.close()

    store = SqliteStore(path)
    try:
        assert store.job("old")["dispatch_mode"] == ""
        assert store.claim("runner-1", [], 60, 3) is not None
    finally:
        store.close()
