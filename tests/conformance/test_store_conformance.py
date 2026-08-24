"""Every guarantee the queue makes, asserted once and run against every backend.

These are the properties the three-test-server harness demonstrates at runtime.
Demonstrating is not proving — a race that shows up once in fifty runs looks
like a green harness — so they are pinned here where the timing is controlled.

NOT ONE `if backend ==` IN THIS FILE, ON PURPOSE. The moment a test needs to
know which store it is talking to, the two implementations have been allowed to
mean different things, and the suite has stopped being a conformance suite.
"""

from __future__ import annotations

import threading

import pytest

from slack_runtests.store import (
    ABANDONED, CLAIMED, DONE, FAILED, NO_CAPS, QUEUED, RUNNING, Caps,
    EnqueueResult, JobStore, MAX_SUMMARY, NOT_DISPATCHED, SaveResult, StoreError,
)

pytestmark = pytest.mark.unit


# ── idempotency ──────────────────────────────────────────────────────────────

def test_the_same_job_id_is_only_queued_once(store: JobStore, make_job) -> None:
    """Slack retries anything slow. The PRIMARY KEY is the whole mechanism."""
    assert store.enqueue(make_job()) is EnqueueResult.ACCEPTED
    assert store.enqueue(make_job()) is EnqueueResult.DUPLICATE
    assert store.counts() == {QUEUED: 1}


def test_a_duplicate_and_a_cap_refusal_are_different_answers(
    store: JobStore, make_job
) -> None:
    """They send a user to two different places, so they must not share a value.

    The old `enqueue` returned a bool, and a bool cannot carry this. "You already
    asked for this" and "too much of this is already happening" have different
    remedies, and reporting the wrong one is how a working cap gets raised as a
    bug.
    """
    caps = Caps(max_active_per_job=1)
    assert store.enqueue(make_job("a"), caps=caps) is EnqueueResult.ACCEPTED
    assert store.enqueue(make_job("a"), caps=caps) is EnqueueResult.DUPLICATE
    assert store.enqueue(make_job("b"), caps=caps) is EnqueueResult.JOB_AT_CAPACITY


def test_a_dispatched_run_is_remembered_across_a_reopen(store: JobStore, make_job) -> None:
    """The V1 path's record, which used to be a per-worker dict.

    A dict answered "no recorded run" about a suite that had just finished, and
    gave a second worker no way to know the first had already dispatched.
    """
    assert store.record_dispatch(make_job(), mode="github") is EnqueueResult.ACCEPTED
    assert store.record_dispatch(make_job(), mode="github") is EnqueueResult.DUPLICATE

    row = store.job("job-1")
    assert row["state"] == RUNNING
    assert row["dispatch_mode"] == "github"
    assert row["started_at"] is not None


def test_a_dispatched_run_is_never_requeued_by_the_reaper(
    store: JobStore, make_job
) -> None:
    """It has no lease, because nothing is ever going to report back on it.

    Requeueing one would invent a job no test server agreed to take — and then
    hand it to a real one, which would run a suite nobody asked for.
    """
    store.record_dispatch(make_job(), mode="local", now=1_000.0)
    store.reap(max_attempts=2, now=99_000.0)

    assert store.job("job-1")["state"] == RUNNING
    assert store.claim("runner-1", [], 60, 3, now=99_001.0) is None


def test_simultaneous_retries_of_one_command_do_not_raise(
    store: JobStore, make_job
) -> None:
    """Slack does not retry politely one at a time, and neither does this test.

    NO CAPS on purpose. Idempotency must not depend on a cap being configured —
    the Postgres backend originally serialised `_insert` with an advisory lock
    taken only when a cap was set, so turning caps off silently turned this
    guarantee off with them.

    Found by review and reproduced before it was fixed: eight simultaneous
    identical enqueues gave one `accepted` and SEVEN unhandled UniqueViolations,
    every one of which is a 500 on the exact path idempotency exists for.
    """
    ready = threading.Barrier(8, timeout=30)
    outcomes: list[EnqueueResult] = []
    errors: list[str] = []
    lock = threading.Lock()

    def retry() -> None:
        ready.wait()
        try:
            outcome = store.enqueue(make_job("same-trigger"), caps=NO_CAPS)
        except Exception as exc:  # noqa: BLE001 - the point is that there are none
            with lock:
                errors.append(type(exc).__name__)
            return
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=retry) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert errors == [], f"a retry raised instead of answering: {errors}"
    assert sum(1 for o in outcomes if o is EnqueueResult.ACCEPTED) == 1
    assert sum(1 for o in outcomes if o is EnqueueResult.DUPLICATE) == 7
    assert store.counts() == {QUEUED: 1}


