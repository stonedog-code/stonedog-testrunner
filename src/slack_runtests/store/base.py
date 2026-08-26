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
QUEUED, CLAIMED, RUNNING, DONE, FAILED, ABANDONED, NOT_DISPATCHED = (
    "queued", "claimed", "running", "done", "failed", "abandoned", "not_dispatched",
)

#: `not_dispatched` is a DIFFERENT FACT from `failed`, and the two must never
#: share a state.
#:
#: `failed` means the suite ran and did not pass. `not_dispatched` means it
#: never started -- GitHub refused the workflow, or could not be reached. A
#: reader who cannot tell those apart will go looking for a test failure that
#: does not exist, and a History surface built on a shared state would report
#: the wrong one with complete confidence.
#:
#: It exists because the gh-action dispatch happens AFTER the reply to Slack has
#: gone (NEH-1156). The user has already been told the run is on its way, the
#: edge holds no bot token and cannot correct that, so the record is the only
#: place the truth can live.

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

    #: Which DEFINITION produced this run, when one did (NEH-1167).
    #:
    #: Optional, and it must stay optional. Runs recorded before definitions
    #: existed have none, and a migration that made this required would make
    #: every one of them unreadable — the history is the thing being preserved.
    #:
    #: NOT a foreign key with a cascade. The run happened; deleting its
    #: definition must not erase the record of what it did. An orphaned id is
    #: correct, and a reader says "the definition is gone" rather than hiding
    #: the run.
    job_def_id: str | None = None

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


# ─────────────────────────────────────────────────────────────────────────────
# JOB DEFINITIONS (PRD A2.2)
#
# A `Job` above is a RUN: one row per execution, with a state, a lease and an
# attempt count. A `JobDef` here is a DEFINITION: a saved name, trigger and
# action, which produces runs. Two different things, and the PRD calls both of
# them "job".
#
# They are deliberately not named `Job` and `JobDefinition` in the schema. The
# tables are `jobs` and `job_defs`, because `jobs` and `job_definitions` differ
# by a suffix a hurried reader skips, and the two are joined in the same queries.
#
# THE RULE THAT SHAPES ALL OF THIS (A2.3):
#
#     A stored row is a REQUEST, never an AUTHORISATION.
#
# Nothing in this module decides that a job may run. The allowlists stay in
# configuration (NEH-1139) and are re-checked at execution against whatever the
# process is configured with NOW — a row that was valid when saved and is
# invalid today is refused, because configuration drifts and the check that
# matters is the one at use. So `save_job_def` validates SHAPE, and the
# execution path validates AUTHORITY, and they are different functions on
# purpose.
# ─────────────────────────────────────────────────────────────────────────────


class ActionKind(str, Enum):
    """What a job does when its trigger fires."""

    #: Dispatch a workflow in an allowlisted repository. WHICH repo is
    #: code/env; which workflow inside it may be a row (A2.3).
    GH_ACTION = "gh-action"
    #: Hand the job to an enrolled test server.
    TEST_SERVER = "test-server"


class Language(str, Enum):
    """What the suite under test is written in.

    A CLOSED SET, and not free text, because this value chooses a workflow
    FILENAME. Free text there is a path fragment an operator types, which is
    the shape `RUNTESTS_PRODUCTS` exists to stop for products.

    ## Why this is stored and NOT dispatched

    `workflow_dispatch` accepts at most ten inputs and `runners/github.py`
    already sends eight. Spending the ninth on something that only ever picks
    between two workflow files buys nothing: the choice can be made here, at
    save, where it is also checkable.

    So `language` derives `action_target` and never leaves the edge. The
    operator sets a language; the workflow that runs follows from it.
    """

    PYTHON = "python"
    NODE = "node"


#: The workflow each language runs, unless a definition overrides it.
#:
#: Two files rather than one taking a `language` input, deliberately. One
#: workflow serving both needs `if:` around every setup, cache and install
#: step, and the input budget above is genuinely tight.
DEFAULT_WORKFLOW: dict[str, str] = {
    Language.PYTHON.value: "runtests-python.yml",
    Language.NODE.value: "runtests-node.yml",
}


