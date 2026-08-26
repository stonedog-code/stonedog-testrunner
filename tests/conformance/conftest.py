"""One suite, every configured backend — and it says which ones those were.

WHY THIS IS NOT TWO SUITES

The two backends express the same guarantees in completely different SQL:
SQLite takes the write lock up front with `BEGIN IMMEDIATE`; Postgres steps over
locked rows with `FOR UPDATE SKIP LOCKED`. Written as two suites, each would be
tested against the implementation it was written from — and a backend that
silently fails to claim atomically is exactly what a single-backend suite cannot
see, because the property is invisible until two claimers meet.

So the assertions live once and the backend is a fixture parameter. Every test
below is written in terms of the interface and mentions neither backend.

HOW POSTGRES IS SELECTED, AND WHY IT CANNOT SILENTLY VANISH

`TESTRUNNER_TEST_POSTGRES_DSN` adds the Postgres parameter. Nothing else does —
a laptop with no database still runs the whole suite against SQLite, which is
the backend that laptop is going to use.

That leaves the failure this fleet keeps repeating: a suite that quietly stops
covering something and stays green. Two guards, both required:

  * `pytest_report_header` prints the backend list on every run, so the size of
    the input set is in the output of every run rather than inferable from it.
  * `TESTRUNNER_REQUIRED_BACKENDS` names the backends that MUST be present, and
    `test_required_backends.py` FAILS — never skips — when one is missing. CI
    sets it to `sqlite,postgres`, so a CI job whose Postgres service failed to
    start goes red instead of passing over half the code.
"""

from __future__ import annotations

import uuid

import pytest

from slack_runtests.store import Job, JobDef, JobStore, open_store
from support import DSN_ENV, available_backends, postgres_dsn, required_backends

#: The widest concurrency any test in this directory uses. The Postgres pool is
#: opened at this size so those tests genuinely race — see the fixture below.
CONCURRENCY = 12


def pytest_report_header(config: pytest.Config) -> list[str]:
    """The input-set size, in the header of every single run.

    `0 backends` and `2 backends` produce identical output once the tests pass;
    the count is the only thing that distinguishes a suite that covered
    everything from one that covered nothing.
    """
    available = available_backends()
    required = required_backends()
    lines = [
        f"store conformance: {len(available)} backend(s) — {', '.join(available)}",
        f"store conformance: required — {', '.join(required)}",
    ]
    if not postgres_dsn():
        lines.append(
            f"store conformance: postgres NOT exercised (set {DSN_ENV} to include it)"
        )
    return lines


@pytest.fixture(params=available_backends(), ids=available_backends())
def store(request: pytest.FixtureRequest, tmp_path) -> JobStore:
    """A brand-new, empty store on the parameterised backend.

    Postgres gets its own SCHEMA per test rather than its own database: creating
    a database per test is slow enough to change what people are willing to run,
    and a dropped schema leaves exactly as little behind.
    """
    if request.param == "sqlite":
        opened = open_store(str(tmp_path / "edge.db"), busy_timeout=2)
        yield opened
        opened.close()
        return

    import psycopg

    dsn = postgres_dsn()
    schema = f"conf_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    try:
        # `options=-c search_path=...` is how a connection is confined to one
        # schema without any statement in the store knowing it exists — which
        # is the point: the backend under test must be the real one, unmodified.
        sep = "&" if "?" in dsn else "?"
        # A pool at least as large as the widest concurrency test below. With
        # the default pool the threads queue for connections instead of racing,
        # and a planted defect — the claim path's serialisation deleted —
        # passed every concurrency test in this file.
        opened = open_store(
            f"{dsn}{sep}options=-c%20search_path%3D{schema}",
            busy_timeout=2, pool_size=CONCURRENCY,
        )
        try:
            yield opened
        finally:
            opened.close()
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.fixture
def make_job():
    def _make(job_id: str = "job-1", product: str = "webapp", server: str = "staging",
              channel: str = "#testing", job_def_id: str | None = None) -> Job:
        return Job(
            id=job_id, product=product, server=server, select_expr=None, marker=None,
            slack_channel=channel, slack_user="U1", job_def_id=job_def_id,
        )

    return _make


@pytest.fixture
def make_job_def():
    """A valid definition, varied by keyword.

    Fictional vocabulary, like the unit tests: a fixture naming real products
    would make it a claim about somebody's estate rather than about the store.
    """
    def _make(job_def_id: str = "jd-1", name: str = "alpha smoke",
              product: str = "alpha", test_scope: str = "smoke",
              server: str = "sandbox",
              action_kind: str = "gh-action",
              action_target: str = "alpha_smoke.yml",
              language: str = "python",
              description: str = "") -> JobDef:
        return JobDef(
            id=job_def_id, name=name, description=description, product=product,
            test_scope=test_scope, server=server,
            action_kind=action_kind, action_target=action_target,
            language=language,
        )

    return _make
