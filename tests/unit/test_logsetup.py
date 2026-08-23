"""Logging is configured when nobody else did it, and never otherwise.

The line this protects is `store ready:`, which distinguishes a working Postgres
from a silent fallback to a file a redeploy will delete. Under a bare
`uvicorn module:app` that line went nowhere at all — an application logger with
no handler falls back to `lastResort`, which emits WARNING and above. A
diagnostic that vanishes in the launcher people actually type is a diagnostic
that does not exist, and its absence looks exactly like everything being fine.

Only the DECISION is unit-tested here. The action is `logging.basicConfig`,
which always targets the real root logger, and pytest's own `caplog` plugin
attaches a handler to that root during every test — so the bare state this is
about cannot be constructed in a unit test, and a test that pretended to would
be asserting the opposite of what it claimed. That it is acted on correctly is
covered in `tests/integration/test_startup_gate.py`, which launches a real
`uvicorn module:app` with nothing configured and reads what it printed.
"""

from __future__ import annotations

import logging

import pytest

from slack_runtests.logsetup import needs_configuring

pytestmark = pytest.mark.unit


def test_a_logger_with_no_handlers_needs_configuring() -> None:
    assert needs_configuring(logging.getLogger("slack_runtests.test.bare")) is True


def test_a_logger_that_already_has_one_does_not() -> None:
    """The other direction, and the one that makes this safe to call at startup.

    An operator who configured their own logging — and `main()`, which calls
    basicConfig itself — must not have a level or a format taken from them by a
    helper meant only for the case where nobody did.
    """
    configured = logging.getLogger("slack_runtests.test.configured")
    handler = logging.StreamHandler()
    configured.addHandler(handler)
    try:
        assert needs_configuring(configured) is False
    finally:
        configured.removeHandler(handler)


def test_the_real_root_is_the_default_subject() -> None:
    """Called with nothing, it asks about the root logger — which is what
    `basicConfig` would configure. A version that asked about some other logger
    would answer correctly and act on the wrong thing."""
    root = logging.getLogger()
    assert needs_configuring() == needs_configuring(root)