def default_action_target(language: str) -> str:
    """The workflow a language runs by default.

    Returns "" for an unknown language rather than guessing. The caller is
    validating anyway, and a guess here would hand a plausible filename to a
    definition whose language was rejected a moment later.
    """
    return DEFAULT_WORKFLOW.get(language, "")


class SaveResult(Enum):
    """Why a definition was or was not saved.

    A bool cannot carry this, for the same reason `EnqueueResult` exists: a
    duplicate trigger and a malformed row need different messages, and telling
    somebody the wrong one sends them to edit the wrong field.
    """

    CREATED = "created"
    UPDATED = "updated"
    #: Another definition already claims this exact (product, test_scope,
    #: server). A2.2.2 requires this be refused AT SAVE rather than resolved at
    #: match time by whichever row was written last.
    DUPLICATE_TRIGGER = "duplicate_trigger"


@dataclass(frozen=True, slots=True)
class JobDef:
    id: str
    name: str
    product: str
    test_scope: str
    server: str
    action_kind: str
    action_target: str
    #: Which suite runner this product uses. Chooses the workflow; never
    #: dispatched. REQUIRED -- there is deliberately no default.
    #:
    #: A default would have to be a guess about somebody else's repository, and
    #: the guess is free to make wrong because nothing downstream would notice:
    #: a Node product defaulted to python dispatches a workflow that is simply
    #: absent, which reports as a missing workflow rather than as a wrong
    #: language. Measured before choosing: `job_defs` held ZERO rows in prod
    #: when this landed, so requiring it backfills nothing.
    language: str
    description: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def trigger(self) -> tuple[str, str, str]:
        """The full tuple a command must match. A2.2.2: exact, never partial."""
        return (self.product, self.test_scope, self.server)

    def trigger_text(self) -> str:
        """The canonical command that fires this, in flag form.

        Flags rather than positionals because this is what a person READS in a
        definition, and the positional form is what they type (A2.2.1). One
        grammar, two spellings; this is the spelling that says which is which.
        """
        return (f"runtests --product {self.product} "
                f"--test_scope {self.test_scope} --server {self.server}")


def validate_job_def(job_def: JobDef) -> None:
    """Refuse a definition both backends would treat differently, or that is
    structurally meaningless. Raises StoreError.

    SHAPE ONLY — deliberately. Whether `product` is on the allowlist is an
    authorisation question, and A2.3 puts that at execution time against live
    configuration rather than here against whatever was true at save. Checking
    it in this function too would look thorough and would be the beginning of
    the store becoming the boundary.
    """
    if not job_def.id:
        raise StoreError("a job definition needs an id")
    if not job_def.name.strip():
        raise StoreError("a job definition needs a name")

    if job_def.action_kind not in {k.value for k in ActionKind}:
        raise StoreError(
            f"action_kind must be one of "
            f"{', '.join(sorted(k.value for k in ActionKind))}, "
            f"got {job_def.action_kind!r}"
        )
    if not job_def.action_target.strip():
        raise StoreError("a job definition needs an action target")

    if job_def.language not in {lang.value for lang in Language}:
        raise StoreError(
            f"language must be one of "
            f"{', '.join(sorted(lang.value for lang in Language))}, "
            f"got {job_def.language!r}"
        )

    for name, value in (
        ("id", job_def.id),
        ("name", job_def.name),
        ("product", job_def.product),
        ("test_scope", job_def.test_scope),
        ("server", job_def.server),
        ("action_target", job_def.action_target),
        ("description", job_def.description),
    ):
        if len(value) > MAX_FIELD:
            raise StoreError(f"{name} is longer than {MAX_FIELD} characters")

    # Every trigger token is required. An empty one would make the tuple match
    # a command that named nothing there, which is a partial match wearing an
    # exact match's clothes.
    for name in ("product", "test_scope", "server"):
        if not getattr(job_def, name).strip():
            raise StoreError(f"a job definition needs a {name}")


