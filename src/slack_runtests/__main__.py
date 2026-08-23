"""`slack-runtests` — start the API server."""

from __future__ import annotations

import logging
import os
import sys

from .authz import refuse_or_warn
from .config import load
from .slack import announce_configuration


def main() -> int:
    import uvicorn

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(levelname)-8s %(name)s: %(message)s",
    )

    # Say up front whether results will actually reach Slack. This server runs
    # the tests itself (V1) and reports through SlackNotifier, so an operator
    # who starts it with no token gets a working test runner that posts
    # nowhere — and, without this line, no hint of that until the first run.
    cfg = load()
    log = logging.getLogger("slack-runtests")
    announce_configuration(log, cfg.default_channel)

    # BEFORE uvicorn.run, which blocks. A refusal printed after the server is
    # already serving is not a refusal — and this is the same ordering the
    # Slack announcement above is tested for.
    refusal = refuse_or_warn(
        log,
        signing_secret=cfg.signing_secret,
        allowed_team=cfg.allowed_team,
        allowed_channels=cfg.allowed_channels,
        allowed_users=cfg.allowed_users,
        # The app's lifespan does the announcing; it runs whichever way this
        # process was started, so warning here too would print it twice.
        warn=False,
    )
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return 2

    uvicorn.run(
        "slack_runtests.api:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8500")),
        reload=bool(os.environ.get("RELOAD")),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
