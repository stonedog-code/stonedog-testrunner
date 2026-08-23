"""The guard against this suite quietly covering half of what it claims to.

A conformance suite that runs against one backend is not a conformance suite,
and nothing about its output says so: the same tests pass, the same green
appears, and the backend that was never exercised is the one in production.

So the set of backends that MUST run is declared in the environment, and a
missing one is a FAILURE — never a skip. A skip here would be a green result
over an empty set with a polite note attached.
"""

from __future__ import annotations

import pytest

from support import DSN_ENV, available_backends, required_backends

pytestmark = pytest.mark.unit


def test_every_required_backend_is_actually_exercised() -> None:
    available = available_backends()
    missing = [b for b in required_backends() if b not in available]
    assert not missing, (
        f"required backend(s) {missing} were not exercised — available: {available}. "
        f"Set {DSN_ENV} to a reachable Postgres, or correct TESTRUNNER_REQUIRED_BACKENDS."
    )


def test_sqlite_is_always_required() -> None:
    """The default backend can never be configured out of the suite."""
    assert "sqlite" in required_backends()
