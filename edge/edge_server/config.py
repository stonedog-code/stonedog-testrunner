"""Every environment variable the edge reads, in one place."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from slack_runtests.parsing import Grammar

from slack_runtests.slack import DEFAULT_CHANNEL
from slack_runtests.store import Caps, backend_for


def _csv(name: str, default: str = "") -> frozenset[str]:
    raw = os.environ.get(name, default)
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass(slots=True)
class EdgeConfig:
    # ── Slack side (the public door) ─────────────────────────────────────────
    signing_secret: str = field(default_factory=lambda: os.environ.get("SLACK_SIGNING_SECRET", ""))
    default_channel: str = field(
        default_factory=lambda: os.environ.get("SLACK_DEFAULT_CHANNEL", DEFAULT_CHANNEL)
    )
    allowed_team: str = field(default_factory=lambda: os.environ.get("SLACK_TEAM_ID", ""))
    allowed_channels: frozenset[str] = field(default_factory=lambda: _csv("RUNTESTS_CHANNELS"))
    allowed_users: frozenset[str] = field(default_factory=lambda: _csv("RUNTESTS_USERS"))

    # The trigger allowlists (PRD §4.1, A2.10). Read from the same variables as
    # the V1 server, because a command must mean the same thing whichever
    # process authorises it -- two deployments of one product disagreeing about
    # what `--product` may be is a boundary with a hole in it.
    allowed_products: frozenset[str] = field(default_factory=lambda: _csv("RUNTESTS_PRODUCTS"))
    allowed_servers: frozenset[str] = field(default_factory=lambda: _csv("RUNTESTS_SERVERS"))
    allowed_test_scopes: frozenset[str] = field(default_factory=lambda: _csv("RUNTESTS_TEST_SCOPES"))

    def grammar(self) -> "Grammar":
        return Grammar.of(self.allowed_products, self.allowed_servers, self.allowed_test_scopes)

    # ── the store ────────────────────────────────────────────────────────────
    #: The default, and the reason a standalone runner needs no database: a
    #: file. Still honoured exactly as before — an upgrade that hard-crashed on
    #: the environment somebody already has would not be an upgrade.
    db_path: str = field(default_factory=lambda: os.environ.get("EDGE_DB_PATH", "data/edge.db"))
    #: The only way to select Postgres. Empty means SQLite, always. Set it and
    #: it wins over `db_path`, which is what the embedded deployment does —
    #: Lightsail containers have no persistent volume, so a file store there is
    #: deleted on every redeploy along with the queue and the history.
    db_dsn: str = field(default_factory=lambda: os.environ.get("EDGE_DB_DSN", ""))
    #: How long a write waits for a lock before the caller is told the runner is
    #: busy. Bounded well inside Slack's three-second budget on purpose: a
    #: handler that waits longer than Slack does produces a retry, and the retry
    #: is what the idempotency key exists to absorb.
    db_busy_timeout: float = field(default_factory=lambda: _num("EDGE_DB_BUSY_TIMEOUT", 5))
    key_path: str = field(
        default_factory=lambda: os.environ.get("EDGE_KEY_PATH", "keys/edge_ed25519.pem")
    )
    #: Directory of pre-authorised public keys, one file per test server named
    #: `<runner_id>.pub`. This is the production enrolment path: an operator
    #: puts the key there and nothing else is needed or accepted.
    trusted_keys_dir: str = field(
        default_factory=lambda: os.environ.get("EDGE_TRUSTED_KEYS_DIR", "trusted_runners")
    )
    #: A shared bootstrap token that lets an UNKNOWN test server enrol itself.
    #: Convenient in a lab, wrong in production — so it is empty by default and
    #: the edge says so at startup when it is set.
    enroll_token: str = field(default_factory=lambda: os.environ.get("RUNNER_ENROLL_TOKEN", ""))
    #: An operator-only view of the fleet. Empty means the endpoint 404s rather
    #: than answering — default deny, because "which machines exist and when
    #: were they last seen" is internal detail on a public surface.
    admin_token: str = field(default_factory=lambda: os.environ.get("EDGE_ADMIN_TOKEN", ""))

    # ── timings ──────────────────────────────────────────────────────────────
    #: How often a test server must check in. Four of these fit inside one
    #: lease, so a live server renews well before it could be declared dead.
    heartbeat_interval: float = field(default_factory=lambda: _num("RUNNER_HEARTBEAT_INTERVAL", 30))
    offline_after: float = field(default_factory=lambda: _num("RUNNER_OFFLINE_AFTER", 90))
    #: How long the edge holds a claim request open with nothing to give. Kept
    #: under the 30s that most proxies use as an idle timeout — a long-poll
    #: that outlives the proxy is a 504 the test server reads as an outage.
    poll_timeout: float = field(default_factory=lambda: _num("EDGE_POLL_TIMEOUT", 25))
    lease_seconds: float = field(default_factory=lambda: _num("JOB_LEASE_SECONDS", 120))
    max_attempts: int = field(default_factory=lambda: int(_num("JOB_MAX_ATTEMPTS", 2)))

    # ── concurrency caps ─────────────────────────────────────────────────────
    # Three caps doing three different jobs; zero disables any of them. See
    # `slack_runtests.store.Caps` for why they are counted inside the store's
    # own transactions rather than in a handler.
    #: How many runs of the same (product, server) may be in flight at once.
    #: One by default: typing the same command twice while the first is still
    #: going is a mistake far more often than it is a request.
    max_active_per_job: int = field(
        default_factory=lambda: int(_num("RUNTESTS_MAX_ACTIVE_PER_JOB", 1))
    )
    #: The "a chat box cannot queue fifty runs" cap, literally.
    max_queued_per_channel: int = field(
        default_factory=lambda: int(_num("RUNTESTS_MAX_QUEUED_PER_CHANNEL", 10))
    )
    #: How many of one channel's runs may occupy test servers simultaneously,
    #: so one busy channel cannot starve every other one.
    max_running_per_channel: int = field(
        default_factory=lambda: int(_num("RUNTESTS_MAX_RUNNING_PER_CHANNEL", 3))
    )

    @property
    def store_dsn(self) -> str:
        """What `open_store` is given. The DSN wins; otherwise it is the path."""
        return self.db_dsn.strip() or self.db_path

    @property
    def store_backend(self) -> str:
        """Which backend the configuration selects, without opening it."""
        return backend_for(self.store_dsn)

    @property
    def caps(self) -> Caps:
        return Caps(
            max_active_per_job=self.max_active_per_job,
            max_queued_per_channel=self.max_queued_per_channel,
            max_running_per_channel=self.max_running_per_channel,
        )

    @property
    def verify_signatures(self) -> bool:
        return bool(self.signing_secret)


def load() -> EdgeConfig:
    return EdgeConfig()
