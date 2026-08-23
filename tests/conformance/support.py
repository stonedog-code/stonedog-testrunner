"""Which backends this run covers, and which it is required to cover.

Kept out of `conftest.py` so the guard test can import it the same way the rest
of this repo's tests import `harness` — pytest puts a test directory on the path
when it has no `__init__.py`, and a relative import would not work there.
"""

from __future__ import annotations

import os

#: Set to a DSN to add Postgres to the parameter list. Absent means SQLite only.
DSN_ENV = "TESTRUNNER_TEST_POSTGRES_DSN"
#: Comma-separated backends that MUST be exercised, or the run fails.
REQUIRED_ENV = "TESTRUNNER_REQUIRED_BACKENDS"


def postgres_dsn() -> str:
    return os.environ.get(DSN_ENV, "").strip()


def available_backends() -> list[str]:
    return ["sqlite"] + (["postgres"] if postgres_dsn() else [])


def required_backends() -> list[str]:
    raw = os.environ.get(REQUIRED_ENV, "sqlite")
    return [x.strip() for x in raw.split(",") if x.strip()]