def near_misses(
    requested: tuple[str, str, str],
    definitions: Iterable[JobDef],
) -> list[JobDef]:
    """Definitions differing from `requested` in exactly one trigger token.

    A2.2.2: a command matching no job is refused WITH THE LIST OF WHAT WOULD
    HAVE MATCHED. Silently ignoring it reads to the user as the bot being down,
    and "no such job" with no further help reads as the job having been deleted.

    One token, not two: a definition differing in all three is not a near miss,
    it is a different job, and listing every definition as a suggestion is the
    same as listing none.

    Sorted by name so the same mistake produces the same message twice running.
    """
    out = [
        d for d in definitions
        if sum(1 for a, b in zip(requested, d.trigger) if a != b) == 1
    ]
    return sorted(out, key=lambda d: d.name)


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
    def mark_not_dispatched(self, job_id: str, reason: str,
                            now: float | None = None) -> bool:
        """Record that a job never started. True if this call changed the row.

        Only from QUEUED. A job a runner has already claimed is somebody else's
        to finish, and a dispatch failure arriving late must not overwrite a
        real result -- so the state is in the WHERE clause rather than checked
        first, which is the same shape `finish` uses and for the same reason.

        `reason` is stored in `summary` and is written for a USER: GitHub's own
        error bodies carry repository names and belong in the log, never in a
        column something renders.
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
    def runs_for_job_def(self, job_def_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """This definition's runs, newest first.

        Matched on `job_def_id` and nothing else. The tempting approximation --
        match on product and server -- is WRONG rather than absent: two
        definitions differing only in `test_scope` would share a history
        belonging to neither, and a History table showing the wrong runs looks
        exactly like one showing the right runs.
        """

    @abstractmethod
    def last_for(self, product: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def counts(self) -> dict[str, int]: ...

    # ── job definitions (A2.2) ───────────────────────────────────────────────

    @abstractmethod
    def save_job_def(self, job_def: JobDef, *, now: float | None = None) -> SaveResult:
        """Create or replace a definition, keyed by id.

        Returns DUPLICATE_TRIGGER when another id already claims this exact
        (product, test_scope, server).

        THE REFUSAL MUST COME FROM A UNIQUE CONSTRAINT, NOT A PRIOR SELECT.
        Two concurrent saves both pass a read-then-write check and both insert,
        which is precisely the state A2.2.2 says must be impossible -- and it
        would then be discovered at match time, by whichever row the database
        felt like returning first.
        """

    @abstractmethod
    def job_def(self, job_def_id: str) -> JobDef | None: ...

    @abstractmethod
    def job_def_for(self, product: str, test_scope: str, server: str) -> JobDef | None:
        """The definition matching this EXACT tuple, or None.

        Exact, never partial: a definition matching two of three tokens is a
        different job, and returning it would run something nobody asked for.
        """

    @abstractmethod
    def job_defs(self) -> list[JobDef]:
        """Every definition, ordered by name so two listings agree."""

    @abstractmethod
    def delete_job_def(self, job_def_id: str) -> bool:
        """True if a row was removed, False if there was nothing to remove.

        The distinction matters to a caller reporting to a person: "deleted"
        over an id that never existed is a lie that reads as success.
        """

    def count_job_defs(self) -> int:
        """How many definitions exist.

        Concrete rather than abstract: `job_defs()` already has to be right, and
        a backend that got this wrong separately would be wrong in a way nothing
        else notices. It exists at all because a startup line saying
        `job definitions loaded` says the same thing over zero as over five.
        """
        return len(self.job_defs())


__all__ = [
    "ABANDONED", "ACTIVE_STATES", "NOT_DISPATCHED", "BUSY_STATES", "CLAIMED", "Caps", "DONE",
    "EnqueueResult", "FAILED", "Job", "JobStore", "MAX_FIELD", "MAX_SUMMARY",
    "NO_CAPS", "QUEUED", "RUNNING", "StoreBusy", "StoreError", "StoreUnavailable",
    "now_or", "runner_view", "validate_job", "validate_runner",
]
