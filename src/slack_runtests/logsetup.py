"""Make sure the startup lines are actually emitted, however this was launched.

THE PROBLEM, WHICH IS NOT OBVIOUS

`uvicorn module:app --log-level info` configures **uvicorn's own loggers** and
nothing else. An application logger with no handler falls back to Python's
`lastResort`, which emits WARNING and above — so every INFO line this app writes
at startup disappears. Not to a file, not at a lower level: nowhere.

That is worse than untidy here. The lines it silences are `store ready:` and
`store configured:`, whose entire purpose is to distinguish a working Postgres
from a silent fallback to a file that a redeploy will delete. A diagnostic that
vanishes in the launcher people actually type is a diagnostic that does not
exist, and its absence looks exactly like everything being fine.

`main()` has always called `basicConfig` and so has never had this problem —
which is precisely why it went unnoticed: the images run `main()`, and the bare
uvicorn launch is what a person types while developing, or copies into a
Dockerfile of their own.

WHY THIS IS SAFE

It configures logging only when NOTHING has configured it — no root handlers at
all. An operator who has set up their own logging, and `main()`, both leave the
root logger with handlers, so this does nothing at all in either case. It never
changes a level that somebody chose.
"""

from __future__ import annotations

import logging
import os


def needs_configuring(root: logging.Logger | None = None) -> bool:
    """Has nobody configured logging yet?

    Split out from the action because the action is `logging.basicConfig`, which
    always targets the real root logger — and pytest's own `caplog` plugin
    attaches a handler to that root during every test, so a unit test cannot
    construct the bare state this is about. The decision is pure and testable;
    that it is acted on correctly is covered end-to-end by the integration tier,
    which launches a real `uvicorn module:app` and reads what it printed.
    """
    return not (root or logging.getLogger()).handlers


def ensure_configured(default_level: str | None = None) -> bool:
    """Configure root logging if nobody else has. Returns True if it acted."""
    if not needs_configuring():
        return False

    logging.basicConfig(
        level=default_level or os.environ.get("LOG_LEVEL", "INFO"),
        format="%(levelname)-8s %(name)s: %(message)s",
    )
    return True


__all__ = ["ensure_configured", "needs_configuring"]
