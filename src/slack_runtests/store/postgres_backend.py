"""The durable store: Postgres, selected by a DSN and never by default.

WHY THIS EXISTS, AND IT IS NOT "SQLITE BUT BIGGER"

Two reasons, and only one of them is durability.

  1. **Lightsail container services have no persistent volume.** The embedded
     deployment (strategy B: the edge as a sidecar inside an existing app) would
     lose its SQLite file on every redeploy, taking the queue, the in-flight
     leases and the entire run history with it. A queue that forgets what it was
     doing whenever the site ships is not a queue.
  2. **SQLite has exactly one writer.** A burst of slash commands against the
     file-backed store produces `database is locked`, which is bounded and
     surfaced honestly (see the sqlite backend) but is still a refusal. Postgres
     has no such limit, and `FOR UPDATE SKIP LOCKED` gives a queue claim that is
     both atomic and non-blocking — a second claimer steps over the locked row
     rather than waiting behind it.

WHY THE CLAIM LOOKS LIKE THAT

`SELECT ... FOR UPDATE SKIP LOCKED` inside the same statement as the UPDATE is
the idiom this whole file is built around. It is the Postgres equivalent of the
SQLite backend's `BEGIN IMMEDIATE`: the row is read and taken without a window
in which a second claimer could see it unclaimed. Written the natural way — a
SELECT, then an UPDATE — three long-polling test servers all read the same
`queued` row and all three run the suite. That failure is invisible to a suite
that only ever runs one backend, which is why the conformance suite runs both.

WHY A CAP NEEDS A LOCK HERE AND NOT IN SQLITE

SQLite has one writer, so counting rows and inserting one inside `BEGIN
IMMEDIATE` is atomic for free. Postgres runs READ COMMITTED, where a dozen
concurrent transactions can all read "2 queued", all decide they are under a cap
of 3, and all insert. Measured, not reasoned about: the conformance suite's
concurrent-enqueue test passed on SQLite and let **four** rows through here on
the first run.

So a cap is bought with a transaction-scoped advisory lock on the write path
that enforces it — and **only when a cap is actually configured**. With caps off
(the zero default for any of the three), nothing is locked and `FOR UPDATE SKIP
LOCKED` does its work unimpeded. The lock is the price of the cap, paid by the
deployment that asked for one.

DELIBERATELY THE SAME TYPES AS SQLITE

Timestamps are `DOUBLE PRECISION` epoch seconds, not `TIMESTAMPTZ`. Postgres's
timestamp handling is better in every way that matters *except* the one that
matters here: it would make the two backends disagree about what `created_at`
is, and every caller would need to know which store it was talking to. The
whole point of a conformance suite is that they cannot diverge.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterable, Iterator

from .base import (
    ABANDONED, ACTIVE_STATES, BUSY_STATES, CLAIMED, DONE, FAILED, MAX_SUMMARY,
    NO_CAPS, QUEUED, RUNNING, Caps, EnqueueResult, Job, JobStore, StoreBusy,
    StoreUnavailable, now_or, runner_view, validate_job, validate_runner,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runners (
    runner_id   TEXT PRIMARY KEY,
    public_key  TEXT NOT NULL,
    labels      TEXT NOT NULL DEFAULT '',
    enrolled_at DOUBLE PRECISION NOT NULL,
    last_seen   DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    product       TEXT NOT NULL,
    server        TEXT NOT NULL,
    select_expr   TEXT,
    marker        TEXT,
    slack_channel TEXT NOT NULL,
    slack_user    TEXT NOT NULL,
    created_at    DOUBLE PRECISION NOT NULL,
    state         TEXT NOT NULL,
    dispatch_mode TEXT NOT NULL DEFAULT '',
    runner_id     TEXT,
    lease_expires DOUBLE PRECISION,
    attempts      INTEGER NOT NULL DEFAULT 0,
    started_at    DOUBLE PRECISION,
    finished_at   DOUBLE PRECISION,
    exit_code     INTEGER,
    passed        INTEGER,
    failed        INTEGER,
    skipped       INTEGER,
    duration      DOUBLE PRECISION,
    summary       TEXT
);

CREATE INDEX IF NOT EXISTS jobs_state_created ON jobs (state, created_at);
CREATE INDEX IF NOT EXISTS jobs_state_product_server ON jobs (state, product, server);
CREATE INDEX IF NOT EXISTS jobs_state_channel ON jobs (state, slack_channel);
"""

