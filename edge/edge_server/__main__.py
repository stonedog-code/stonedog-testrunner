"""`slack-runtests-edge` — start the edge server."""

from __future__ import annotations

import logging
import os
import sys

from slack_runtests import identity
from slack_runtests.authz import refuse_or_warn

from .config import load


def _redacted(dsn: str) -> str:
    """A Postgres DSN with the password removed, for a log line.

    A startup line naming the store is worth having; a startup line naming the
    store's password is a credential in every log aggregator that ever reads it.
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(dsn)
    if not parts.password:
        return dsn
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    netloc = f"{parts.username}:***@{host}" if parts.username else host
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def main() -> int:
    import uvicorn

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(levelname)-8s %(name)s: %(message)s",
    )
    log = logging.getLogger("edge")
    cfg = load()

    # BEFORE ANYTHING TOUCHES THE DISK. `identity.load_or_create` GENERATES a
    # keypair when none exists, so running it first meant a process that was
    # about to refuse to start still wrote a private key — a side effect from a
    # run that was rejected, and a key an operator would later have to reason
    # about. A gate belongs above the side effects it is gating.
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

    # Print the edge's own fingerprint at startup. This is what an operator
    # compares against the value pinned in each test server's config, and it is
    # far easier to check a line of log output than to go and read a key file.
    key = identity.load_or_create(cfg.key_path)
    log.info("edge identity %s (key %s)", identity.fingerprint(identity.public_b64(key)), cfg.key_path)

    if cfg.enroll_token:
        log.warning("RUNNER_ENROLL_TOKEN is set — unknown test servers can self-enrol")
    if not cfg.admin_token:
        log.info("EDGE_ADMIN_TOKEN unset — /admin/fleet will 404")

    # WHAT WAS ASKED FOR. The app's lifespan logs `store ready: <backend>` once
    # it has actually opened one — and the difference between the two lines is
    # the point. This one said `store: postgres` for a DSN that could not be
    # reached at all, which is the green-over-an-empty-set shape: a line whose
    # only job is to distinguish a working Postgres from a silent fallback, and
    # which could not.
    log.info(
        "store configured: %s (%s)", cfg.store_backend,
        cfg.store_dsn if cfg.store_backend == "sqlite" else _redacted(cfg.store_dsn),
    )
    if cfg.store_backend == "sqlite":
        log.info(
            "sqlite serialises writers; a lock is waited on for %.0fs, then the "
            "caller is told the runner is busy", cfg.db_busy_timeout,
        )
    log.info(
        "caps: %s in flight per (product, server) · %s queued per channel · "
        "%s running per channel  (0 = unlimited)",
        cfg.max_active_per_job, cfg.max_queued_per_channel, cfg.max_running_per_channel,
    )

    uvicorn.run(
        "edge_server.app:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8500")),
        reload=bool(os.environ.get("RELOAD")),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
