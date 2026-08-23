"""The store contract: what a queue must do, independent of what it is stored in.

WHY AN INTERFACE AT ALL

The V3 edge shipped with one store — SQLite on local disk — and that is still
the right default: a standalone runner should need no database, no DSN and no
container beyond itself. But the embedded deployment (a sidecar inside an
existing app, strategy B in the README) runs on a Lightsail container service,
and those have **no persistent volume**. SQLite there is not "slower but fine",
it is *deleted on every redeploy* — taking the queue, the leases and the whole
run history with it. That is the requirement this module exists for.

WHAT THE INTERFACE IS ALLOWED TO ASSUME

Almost nothing. Every method here is expressed in terms a key-value store could
not satisfy and a relational one can: a conditional update whose row count is
the answer, and a claim that reads and writes inside one transaction. Those two
shapes are the whole design, and they are why the conformance suite can be
written once and run against both backends without a single `if backend ==`.

THE ONE THING BOTH BACKENDS MUST AGREE ABOUT, AND WOULD NOT IF LEFT TO THEM

Column types are the classic way two backends drift: SQLite ignores
`VARCHAR(n)` entirely while Postgres enforces it, so the same over-long value is
stored on one and rejected on the other — and the defect only ever appears in
production, on whichever backend the tests did not run against.

Both schemas therefore use `TEXT`, so **neither backend has an opinion**, and
the single rule lives here in application code where one implementation serves
both. `validate_job` is called by every backend's own `enqueue`, not by the
caller, so there is no path into the table that skips it.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

#: Every state a job can be in. `queued` and `claimed` are the only two a
#: reaper ever moves between; the rest are terminal or runner-driven.
QUEUED, CLAIMED, RUNNING, DONE, FAILED, ABANDONED = (
    "queued", "claimed", "running", "done", "failed", "abandoned",
)

#: What "this job is occupying a slot" means. A queued job counts: it has been
#: promised to somebody and will run. Anything terminal does not.
ACTIVE_STATES = (QUEUED, CLAIMED, RUNNING)
#: What "this job is executing right now" means — the narrower set the
#: per-channel running cap is measured over.
BUSY_STATES = (CLAIMED, RUNNING)

#: Longest value accepted for any identifier-ish column. Enforced here rather
#: than by the column type, because only one of the two backends would enforce
#: the column type. See the module docstring.
MAX_FIELD = 200
#: The summary is the one long field; it is truncated rather than refused,
#: because a run whose result is 4 KB of pytest output still has a real result.
MAX_SUMMARY = 2000


class StoreError(RuntimeError):
    """Anything the store could not do. Never leaks a driver exception upward."""


class StoreBusy(StoreError):
    """The backend could not get a lock inside its bounded wait.

    This exists so a caller can say "the runner is busy, try again" instead of
    returning a 500. SQLite serialises writers, so a burst of slash commands
    against the file-backed store produces `database is locked` — which is a
    *load* signal, not a fault, and must not read to a user as the bot being
    broken. The wait is bounded (see `busy_timeout`) so a caller is never held
    past Slack's three-second budget.
    """


class StoreUnavailable(StoreError):
    """The configured backend cannot be opened at all — bad DSN, missing driver."""


class EnqueueResult(Enum):
    """Why a job was or was not queued.

    A bool cannot carry this. The old `enqueue` returned False for a duplicate,
    and a cap refusal is not a duplicate — one means "you already asked for
    this", the other means "you are asking for too much at once", and telling a
    user the wrong one of those sends them looking in the wrong place.
    """

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    JOB_AT_CAPACITY = "job_at_capacity"
    #: Too many of this channel's runs are WAITING. Only the queueing path can
    #: hit this, because only it has a queue.
    CHANNEL_QUEUE_FULL = "channel_queue_full"
    #: Too many of this channel's runs are RUNNING. Only `record_dispatch` hits
    #: this: on the queueing path the same cap is applied when a test server
    #: claims, which is the moment a run actually starts.
    CHANNEL_BUSY = "channel_busy"

    @property
    def accepted(self) -> bool:
        return self is EnqueueResult.ACCEPTED


@dataclass(frozen=True, slots=True)
class Caps:
    """Concurrency limits, enforced inside the store's own transactions.

    THREE CAPS, THREE DIFFERENT JOBS. They are not variations on one number:

      * `max_active_per_job` bounds *repetition* — how many runs of the same
        (product, server) may exist at once. The default of 1 means typing the
        same command twice while the first is still going is refused rather
        than doubled.
      * `max_queued_per_channel` bounds the *backlog* — the "a chat box cannot
        queue fifty runs" requirement, literally.
      * `max_running_per_channel` bounds *execution* — how many of one
        channel's runs may occupy test servers simultaneously, so one busy
        channel cannot starve every other one. On the V1/V2 path, where a
        dispatch starts immediately and there is no queue at all, this is the
        cap that stops a chat box starting fifty runs at once.

    Zero means unlimited for any of them.

    WHERE THEY ARE CHECKED IS THE WHOLE POINT. The first two are checked in the
    same transaction as the INSERT; the third in the same transaction as the
    claim. A cap checked in one statement and acted on in the next is not a cap
    — two callers both read "3 running" and both make it 4. That is why this
    lives here and not in a handler.
    """

    max_active_per_job: int = 0
    max_queued_per_channel: int = 0
    max_running_per_channel: int = 0


#: What the store does when no caps are configured: nothing. Callers that do
#: not care (tests of unrelated behaviour, the reaper) pass this and get the
#: pre-cap semantics exactly.
NO_CAPS = Caps()


@dataclass(slots=True)
class Job:
    id: str
    product: str
    server: str
    select_expr: str | None
    marker: str | None
    slack_channel: str
    slack_user: str

    def as_dispatch(self) -> dict[str, Any]:
        """The shape handed to a test server. Deliberately minimal.

        No Slack tokens, no internal hostnames, no config — just what is needed
        to run a suite and say where the answer goes. A job payload is the one
        thing that crosses from the public edge onto an internal machine, so
        the less it carries the less a forged one could do.
        """
        return {
            "job_id": self.id,
            "product": self.product,
            "server": self.server,
            "select": self.select_expr or "",
            "marker": self.marker or "",
            "slack_channel": self.slack_channel,
            "slack_user": self.slack_user,
        }


def validate_job(job: Job) -> None:
    """Refuse a job both backends would treat differently. Raises StoreError.

    Called by every backend's `enqueue` before the INSERT, rather than by the
    caller, so there is no path into the table that skips it.
    """
    if not job.id:
        raise StoreError("a job needs an id")
    for name, value in (
        ("id", job.id),
        ("product", job.product),
        ("server", job.server),
        ("slack_channel", job.slack_channel),
        ("slack_user", job.slack_user),
        ("select", job.select_expr or ""),
        ("marker", job.marker or ""),
    ):
        if len(value) > MAX_FIELD:
            raise StoreError(f"{name} is longer than {MAX_FIELD} characters")


def validate_runner(runner_id: str, public_key: str) -> None:
    if not runner_id or len(runner_id) > MAX_FIELD:
        raise StoreError(f"runner_id must be 1..{MAX_FIELD} characters")
    if not public_key or len(public_key) > MAX_FIELD:
        raise StoreError(f"public_key must be 1..{MAX_FIELD} characters")


def now_or(now: float | None) -> float:
    return time.time() if now is None else now


def runner_view(row: dict[str, Any], offline_after: float, now: float) -> dict[str, Any]:
    """One enrolled test server, as the admin view and the edge both want it."""
    return {
        "runner_id": row["runner_id"],
        "labels": [x for x in str(row["labels"] or "").split(",") if x],
        "enrolled_at": row["enrolled_at"],
        "last_seen": row["last_seen"],
        "seconds_since_seen": round(now - row["last_seen"], 1),
        "state": "online" if (now - row["last_seen"]) <= offline_after else "offline",
    }


class JobStore(ABC):
    """The queue and the registry. Two implementations, one conformance suite."""

    #: "sqlite" or "postgres". Printed at startup, and the parameter id the
    #: conformance suite runs under — so a report naming one backend is
    #: unambiguous about which one it exercised.
    backend: str = "unknown"

    #: How long a write waits for a lock before raising StoreBusy. Bounded on
    #: purpose: Slack gives the whole handler three seconds.
    busy_timeout: float = 5.0

    def close(self) -> None:  # pragma: no cover - overridden where it matters
        """Release whatever the backend holds. Safe to call twice."""

    # ── runners ──────────────────────────────────────────────────────────────

    @abstractmethod
    def enrol(self, runner_id: str, public_key: str, labels: Iterable[str],
              now: float | None = None) -> None: ...

    @abstractmethod
    def runner(self, runner_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def touch(self, runner_id: str, now: float | None = None) -> None: ...

    @abstractmethod
    def runners(self, offline_after: float, now: float | None = None) -> list[dict[str, Any]]: ...

    def online(self, offline_after: float, now: float | None = None) -> list[dict[str, Any]]:
        return [r for r in self.runners(offline_after, now) if r["state"] == "online"]

    # ── jobs ─────────────────────────────────────────────────────────────────

    @abstractmethod
    def enqueue(self, job: Job, *, caps: Caps = NO_CAPS,
                now: float | None = None) -> EnqueueResult:
        """Queue a job for a test server to claim.

        Idempotent on `job.id`, which is derived from Slack's `trigger_id` — so
        a Slack retry lands on the same row and gets DUPLICATE. The uniqueness
        is a PRIMARY KEY rather than a remembered set, because a set is a second
        structure to keep in step and the first thing lost on a restart.
        """

    @abstractmethod
    def record_dispatch(self, job: Job, *, mode: str, caps: Caps = NO_CAPS,
                        now: float | None = None) -> EnqueueResult:
        """Record a run this process dispatched itself, rather than queued.

        This is the V1/V2 path: the API either spawns pytest locally or fires a
        `workflow_dispatch` and never hears back. There is no claim, no lease
        and no result, so the row goes straight to `running` and stays there —
        which is exactly as much as that deployment ever knew.

        It exists because the alternative was a module-level dict, and a dict is
        per-worker: two uvicorn workers each got their own and the idempotency
        guarantee quietly disappeared. Same table, same primary key, same
        answer under any number of workers.
        """

    @abstractmethod
    def claim(self, runner_id: str, labels: Iterable[str], lease_seconds: float,
              max_attempts: int, *, caps: Caps = NO_CAPS,
              now: float | None = None) -> Job | None:
        """Hand exactly one queued job to this test server, or None.

        The read of the queue and the write that takes the row are one
        transaction. Anything less lets two test servers both see the same
        `queued` row and both act on it — which is not a rare race but the
        normal case when three servers long-poll the same queue.
        """

    @abstractmethod
    def mark_running(self, job_id: str, runner_id: str, now: float | None = None) -> bool: ...

    @abstractmethod
    def finish(self, job_id: str, runner_id: str, *, exit_code: int, passed: int,
               failed: int, skipped: int, duration: float, summary: str,
               now: float | None = None) -> bool:
        """Record a result — but only from the test server that holds the job.

        The `runner_id` match is the security control, not an optimisation.
        Without it any enrolled test server could post a result for a job it was
        never given, and since the test servers are what talk to Slack, a forged
        result is a forged message in the channel. It lives in the WHERE clause
        so no code path can forget to call a checker.
        """

    @abstractmethod
    def renew(self, runner_id: str, lease_seconds: float, now: float | None = None) -> int: ...

    @abstractmethod
    def reap(self, max_attempts: int, now: float | None = None) -> None: ...

    @abstractmethod
    def job(self, job_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def recent(self, limit: int = 20) -> list[dict[str, Any]]: ...

    @abstractmethod
    def last_for(self, product: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def counts(self) -> dict[str, int]: ...


__all__ = [
    "ABANDONED", "ACTIVE_STATES", "BUSY_STATES", "CLAIMED", "Caps", "DONE",
    "EnqueueResult", "FAILED", "Job", "JobStore", "MAX_FIELD", "MAX_SUMMARY",
    "NO_CAPS", "QUEUED", "RUNNING", "StoreBusy", "StoreError", "StoreUnavailable",
    "now_or", "runner_view", "validate_job", "validate_runner",
]