#: Kept in step with the SQLite backend's migration list by the conformance
#: suite, which asserts both backends expose the same columns.
_MIGRATIONS = (
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS dispatch_mode TEXT NOT NULL DEFAULT ''",
)

#: Advisory locks are keyed by two 32-bit integers. The first is a namespace
#: chosen so this package cannot collide with an application sharing the
#: database — which is the embedded deployment's normal case, not a hypothetical.
_LOCK_NAMESPACE = 0x5344_4351  # "SDCQ"
_LOCK_ENQUEUE = 1
_LOCK_CLAIM = 2

_JOB_COLUMNS = (
    "id, product, server, select_expr, marker, slack_channel, slack_user, "
    "created_at, state, dispatch_mode, started_at"
)


def _require_psycopg():
    """Import psycopg, or say plainly what to install.

    A bare ImportError here reads as a bug in this package. It is not — it is a
    deployment that asked for Postgres without installing the driver, and the
    fix is one command.
    """
    try:
        import psycopg  # noqa: PLC0415
        from psycopg.rows import dict_row  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised by the DSN tests
        raise StoreUnavailable(
            "a Postgres DSN was configured but the driver is not installed — "
            "install this package with the 'postgres' extra"
        ) from exc
    return psycopg, dict_row


class PostgresStore(JobStore):
    backend = "postgres"

    def __init__(self, dsn: str, *, busy_timeout: float = 5.0, pool_size: int = 4,
                 max_size: int | None = None) -> None:
        psycopg, dict_row = _require_psycopg()
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as exc:  # pragma: no cover
            raise StoreUnavailable(
                "a Postgres DSN was configured but psycopg_pool is not installed — "
                "install this package with the 'postgres' extra"
            ) from exc

        self.dsn = dsn
        self.busy_timeout = busy_timeout
        self._psycopg = psycopg
        # A lock timeout rather than an unbounded wait, for the same reason the
        # SQLite backend has a busy timeout: Slack's handler has three seconds,
        # and a blocked statement that never returns is a 500 with extra steps.
        #
        # APPENDED to whatever the DSN already asked for, never substituted. A
        # kwarg `options` REPLACES the connection string's, so writing it the
        # obvious way silently discards a caller's `search_path` or
        # `application_name` — a setting that vanishes without an error, which
        # is the hardest kind to notice.
        ms = max(1, int(busy_timeout * 1000))
        existing = str(psycopg.conninfo.conninfo_to_dict(dsn).get("options", "") or "")
        self._pool = ConnectionPool(
            dsn,
            min_size=pool_size,
            max_size=max(pool_size, max_size or pool_size * 2),
            kwargs={"row_factory": dict_row, "autocommit": True,
                    "options": f"{existing} -c lock_timeout={ms}".strip()},
            open=True,
        )
        try:
            # WAIT FOR THE POOL, for two reasons that are easy to conflate.
            #
            # The operational one: without it a bad DSN or an unreachable
            # database is not discovered until the first Slack command, which
            # is the worst possible moment and reads as the bot being broken.
            # Failing here means the edge refuses to start, and says why.
            #
            # The correctness one, found by a planted failure: a pool that grows
            # lazily hands out its connections one at a time, so callers that
            # should race instead queue. That silently serialised the store's
            # own concurrency tests, and a cap with its locking removed passed
            # every one of them. A pool that is not warm does not just make
            # concurrency slower — it makes it undetectable.
            self._pool.wait(timeout=max(5.0, busy_timeout))
        except Exception as exc:  # noqa: BLE001
            self._pool.close()
            raise StoreUnavailable(f"cannot reach the Postgres store: {exc}") from exc

        with self._txn() as cur:
            cur.execute(SCHEMA)
            for statement in _MIGRATIONS:
                cur.execute(statement)

    def close(self) -> None:
        self._pool.close()

    # ── connections ──────────────────────────────────────────────────────────

    @contextmanager
    def _cursor(self) -> Iterator[Any]:
        """A single autocommit statement, with driver errors translated."""
        try:
            with self._pool.connection() as conn, conn.cursor() as cur:
                yield cur
        except Exception as exc:  # noqa: BLE001 - re-raised unless it is a busy signal
            raise _translate(exc) from exc

    @contextmanager
    def _txn(self) -> Iterator[Any]:
        """One explicit transaction. Committed on the way out, rolled back on error."""
        try:
            with self._pool.connection() as conn:
                with conn.transaction(), conn.cursor() as cur:
                    yield cur
        except Exception as exc:  # noqa: BLE001
            raise _translate(exc) from exc

    # ── runners ──────────────────────────────────────────────────────────────

    def enrol(self, runner_id: str, public_key: str, labels: Iterable[str],
              now: float | None = None) -> None:
        validate_runner(runner_id, public_key)
        now = now_or(now)
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO runners (runner_id, public_key, labels, enrolled_at, last_seen) "
                "VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT(runner_id) DO UPDATE SET public_key=excluded.public_key, "
                "labels=excluded.labels, last_seen=excluded.last_seen",
                (runner_id, public_key, ",".join(sorted(labels)), now, now),
            )

    def runner(self, runner_id: str) -> dict[str, Any] | None:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM runners WHERE runner_id=%s", (runner_id,))
            return cur.fetchone()

    def touch(self, runner_id: str, now: float | None = None) -> None:
        now = now_or(now)
        with self._cursor() as cur:
            cur.execute("UPDATE runners SET last_seen=%s WHERE runner_id=%s", (now, runner_id))

    def runners(self, offline_after: float, now: float | None = None) -> list[dict[str, Any]]:
        now = now_or(now)
        with self._cursor() as cur:
            cur.execute("SELECT * FROM runners ORDER BY runner_id")
            rows = cur.fetchall()
        return [runner_view(r, offline_after, now) for r in rows]

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
        with self._txn() as cur:
            # Before the count, not after: the whole point is that no other
            # enqueue can read the same number and act on it. Skipped entirely
            # when no cap is set, because then there is no number to protect.
            _lock_if_capped(cur, _LOCK_ENQUEUE, caps)

            cur.execute("SELECT 1 FROM jobs WHERE id=%s", (job.id,))
            if cur.fetchone():
                return EnqueueResult.DUPLICATE

            # Counted inside the transaction that writes. Outside it, two
            # callers read the same number and both write past the cap.
            if caps.max_active_per_job > 0:
                cur.execute(
                    "SELECT COUNT(*) n FROM jobs WHERE product=%s AND server=%s "
                    "AND state = ANY(%s)",
                    (job.product, job.server, list(ACTIVE_STATES)),
                )
                if cur.fetchone()["n"] >= caps.max_active_per_job:
                    return EnqueueResult.JOB_AT_CAPACITY

            # WHICH channel cap applies depends on what is being inserted — see
            # the same comment in the SQLite backend. A dispatched row is
            # already running and is never claimed, so its running cap has to be
            # applied here or it never is.
            channel_cap, channel_states, refusal = (
                (caps.max_queued_per_channel, [QUEUED], EnqueueResult.CHANNEL_QUEUE_FULL)
                if state == QUEUED
                else (caps.max_running_per_channel, list(BUSY_STATES),
                      EnqueueResult.CHANNEL_BUSY)
            )
            if channel_cap > 0:
                cur.execute(
                    "SELECT COUNT(*) n FROM jobs WHERE slack_channel=%s AND state = ANY(%s)",
                    (job.slack_channel, channel_states),
                )
                if cur.fetchone()["n"] >= channel_cap:
                    return refusal

            started = now if state == RUNNING else None
            cur.execute(
                f"INSERT INTO jobs ({_JOB_COLUMNS}) "
                f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (job.id, job.product, job.server, job.select_expr, job.marker,
                 job.slack_channel, job.slack_user, now, state, mode, started),
            )
            return EnqueueResult.ACCEPTED

    def claim(self, runner_id: str, labels: Iterable[str], lease_seconds: float,
              max_attempts: int, *, caps: Caps = NO_CAPS,
              now: float | None = None) -> Job | None:
        now = now_or(now)
        wanted = sorted({x for x in labels if x})
        with self._txn() as cur:
            # Only the per-channel RUNNING cap needs this. The other two are
            # enforced at enqueue, and without any cap the claim is a single
            # atomic statement that needs no help.
            if caps.max_running_per_channel > 0:
                _advisory_lock(cur, _LOCK_CLAIM)
            self._reap(cur, now, max_attempts)
            cur.execute(
                """
                WITH candidate AS (
                    SELECT j.id
                      FROM jobs j
                     WHERE j.state = %(queued)s
                       AND (%(any_server)s OR j.server = ANY(%(labels)s))
                       AND (%(chan_cap)s = 0 OR (
                             SELECT COUNT(*) FROM jobs b
                              WHERE b.slack_channel = j.slack_channel
                                AND b.state = ANY(%(busy)s)
                           ) < %(chan_cap)s)
                     ORDER BY j.created_at ASC
                       FOR UPDATE SKIP LOCKED
                     LIMIT 1
                )
                UPDATE jobs
                   SET state = %(claimed)s,
                       runner_id = %(runner)s,
                       lease_expires = %(expires)s,
                       attempts = jobs.attempts + 1
                  FROM candidate
                 WHERE jobs.id = candidate.id
             RETURNING jobs.*
                """,
                {
                    "queued": QUEUED, "claimed": CLAIMED, "runner": runner_id,
                    "expires": now + lease_seconds,
                    "any_server": not wanted, "labels": wanted,
                    "chan_cap": caps.max_running_per_channel, "busy": list(BUSY_STATES),
                },
            )
            row = cur.fetchone()
        if row is None:
            return None
        return Job(
            id=row["id"], product=row["product"], server=row["server"],
            select_expr=row["select_expr"], marker=row["marker"],
            slack_channel=row["slack_channel"], slack_user=row["slack_user"],
        )

    def mark_running(self, job_id: str, runner_id: str, now: float | None = None) -> bool:
        now = now_or(now)
        with self._cursor() as cur:
            cur.execute(
                "UPDATE jobs SET state=%s, started_at=%s "
                "WHERE id=%s AND runner_id=%s AND state=%s",
                (RUNNING, now, job_id, runner_id, CLAIMED),
            )
            return cur.rowcount == 1

    def finish(self, job_id: str, runner_id: str, *, exit_code: int, passed: int,
               failed: int, skipped: int, duration: float, summary: str,
               now: float | None = None) -> bool:
        now = now_or(now)
        state = DONE if exit_code == 0 else FAILED
        with self._cursor() as cur:
            cur.execute(
                "UPDATE jobs SET state=%s, finished_at=%s, exit_code=%s, passed=%s, "
                "failed=%s, skipped=%s, duration=%s, summary=%s, lease_expires=NULL "
                "WHERE id=%s AND runner_id=%s AND state = ANY(%s)",
                (state, now, exit_code, passed, failed, skipped, duration,
                 summary[:MAX_SUMMARY], job_id, runner_id, [CLAIMED, RUNNING]),
            )
            return cur.rowcount == 1

    def renew(self, runner_id: str, lease_seconds: float, now: float | None = None) -> int:
        now = now_or(now)
        with self._cursor() as cur:
            cur.execute(
                "UPDATE jobs SET lease_expires=%s WHERE runner_id=%s AND state = ANY(%s)",
                (now + lease_seconds, runner_id, [CLAIMED, RUNNING]),
            )
            return cur.rowcount

    def _reap(self, cur: Any, now: float, max_attempts: int) -> None:
        """Requeue anything whose lease ran out; abandon it if it has had enough goes.

        `lease_expires IS NOT NULL` keeps a V1 dispatch out of this: a row from
        `record_dispatch` is `running` with no lease, because nothing will ever
        report back on it, and requeueing it would invent a job no test server
        agreed to take.
        """
        cur.execute(
            "UPDATE jobs SET state=%s, runner_id=NULL, lease_expires=NULL "
            "WHERE state = ANY(%s) AND lease_expires IS NOT NULL AND lease_expires < %s "
            "AND attempts < %s",
            (QUEUED, [CLAIMED, RUNNING], now, max_attempts),
        )
        cur.execute(
            "UPDATE jobs SET state=%s, lease_expires=NULL, "
            "summary='lease expired and out of attempts' "
            "WHERE state = ANY(%s) AND lease_expires IS NOT NULL AND lease_expires < %s "
            "AND attempts >= %s",
            (ABANDONED, [CLAIMED, RUNNING], now, max_attempts),
        )

    def reap(self, max_attempts: int, now: float | None = None) -> None:
        now = now_or(now)
        with self._txn() as cur:
            self._reap(cur, now, max_attempts)

    def job(self, job_id: str) -> dict[str, Any] | None:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM jobs WHERE id=%s", (job_id,))
            return cur.fetchone()

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT %s", (limit,))
            return list(cur.fetchall())

    def last_for(self, product: str) -> dict[str, Any] | None:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM jobs WHERE product=%s ORDER BY created_at DESC LIMIT 1",
                (product,),
            )
            return cur.fetchone()

    def counts(self) -> dict[str, int]:
        with self._cursor() as cur:
            cur.execute("SELECT state, COUNT(*) n FROM jobs GROUP BY state")
            return {r["state"]: r["n"] for r in cur.fetchall()}


