"""The default store: SQLite on local disk. Zero configuration, one file.

WHY THIS IS THE DEFAULT AND NOT THE FALLBACK

A standalone runner on somebody's work machine should need nothing but this
repo. Requiring a database to run a Slack command would defeat the point of
shipping it at all, and the owner's own daily use is this backend — which is
what keeps the file-store path exercised rather than theoretical.

WHAT SQLITE GIVES AND WHAT IT COSTS

  * ATOMIC CLAIM. `BEGIN IMMEDIATE` takes the write lock up front, so the read
    of the queue and the UPDATE that takes a row cannot interleave with another
    claimer. Without it two claims both read the same `queued` row and SQLite
    answers the loser at COMMIT, not at SELECT — a race that only appears under
    the concurrency the three-runner harness exists to create.
  * LEASES and DURABILITY, for the same hundred lines.
  * THE COST: one writer at a time. A burst of commands produces
    `database is locked`, which is a load signal and not a fault. It is bounded
    by `busy_timeout` and surfaced as StoreBusy so a caller can say "the runner
    is busy" — the alternative, a raw OperationalError reaching a handler, is a
    500 that reads to a user as the bot being down.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from .base import (
    ABANDONED, ACTIVE_STATES, BUSY_STATES, CLAIMED, DONE, FAILED, MAX_SUMMARY,
    NO_CAPS, QUEUED, RUNNING, Caps, EnqueueResult, Job, JobStore, StoreBusy,
    now_or, runner_view, validate_job, validate_runner,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runners (
    runner_id   TEXT PRIMARY KEY,
    public_key  TEXT NOT NULL,
    labels      TEXT NOT NULL DEFAULT '',
    enrolled_at REAL NOT NULL,
    last_seen   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    product       TEXT NOT NULL,
    server        TEXT NOT NULL,
    select_expr   TEXT,
    marker        TEXT,
    slack_channel TEXT NOT NULL,
    slack_user    TEXT NOT NULL,
    created_at    REAL NOT NULL,
    state         TEXT NOT NULL,
    dispatch_mode TEXT NOT NULL DEFAULT '',
    runner_id     TEXT,
    lease_expires REAL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    started_at    REAL,
    finished_at   REAL,
    exit_code     INTEGER,
    passed        INTEGER,
    failed        INTEGER,
    skipped       INTEGER,
    duration      REAL,
    summary       TEXT
);

CREATE INDEX IF NOT EXISTS jobs_state_created ON jobs (state, created_at);
-- The two indexes the caps are counted over. Without them every enqueue scans
-- the whole history, and the history is the one table that only grows.
CREATE INDEX IF NOT EXISTS jobs_state_product_server ON jobs (state, product, server);
CREATE INDEX IF NOT EXISTS jobs_state_channel ON jobs (state, slack_channel);
"""

#: The migration for a database written before `dispatch_mode` existed. Adding
#: a column with a default is the one schema change SQLite does cheaply, and
#: doing it here means an operator upgrading the edge does not have to know.
_MIGRATIONS = (
    ("jobs", "dispatch_mode", "ALTER TABLE jobs ADD COLUMN dispatch_mode TEXT NOT NULL DEFAULT ''"),
)

_JOB_COLUMNS = (
    "id, product, server, select_expr, marker, slack_channel, slack_user, "
    "created_at, state, dispatch_mode"
)


def _placeholders(values: Iterable[str]) -> str:
    return ",".join("?" for _ in values)