# ── exactly-once claim ───────────────────────────────────────────────────────

def test_one_job_goes_to_exactly_one_test_server(store: JobStore, make_job) -> None:
    store.enqueue(make_job())

    claims = [store.claim(f"runner-{n}", [], 60, 3) for n in range(1, 4)]
    won = [c for c in claims if c is not None]

    assert len(won) == 1, "a job must never be handed to two machines"
    assert won[0].id == "job-1"


def test_concurrent_claims_do_not_double_assign(store: JobStore, make_job) -> None:
    """The race the harness creates, run deliberately.

    Ten threads against ten jobs: every job must be claimed exactly once. This
    is the property the two backends implement in completely different SQL —
    `BEGIN IMMEDIATE` on one, `FOR UPDATE SKIP LOCKED` on the other — and the
    reason the suite is parameterised rather than written twice.
    """
    for n in range(10):
        store.enqueue(make_job(f"job-{n}"))

    results: list[str] = []
    lock = threading.Lock()

    def grab(runner: str) -> None:
        while True:
            job = store.claim(runner, [], 60, 3)
            if job is None:
                return
            with lock:
                results.append(job.id)

    threads = [threading.Thread(target=grab, args=(f"runner-{n}",)) for n in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert len(results) == 10
    assert len(set(results)) == 10, "a job was claimed twice"


def test_an_empty_queue_returns_nothing_rather_than_blocking(store: JobStore) -> None:
    assert store.claim("runner-1", [], 60, 3) is None


# ── routing ──────────────────────────────────────────────────────────────────

def test_a_labelled_server_only_takes_jobs_for_its_environments(
    store: JobStore, make_job
) -> None:
    store.enqueue(make_job("job-prod-ish", server="staging"))

    assert store.claim("dev-only", ["dev"], 60, 3) is None
    assert store.claim("staging-box", ["staging"], 60, 3) is not None


def test_an_unlabelled_server_takes_anything(store: JobStore, make_job) -> None:
    """Empty labels is the shared pool — the default, and what the harness uses."""
    store.enqueue(make_job(server="dev"))

    assert store.claim("general", [], 60, 3) is not None


# ── concurrency caps ─────────────────────────────────────────────────────────

def test_the_same_product_and_server_cannot_be_queued_twice_over(
    store: JobStore, make_job
) -> None:
    """Typing the same command twice is a mistake far more often than a request."""
    caps = Caps(max_active_per_job=1)
    assert store.enqueue(make_job("first"), caps=caps) is EnqueueResult.ACCEPTED
    assert store.enqueue(make_job("second"), caps=caps) is EnqueueResult.JOB_AT_CAPACITY

    # A different server is a different job, and is not affected.
    assert store.enqueue(
        make_job("third", server="dev"), caps=caps
    ) is EnqueueResult.ACCEPTED


def test_the_cap_frees_up_when_the_run_finishes(store: JobStore, make_job) -> None:
    """A cap that never releases is an outage with a friendly message."""
    caps = Caps(max_active_per_job=1)
    store.enqueue(make_job("first"), caps=caps)
    store.claim("runner-1", [], 60, 3)
    store.finish("first", "runner-1", exit_code=0, passed=1, failed=0, skipped=0,
                 duration=1.0, summary="")

    assert store.enqueue(make_job("second"), caps=caps) is EnqueueResult.ACCEPTED


def test_a_channel_cannot_queue_more_than_its_share(store: JobStore, make_job) -> None:
    """The "a chat box cannot queue fifty runs" cap, literally."""
    caps = Caps(max_queued_per_channel=2)
    for n in range(2):
        assert store.enqueue(
            make_job(f"job-{n}", product=f"p{n}"), caps=caps
        ) is EnqueueResult.ACCEPTED

    assert store.enqueue(
        make_job("job-over", product="p9"), caps=caps
    ) is EnqueueResult.CHANNEL_QUEUE_FULL
    # Another channel is unaffected — one noisy room must not stop the others.
    assert store.enqueue(
        make_job("job-elsewhere", product="p9", channel="#other"), caps=caps
    ) is EnqueueResult.ACCEPTED


def test_a_busy_channel_cannot_occupy_every_test_server(
    store: JobStore, make_job
) -> None:
    """The running cap, applied in the same transaction that takes the row.

    This is the one that cannot be done in a handler: a check and a claim in two
    statements let two claimers both read "1 running" and both make it 2. The
    third job here must stay queued, not be refused — it runs as soon as one of
    the first two finishes.
    """
    caps = Caps(max_running_per_channel=2)
    for n in range(3):
        store.enqueue(make_job(f"job-{n}", product=f"p{n}"), caps=caps)

    taken = [store.claim(f"runner-{n}", [], 60, 3, caps=caps) for n in range(3)]
    assert [t is not None for t in taken] == [True, True, False]
    assert store.counts()[QUEUED] == 1

    store.finish(taken[0].id, "runner-0", exit_code=0, passed=1, failed=0, skipped=0,
                 duration=1.0, summary="")
    assert store.claim("runner-3", [], 60, 3, caps=caps) is not None


def test_concurrent_claims_cannot_both_pass_the_running_cap(
    store: JobStore, make_job
) -> None:
    """The claim-side cap under the concurrency it exists for.

    Eight test servers race for eight jobs in one channel under a running cap of
    two. Exactly two may be claimed. This is the same shape as the enqueue race
    and it fails the same way: read "1 running" in one statement, take a row in
    the next, and several claimers all get past a cap none of them exceeded when
    they looked.
    """
    caps = Caps(max_running_per_channel=2)
    for n in range(8):
        store.enqueue(make_job(f"job-{n}", product=f"p{n}"), caps=NO_CAPS)

    taken: list[str] = []
    lock = threading.Lock()
    # A BARRIER, not just eight threads. Started one at a time they finish one
    # at a time — each claim is a single fast statement — and the race never
    # happens, so the test passes with the serialisation removed. Measured:
    # without this barrier, deleting the lock this test exists for changed
    # nothing. A concurrency test that does not force concurrency is a green
    # result over an empty set wearing a thread pool.
    ready = threading.Barrier(8, timeout=30)

    def grab(runner: str) -> None:
        ready.wait()
        job = store.claim(runner, [], 60, 3, caps=caps)
        if job is not None:
            with lock:
                taken.append(job.id)

    threads = [threading.Thread(target=grab, args=(f"runner-{n}",)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert len(taken) == 2, f"the running cap let {len(taken)} through"
    assert store.counts()[QUEUED] == 6


def test_a_dispatched_run_honours_the_running_cap_at_insert(
    store: JobStore, make_job
) -> None:
    """The V1/V2 path has no queue, so a cap it cannot apply here is never applied.

    `record_dispatch` inserts a row that is ALREADY running and will never be
    claimed. Applying the backlog cap to it would count `queued` rows that can
    never exist — a cap that is structurally always satisfied, which is the
    check-over-an-empty-set failure in cap form. The running cap is the one that
    means something here, and it is what stops a chat box starting fifty local
    pytest processes at once.
    """
    caps = Caps(max_running_per_channel=2, max_queued_per_channel=1)
    assert store.record_dispatch(make_job("a", product="p1"), mode="local",
                                 caps=caps) is EnqueueResult.ACCEPTED
    assert store.record_dispatch(make_job("b", product="p2"), mode="local",
                                 caps=caps) is EnqueueResult.ACCEPTED
    assert store.record_dispatch(make_job("c", product="p3"), mode="local",
                                 caps=caps) is EnqueueResult.CHANNEL_BUSY

    # A different channel is unaffected, and so is a queued job — the two caps
    # count different things.
    assert store.record_dispatch(make_job("d", product="p4", channel="#other"),
                                 mode="local", caps=caps) is EnqueueResult.ACCEPTED


def test_a_cap_of_zero_means_unlimited(store: JobStore, make_job) -> None:
    """Because "off" must be expressible, and must not read as "none allowed"."""
    for n in range(5):
        assert store.enqueue(make_job(f"job-{n}"), caps=NO_CAPS) is EnqueueResult.ACCEPTED
    assert store.counts() == {QUEUED: 5}


def test_concurrent_enqueues_cannot_both_pass_the_same_cap(
    store: JobStore, make_job
) -> None:
    """The property a cap checked outside its transaction does NOT have.

    Twelve threads race to queue twelve distinct jobs for one channel under a
    cap of three. Exactly three may be accepted. Read the count in one statement
    and write in the next, and several threads all see "2 queued" and all write.
    """
    caps = Caps(max_queued_per_channel=3)
    accepted: list[EnqueueResult] = []
    lock = threading.Lock()
    ready = threading.Barrier(12, timeout=30)

    def enqueue(n: int) -> None:
        ready.wait()
        outcome = store.enqueue(make_job(f"job-{n}", product=f"p{n}"), caps=caps)
        with lock:
            accepted.append(outcome)

    threads = [threading.Thread(target=enqueue, args=(n,)) for n in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert len(accepted) == 12
    assert sum(1 for a in accepted if a is EnqueueResult.ACCEPTED) == 3
    assert store.counts()[QUEUED] == 3


# ── leases ───────────────────────────────────────────────────────────────────

def test_an_expired_lease_returns_the_job_to_the_queue(store: JobStore, make_job) -> None:
    """A test server that dies mid-run must not take the job with it."""
    store.enqueue(make_job())
    claimed = store.claim("runner-1", [], lease_seconds=10, max_attempts=3, now=1_000.0)
    assert claimed is not None

    recovered = store.claim("runner-2", [], lease_seconds=10, max_attempts=3, now=1_011.0)

    assert recovered is not None and recovered.id == "job-1"
    assert store.job("job-1")["runner_id"] == "runner-2"


def test_a_heartbeat_keeps_a_running_job_from_being_stolen(
    store: JobStore, make_job
) -> None:
    """The failure this prevents: a healthy server declared dead because it was busy."""
    store.enqueue(make_job())
    store.claim("runner-1", [], lease_seconds=10, max_attempts=3, now=1_000.0)
    store.renew("runner-1", lease_seconds=10, now=1_008.0)

    assert store.claim("runner-2", [], lease_seconds=10, max_attempts=3, now=1_011.0) is None


def test_a_job_that_keeps_killing_its_runner_is_abandoned(
    store: JobStore, make_job
) -> None:
    """Otherwise one poisonous job takes down all three machines in turn."""
    store.enqueue(make_job())
    store.claim("runner-1", [], lease_seconds=10, max_attempts=2, now=1_000.0)
    store.claim("runner-2", [], lease_seconds=10, max_attempts=2, now=1_011.0)
    store.reap(max_attempts=2, now=1_022.0)

    assert store.job("job-1")["state"] == ABANDONED
    assert store.claim("runner-3", [], 10, 2, now=1_030.0) is None


# ── ownership ────────────────────────────────────────────────────────────────

def test_only_the_holder_can_report_a_result(store: JobStore, make_job) -> None:
    """A forged result is a forged Slack message — the test servers do the posting."""
    store.enqueue(make_job())
    store.claim("runner-1", [], 60, 3)

    stolen = store.finish("job-1", "runner-2", exit_code=0, passed=99, failed=0,
                          skipped=0, duration=1.0, summary="")
    assert stolen is False
    assert store.job("job-1")["passed"] is None

    real = store.finish("job-1", "runner-1", exit_code=0, passed=3, failed=0,
                        skipped=1, duration=1.0, summary="")
    assert real is True
    assert store.job("job-1")["state"] == DONE


def test_a_nonzero_exit_records_a_failure(store: JobStore, make_job) -> None:
    store.enqueue(make_job())
    store.claim("runner-1", [], 60, 3)
    store.finish("job-1", "runner-1", exit_code=1, passed=2, failed=1,
                 skipped=0, duration=2.0, summary="webapp::test_x")

    assert store.job("job-1")["state"] == FAILED


def test_a_result_for_an_unclaimed_job_is_refused(store: JobStore, make_job) -> None:
    store.enqueue(make_job())

    assert store.finish("job-1", "runner-1", exit_code=0, passed=1, failed=0,
                        skipped=0, duration=1.0, summary="") is False


def test_started_moves_a_claimed_job_to_running(store: JobStore, make_job) -> None:
    store.enqueue(make_job())
    store.claim("runner-1", [], 60, 3)

    assert store.mark_running("job-1", "runner-1") is True
    assert store.job("job-1")["state"] == RUNNING
    assert store.mark_running("job-1", "runner-2") is False


# ── the two backends must refuse the same input ──────────────────────────────

def test_an_over_long_field_is_refused_by_both_backends(store: JobStore, make_job) -> None:
    """One rule, applied before either backend sees the value.

    Left to column types the two would drift — SQLite ignores `VARCHAR(n)` and
    Postgres enforces it, so the same value is stored on one and rejected on the
    other. Both schemas use TEXT so neither has an opinion, and the length lives
    in application code. This asserts the same refusal on every backend.
    """
    with pytest.raises(StoreError):
        store.enqueue(make_job(product="p" * 500))


def test_an_over_long_summary_is_truncated_rather_than_refused(
    store: JobStore, make_job
) -> None:
    """A run whose output is 4 KB still has a real result; losing it is worse."""
    store.enqueue(make_job())
    store.claim("runner-1", [], 60, 3)

    assert store.finish("job-1", "runner-1", exit_code=0, passed=1, failed=0,
                        skipped=0, duration=1.0, summary="x" * 9000) is True
    assert len(store.job("job-1")["summary"]) == 2000


# ── registry ─────────────────────────────────────────────────────────────────

def test_a_server_that_stops_heartbeating_is_reported_offline(store: JobStore) -> None:
    store.enrol("runner-1", "pubkey", [], now=1_000.0)

    assert store.runners(offline_after=90, now=1_050.0)[0]["state"] == "online"
    assert store.runners(offline_after=90, now=1_200.0)[0]["state"] == "offline"
    assert store.online(offline_after=90, now=1_200.0) == []


def test_re_enrolment_updates_the_key_and_last_seen(store: JobStore) -> None:
    """A restart is normal and must work without an operator touching anything."""
    store.enrol("runner-1", "key-a", ["dev"], now=1_000.0)
    store.enrol("runner-1", "key-b", ["staging"], now=2_000.0)

    row = store.runner("runner-1")
    assert row["public_key"] == "key-b"
    assert row["labels"] == "staging"
    assert len(store.runners(90, now=2_000.0)) == 1


def test_a_runner_row_reads_the_same_way_on_every_backend(store: JobStore) -> None:
    """`app.py` subscripts this row, so its keys are part of the contract."""
    store.enrol("runner-1", "key-a", ["dev", "staging"], now=1_000.0)
    row = store.runner("runner-1")

    assert set(row) == {"runner_id", "public_key", "labels", "enrolled_at", "last_seen"}
    assert row["labels"] == "dev,staging"


def test_an_unknown_runner_is_none_rather_than_an_error(store: JobStore) -> None:
    assert store.runner("never-enrolled") is None


# ── history ──────────────────────────────────────────────────────────────────

def test_recent_is_newest_first_and_bounded(store: JobStore, make_job) -> None:
    for n in range(5):
        store.enqueue(make_job(f"job-{n}", product=f"p{n}"), now=1_000.0 + n)

    recent = store.recent(limit=3)
    assert [r["id"] for r in recent] == ["job-4", "job-3", "job-2"]


def test_last_for_a_product_is_the_most_recent_one(store: JobStore, make_job) -> None:
    store.enqueue(make_job("old", server="dev"), now=1_000.0)
    store.enqueue(make_job("new", server="staging"), now=2_000.0)
    store.enqueue(make_job("other", product="billing"), now=3_000.0)

    assert store.last_for("webapp")["id"] == "new"
    assert store.last_for("nothing-like-this") is None


# ── job definitions (A2.2) ───────────────────────────────────────────────────
#
# A `Job` above is a RUN. A `JobDef` here is a DEFINITION that produces runs.
# The PRD calls both "job", which is why the tables are `jobs` and `job_defs`
# rather than two names a hurried reader would skim past.


def test_a_definition_round_trips_through_both_backends(
    store: JobStore, make_job_def
) -> None:
    """The positive control. Without it every refusal below is trivially true
    of a store that refuses to save anything at all."""
    assert store.save_job_def(make_job_def()) is SaveResult.CREATED

    got = store.job_def("jd-1")
    assert got is not None
    assert got.name == "alpha smoke"
    assert got.trigger == ("alpha", "smoke", "sandbox")
    assert got.action_kind == "gh-action"
    assert got.action_target == "alpha_smoke.yml"


def test_saving_the_same_id_again_updates_rather_than_duplicating(
    store: JobStore, make_job_def
) -> None:
    assert store.save_job_def(make_job_def()) is SaveResult.CREATED
    assert store.save_job_def(make_job_def(name="renamed")) is SaveResult.UPDATED
    assert store.count_job_defs() == 1
    assert store.job_def("jd-1").name == "renamed"


def test_created_at_survives_an_update(store: JobStore, make_job_def) -> None:
    """`updated_at` moves; `created_at` does not.

    An update that reset `created_at` would make "when was this job added"
    unanswerable, and the History surface is meant to answer exactly that.
    """
    store.save_job_def(make_job_def(), now=1000.0)
    store.save_job_def(make_job_def(name="renamed"), now=2000.0)
    got = store.job_def("jd-1")
    assert (got.created_at, got.updated_at) == (1000.0, 2000.0)


def test_two_definitions_cannot_claim_the_same_trigger(
    store: JobStore, make_job_def
) -> None:
    """A2.2.2: refused AT SAVE, not resolved at match time.

    Resolved at match time it becomes "whichever row the database returned
    first", which is a different job running on different days from the same
    command.
    """
    assert store.save_job_def(make_job_def(job_def_id="jd-1")) is SaveResult.CREATED
    assert store.save_job_def(
        make_job_def(job_def_id="jd-2", name="a different name")
    ) is SaveResult.DUPLICATE_TRIGGER
    # And the refusal left nothing behind.
    assert store.count_job_defs() == 1
    assert store.job_def("jd-2") is None


def test_a_definition_may_move_to_a_trigger_it_previously_shared(
    store: JobStore, make_job_def
) -> None:
    """Editing the OWNER of a trigger is not a duplicate.

    Without this, a job could never be renamed or re-pointed: the ON CONFLICT
    (id) path would collide with its own row and report a duplicate trigger,
    which reads as "somebody else has this" about yourself.
    """
    store.save_job_def(make_job_def())
    assert store.save_job_def(
        make_job_def(action_target="alpha_smoke_v2.yml")
    ) is SaveResult.UPDATED
    assert store.job_def("jd-1").action_target == "alpha_smoke_v2.yml"


def test_concurrent_saves_of_one_trigger_leave_exactly_one_row(
    store: JobStore, make_job_def
) -> None:
    """The reason the refusal is a UNIQUE CONSTRAINT and not a prior SELECT.

    Twelve threads, each saving a DIFFERENT id with the SAME trigger. A
    read-then-write check passes in all twelve before any of them inserts, so
    the table ends up with several rows claiming one trigger — the exact state
    A2.2.2 says must be impossible, discovered later at match time.

    The barrier is load-bearing: without it the threads start far enough apart
    that nothing overlaps, and this test passes against the very defect it
    exists to catch. That already happened once in this file, to the cap tests.
    """
    ready = threading.Barrier(12, timeout=30)
    results: list[SaveResult] = []
    errors: list[str] = []
    lock = threading.Lock()

    def save(n: int) -> None:
        ready.wait()
        try:
            outcome = store.save_job_def(make_job_def(job_def_id=f"jd-{n}",
                                                      name=f"racer {n}"))
        except Exception as exc:  # noqa: BLE001 - the point is that there are none
            with lock:
                errors.append(f"{type(exc).__name__}: {exc}")
            return
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=save, args=(n,)) for n in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"a concurrent save raised: {errors}"
    # THE COUNTS, both of them. "one created" alone is satisfied by a run where
    # eleven threads crashed, and "eleven refused" alone by a run where the
    # twelfth did too.
    assert len(results) == 12, f"only {len(results)} of 12 threads reported"
    created = [r for r in results if r is SaveResult.CREATED]
    assert len(created) == 1, f"{len(created)} threads created a row, expected 1"
    assert all(r is SaveResult.DUPLICATE_TRIGGER
               for r in results if r not in created), results
    assert store.count_job_defs() == 1


def test_a_trigger_matches_the_whole_tuple_and_nothing_less(
    store: JobStore, make_job_def
) -> None:
    """Exact, never partial. A definition matching two of three tokens is a
    DIFFERENT job, and returning it runs something nobody asked for."""
    store.save_job_def(make_job_def())

    assert store.job_def_for("alpha", "smoke", "sandbox").id == "jd-1"
    for wrong in (
        ("beta", "smoke", "sandbox"),
        ("alpha", "full", "sandbox"),
        ("alpha", "smoke", "staging"),
    ):
        assert store.job_def_for(*wrong) is None, wrong


def test_definitions_are_listed_in_a_stable_order(
    store: JobStore, make_job_def
) -> None:
    """Two listings of one store must agree, or the tab reorders on refresh."""
    store.save_job_def(make_job_def(job_def_id="c", name="charlie", product="gamma"))
    store.save_job_def(make_job_def(job_def_id="a", name="alfa", product="alpha"))
    store.save_job_def(make_job_def(job_def_id="b", name="bravo", product="beta"))
    assert [d.name for d in store.job_defs()] == ["alfa", "bravo", "charlie"]


def test_deleting_says_whether_anything_was_deleted(
    store: JobStore, make_job_def
) -> None:
    """"Deleted" over an id that never existed is a lie that reads as success."""
    store.save_job_def(make_job_def())
    assert store.delete_job_def("jd-1") is True
    assert store.delete_job_def("jd-1") is False
    assert store.count_job_defs() == 0


def test_a_deleted_trigger_is_free_again(store: JobStore, make_job_def) -> None:
    """Otherwise a deleted job blocks its own replacement forever."""
    store.save_job_def(make_job_def())
    store.delete_job_def("jd-1")
    assert store.save_job_def(
        make_job_def(job_def_id="jd-2", name="the replacement")
    ) is SaveResult.CREATED


def test_an_empty_store_reports_zero_rather_than_looking_the_same_as_a_full_one(
    store: JobStore,
) -> None:
    assert store.count_job_defs() == 0
    assert store.job_defs() == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("job_def_id", ""),
        ("name", "   "),
        ("product", ""),
        ("test_scope", ""),
        ("server", ""),
        ("action_kind", "curl"),
        ("action_target", ""),
    ],
)
def test_a_malformed_definition_is_refused_by_both_backends(
    store: JobStore, make_job_def, field: str, value: str
) -> None:
    """Shape only — whether `product` is ALLOWED is checked at execution (A2.3).

    Both backends refuse identically because `validate_job_def` is called by
    each backend's own `save_job_def` rather than by the caller, so there is no
    path into the table that skips it.
    """
    with pytest.raises(StoreError):
        store.save_job_def(make_job_def(**{field: value}))
    assert store.count_job_defs() == 0


# ── a dispatch that never happened (NEH-1156) ────────────────────────────────
#
# `not_dispatched` means the run NEVER STARTED. `failed` means it ran and did
# not pass. Sharing one state would make every reader — `results`, the tab's
# History — report the wrong one with complete confidence.


def test_a_dispatched_job_can_be_marked_never_started(store: JobStore, make_job) -> None:
    """The positive control, and the state the gh-action path actually uses.

    `record_dispatch` inserts at RUNNING, not QUEUED — there is no queue on the
    dispatch path. A guard written against QUEUED matches nothing and the whole
    call becomes a silent no-op, which is what the first version of this did.
    """
    store.record_dispatch(make_job("gh-1"), mode="gh-action")
    assert store.mark_not_dispatched("gh-1", "GitHub refused the run.") is True

    record = store.job("gh-1")
    assert record["state"] == NOT_DISPATCHED
    assert record["summary"] == "GitHub refused the run."
    assert record["finished_at"] is not None


def test_it_is_terminal_so_a_late_failure_cannot_overwrite_a_result(
    store: JobStore, make_job
) -> None:
    store.record_dispatch(make_job("gh-1"), mode="gh-action")
    store.mark_not_dispatched("gh-1", "first")
    assert store.mark_not_dispatched("gh-1", "second") is False
    assert store.job("gh-1")["summary"] == "first"


def test_a_job_a_RUNNER_holds_is_never_marked_never_started(
    store: JobStore, make_job
) -> None:
    """THE GUARD THAT MATTERS. A test-server job is RUNNING while a runner
    executes it, so a state-only check would let a stray dispatch failure
    overwrite somebody else's live work with a lie.

    `runner_id IS NULL` is what separates a dispatched job from a claimed one.
    """
    store.enqueue(make_job("q-1"))
    claimed = store.claim("runner-1", [], 60, 3)
    assert claimed is not None
    # RUNNING, not merely claimed. A claimed job is excluded by the STATE list
    # alone, so asserting against one proves nothing about `runner_id` — this
    # test passed against a build with the runner check deleted until planting
    # showed it. `mark_running` is what puts a job in the one state where the
    # two guards differ.
    assert store.mark_running("q-1", "runner-1") is True
    assert store.job("q-1")["state"] == RUNNING
    assert store.job("q-1")["runner_id"] == "runner-1"

    assert store.mark_not_dispatched("q-1", "should not apply") is False
    assert store.job("q-1")["state"] == RUNNING


def test_an_unknown_id_changes_nothing_and_says_so(store: JobStore) -> None:
    assert store.mark_not_dispatched("no-such-job", "why") is False


def test_it_frees_the_slot_the_job_was_holding(store: JobStore, make_job) -> None:
    """A never-started run must not occupy a cap forever.

    `not_dispatched` is terminal and therefore outside ACTIVE_STATES — so the
    next command for the same (product, server) is accepted rather than refused
    by a cap held by a run that will never happen.
    """
    caps = Caps(max_active_per_job=1)
    assert store.record_dispatch(make_job("gh-1"), mode="gh-action",
                                 caps=caps) is EnqueueResult.ACCEPTED
    # While it is live, the cap holds.
    assert store.record_dispatch(make_job("gh-2"), mode="gh-action",
                                 caps=caps) is EnqueueResult.JOB_AT_CAPACITY

    store.mark_not_dispatched("gh-1", "GitHub refused the run.")

    assert store.record_dispatch(make_job("gh-3"), mode="gh-action",
                                 caps=caps) is EnqueueResult.ACCEPTED


def test_the_reason_is_capped_like_every_other_stored_text(
    store: JobStore, make_job
) -> None:
    store.record_dispatch(make_job("gh-1"), mode="gh-action")
    store.mark_not_dispatched("gh-1", "x" * (MAX_SUMMARY * 3))
    assert len(store.job("gh-1")["summary"]) <= MAX_SUMMARY