def _advisory_lock(cur: Any, key: int) -> None:
    """A transaction-scoped lock, released by COMMIT or ROLLBACK — never by us.

    `pg_advisory_xact_lock` rather than the session-scoped `pg_advisory_lock`
    on purpose: a session lock leaked by an exception is held until the
    connection is returned to the pool and reused, and the symptom is the whole
    queue stopping for no visible reason. There is no unlock call to forget.
    """
    cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (_LOCK_NAMESPACE, key))


def _lock_if_capped(cur: Any, key: int, caps: Caps) -> None:
    """Any cap the insert path can enforce needs the insert path serialised."""
    if caps.max_active_per_job or caps.max_queued_per_channel or caps.max_running_per_channel:
        _advisory_lock(cur, key)


#: Postgres's own words for "somebody else holds this". Matched by name rather
#: than by message text, because the message is localised and the SQLSTATE is not.
_BUSY_SQLSTATES = frozenset({
    "55P03",  # lock_not_available — our lock_timeout fired
    "40001",  # serialization_failure
    "40P01",  # deadlock_detected
    "57014",  # query_canceled, which is what a statement_timeout looks like
})


def _translate(exc: Exception) -> Exception:
    """Turn a driver error into StoreBusy where that is what it means.

    Anything else is handed back untouched: a syntax error dressed up as "the
    runner is busy" would be a defect that never gets found, because the caller
    would retry it forever.
    """
    if isinstance(exc, StoreBusy):
        return exc
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate in _BUSY_SQLSTATES:
        return StoreBusy(f"the store is busy: {exc}")
    return exc


__all__ = ["PostgresStore", "SCHEMA"]