class SqliteStore(JobStore):
    backend = "sqlite"

    def __init__(self, path: str | Path, *, busy_timeout: float = 5.0) -> None:
        self.path = Path(path)
        self.busy_timeout = busy_timeout
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            self._enable_wal(conn)
            # `executescript` issues its own COMMIT, so it cannot run inside an
            # explicit transaction. That is fine: every statement in SCHEMA is
            # `IF NOT EXISTS`, and SQLite takes the write lock per statement, so
            # a second process starting at the same moment sees the table and
            # does nothing.
            conn.executescript(SCHEMA)
        # THE MIGRATION IS THE PART THAT NEEDS A LOCK. It reads `PRAGMA
        # table_info` and then decides whether to ALTER, and two processes
        # starting together both read "no dispatch_mode" and both alter — the
        # loser dying with `duplicate column name` at boot. `BEGIN IMMEDIATE`
        # makes the read and the write one step. Two workers against one file is
        # not a corner case here: it is the deployment whose per-worker state
        # this whole store exists to fix.
        with self._txn() as conn:
            self._migrate(conn)

    def _enable_wal(self, conn: sqlite3.Connection) -> None:
        """WAL, tolerating another process setting it at the same moment.

        WAL is what stops a long-polling reader blocking the writer that is
        trying to enqueue a job from the Slack handler. Getting there is the
        awkward part: **`PRAGMA journal_mode` can return SQLITE_BUSY without
        ever invoking the busy handler**, so neither `timeout=` nor
        `PRAGMA busy_timeout` covers it. Six processes opening one database
        together will sometimes see one of them fail — measured, as a flaky
        test rather than reasoned about.

        The right response is not to raise. Journal mode is a property of the
        FILE, not of this connection: if a concurrent opener has already set
        WAL, there is nothing left to do and failing would refuse to start a
        process whose database is in exactly the state it wanted.
        """
        deadline = time.monotonic() + self.busy_timeout
        while True:
            try:
                row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
                if row is not None and str(row[0]).lower() == "wal":
                    return
            except sqlite3.OperationalError as exc:
                if not _is_busy(exc):
                    raise
                # Somebody else holds the lock. They are setting it to the same
                # value, so read it back before deciding this failed.
                try:
                    current = conn.execute("PRAGMA journal_mode").fetchone()
                    if current is not None and str(current[0]).lower() == "wal":
                        return
                except sqlite3.OperationalError:
                    pass
                if time.monotonic() >= deadline:
                    raise
            if time.monotonic() >= deadline:
                raise sqlite3.OperationalError(
                    "database is locked: could not enable WAL journal mode"
                )
            time.sleep(0.02)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        for table, column, statement in _MIGRATIONS:
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(statement)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=self.busy_timeout, isolation_level=None)
        conn.row_factory = sqlite3.Row
        # `timeout=` only covers the driver's own retry loop; the pragma is what
        # a statement inside an open transaction obeys. Both, or a contended
        # write raises immediately from inside BEGIN IMMEDIATE.
        conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout * 1000)}")
        return conn

    # ── connections ──────────────────────────────────────────────────────────

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """One connection, closed on the way out, with busy translated.

        The connection is CLOSED explicitly rather than left to the garbage
        collector. `sqlite3.Connection.__exit__` commits or rolls back and does
        not close, so `with self._connect() as conn:` leaks a file handle per
        call — invisible until a long-running edge runs out of them.
        """
        conn = self._connect()
        try:
            yield conn
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise _busy(exc) from exc
            raise
        finally:
            conn.close()

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE, committed on success, with StoreBusy instead of a
        leaked driver error.

        IMMEDIATE takes the write lock at BEGIN rather than at the first write,
        which is what makes a read-then-write claim atomic. Deferred, two
        claimers both read the same `queued` row and SQLite answers the loser at
        COMMIT — far too late for the row it already returned.
        """
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    # ── runners ──────────────────────────────────────────────────────────────

    def enrol(self, runner_id: str, public_key: str, labels: Iterable[str],
              now: float | None = None) -> None:
        validate_runner(runner_id, public_key)
        now = now_or(now)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO runners (runner_id, public_key, labels, enrolled_at, last_seen) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(runner_id) DO UPDATE SET public_key=excluded.public_key, "
                "labels=excluded.labels, last_seen=excluded.last_seen",
                (runner_id, public_key, ",".join(sorted(labels)), now, now),
            )

    def runner(self, runner_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM runners WHERE runner_id=?", (runner_id,)
            ).fetchone()
        return dict(row) if row else None

    def touch(self, runner_id: str, now: float | None = None) -> None:
        now = now_or(now)
        with self._conn() as conn:
            conn.execute("UPDATE runners SET last_seen=? WHERE runner_id=?", (now, runner_id))

    def runners(self, offline_after: float, now: float | None = None) -> list[dict[str, Any]]:
        now = now_or(now)
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM runners ORDER BY runner_id").fetchall()
        return [runner_view(dict(r), offline_after, now) for r in rows]

    # ── jobs ─────────────────────────────────────────────────────────────────

    def enqueue(self, job: Job, *, caps: Caps = NO_CAPS,
                now: float | None = None) -> EnqueueResult:
        return self._insert(job, state=QUEUED, mode="test-server", caps=caps, now=now)

    def record_dispatch(self, job: Job, *, mode: str, caps: Caps = NO_CAPS,
                        now: float | None = None) -> EnqueueResult:
        return self._insert(job, state=RUNNING, mode=mode, caps=caps, now=now)

    def _insert(self, job: Job, *, state: str, mode: str, caps: Caps,
                now: float | None) -> EnqueueResult:
        validate_job(job)
        now = now_or(now)
        with self._txn() as conn:
            if conn.execute("SELECT 1 FROM jobs WHERE id=?", (job.id,)).fetchone():
                return EnqueueResult.DUPLICATE

            # Both counts are read inside the transaction that writes, which is
            # the only arrangement in which a cap is a cap. Read them outside it
            # and two callers see the same number and both write.
            if caps.max_active_per_job > 0:
                active = conn.execute(
                    f"SELECT COUNT(*) n FROM jobs WHERE product=? AND server=? "
                    f"AND state IN ({_placeholders(ACTIVE_STATES)})",
                    (job.product, job.server, *ACTIVE_STATES),
                ).fetchone()["n"]
                if active >= caps.max_active_per_job:
                    return EnqueueResult.JOB_AT_CAPACITY

            # WHICH channel cap applies depends on what is being inserted.
            # A queued row waits, so the backlog cap is the one that matters and
            # the running cap is applied later, when a test server claims. A
            # dispatched row is ALREADY running and will never be claimed, so
            # the running cap has to be applied here or it never is — which is
            # the V1/V2 path, and the one a chat box could otherwise use to
            # start fifty pytest processes at once.
            channel_cap, channel_states, refusal = (
                (caps.max_queued_per_channel, (QUEUED,), EnqueueResult.CHANNEL_QUEUE_FULL)
                if state == QUEUED
                else (caps.max_running_per_channel, BUSY_STATES, EnqueueResult.CHANNEL_BUSY)
            )
            if channel_cap > 0:
                busy = conn.execute(
                    f"SELECT COUNT(*) n FROM jobs WHERE slack_channel=? "
                    f"AND state IN ({_placeholders(channel_states)})",
                    (job.slack_channel, *channel_states),
                ).fetchone()["n"]
                if busy >= channel_cap:
                    return refusal

            started = now if state == RUNNING else None
            # OR IGNORE rather than a bare INSERT, so the PRIMARY KEY is the
            # thing that decides duplication and the `SELECT 1` above is only a
            # nicety. `BEGIN IMMEDIATE` already makes the two atomic here, but
            # writing it the same way as the Postgres backend means the two
            # cannot drift — and it is the Postgres one where the check-then-act
            # was a real defect.
            cur = conn.execute(
                f"INSERT OR IGNORE INTO jobs ({_JOB_COLUMNS}, started_at) "
                f"VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (job.id, job.product, job.server, job.select_expr, job.marker,
                 job.slack_channel, job.slack_user, now, state, mode, started),
            )
            return EnqueueResult.ACCEPTED if cur.rowcount == 1 else EnqueueResult.DUPLICATE

    def claim(self, runner_id: str, labels: Iterable[str], lease_seconds: float,
              max_attempts: int, *, caps: Caps = NO_CAPS,
              now: float | None = None) -> Job | None:
        now = now_or(now)
        wanted = {x for x in labels if x}
        with self._txn() as conn:
            self._reap(conn, now, max_attempts)

            busy_by_channel: dict[str, int] = {}
            if caps.max_running_per_channel > 0:
                busy_by_channel = {
                    r["slack_channel"]: r["n"]
                    for r in conn.execute(
                        f"SELECT slack_channel, COUNT(*) n FROM jobs "
                        f"WHERE state IN ({_placeholders(BUSY_STATES)}) GROUP BY slack_channel",
                        BUSY_STATES,
                    )
                }

            rows = conn.execute(
                "SELECT * FROM jobs WHERE state=? ORDER BY created_at ASC", (QUEUED,)
            ).fetchall()
            for row in rows:
                # A test server with no labels is a general-purpose one and
                # takes anything. A labelled one takes only jobs for an
                # environment it declares — that is the "send it to the right
                # machine" routing, and the default (no labels) is the shared
                # pool the queueing test needs.
                if wanted and row["server"] not in wanted:
                    continue
                # The per-channel running cap is applied HERE, inside the same
                # transaction that takes the row, so the slot and the count
                # cannot disagree. A job over the cap is skipped, not refused:
                # it stays queued and the next claim after something finishes
                # will take it.
                if caps.max_running_per_channel > 0:
                    if busy_by_channel.get(row["slack_channel"], 0) >= caps.max_running_per_channel:
                        continue
                cur = conn.execute(
                    "UPDATE jobs SET state=?, runner_id=?, lease_expires=?, attempts=attempts+1 "
                    "WHERE id=? AND state=?",
                    (CLAIMED, runner_id, now + lease_seconds, row["id"], QUEUED),
                )
                if cur.rowcount == 1:
                    return Job(
                        id=row["id"], product=row["product"], server=row["server"],
                        select_expr=row["select_expr"], marker=row["marker"],
                        slack_channel=row["slack_channel"], slack_user=row["slack_user"],
                    )
        return None

    def mark_running(self, job_id: str, runner_id: str, now: float | None = None) -> bool:
        now = now_or(now)
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE jobs SET state=?, started_at=? WHERE id=? AND runner_id=? AND state=?",
                (RUNNING, now, job_id, runner_id, CLAIMED),
            )
            return cur.rowcount == 1

    def finish(self, job_id: str, runner_id: str, *, exit_code: int, passed: int,
               failed: int, skipped: int, duration: float, summary: str,
               now: float | None = None) -> bool:
        now = now_or(now)
        state = DONE if exit_code == 0 else FAILED
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE jobs SET state=?, finished_at=?, exit_code=?, passed=?, failed=?, "
                "skipped=?, duration=?, summary=?, lease_expires=NULL "
                "WHERE id=? AND runner_id=? AND state IN (?,?)",
                (state, now, exit_code, passed, failed, skipped, duration,
                 summary[:MAX_SUMMARY], job_id, runner_id, CLAIMED, RUNNING),
            )
            return cur.rowcount == 1

    def renew(self, runner_id: str, lease_seconds: float, now: float | None = None) -> int:
        now = now_or(now)
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE jobs SET lease_expires=? WHERE runner_id=? AND state IN (?,?)",
                (now + lease_seconds, runner_id, CLAIMED, RUNNING),
            )
            return cur.rowcount

    def _reap(self, conn: sqlite3.Connection, now: float, max_attempts: int) -> None:
        """Requeue anything whose lease ran out; abandon it if it has had enough goes.

        The attempts cap is what stops a job that crashes its host from being
        handed to each of the three in turn and taking the whole fleet down — a
        failure mode that looks like "the test servers are unstable" and is
        really one poisonous job.

        `lease_expires IS NOT NULL` is what keeps a V1 dispatch out of this: a
        row recorded by `record_dispatch` is `running` with no lease, because
        nothing is going to report back on it, and requeueing it would invent a
        job no test server ever agreed to take.
        """
        conn.execute(
            "UPDATE jobs SET state=?, runner_id=NULL, lease_expires=NULL "
            "WHERE state IN (?,?) AND lease_expires IS NOT NULL AND lease_expires < ? "
            "AND attempts < ?",
            (QUEUED, CLAIMED, RUNNING, now, max_attempts),
        )
        conn.execute(
            "UPDATE jobs SET state=?, lease_expires=NULL, "
            "summary='lease expired and out of attempts' "
            "WHERE state IN (?,?) AND lease_expires IS NOT NULL AND lease_expires < ? "
            "AND attempts >= ?",
            (ABANDONED, CLAIMED, RUNNING, now, max_attempts),
        )

    def reap(self, max_attempts: int, now: float | None = None) -> None:
        now = now_or(now)
        with self._txn() as conn:
            self._reap(conn, now, max_attempts)

    def job(self, job_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def last_for(self, product: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE product=? ORDER BY created_at DESC LIMIT 1", (product,)
            ).fetchone()
        return dict(row) if row else None

    def counts(self) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute("SELECT state, COUNT(*) n FROM jobs GROUP BY state").fetchall()
        return {r["state"]: r["n"] for r in rows}


def _busy(exc: sqlite3.OperationalError) -> StoreBusy:
    return StoreBusy(f"the store is busy: {exc}")


def _is_busy(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


__all__ = ["SqliteStore", "SCHEMA"]
