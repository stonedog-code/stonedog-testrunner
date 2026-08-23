"""Configuration, read from the environment in exactly one place."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .slack import DEFAULT_CHANNEL
from .store import Caps, backend_for


def _csv(name: str, default: str = "") -> frozenset[str]:
    raw = os.environ.get(name, default)
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass(slots=True)
class Config:
    #: "local" (V1 — run pytest here) or "github" (V2 — dispatch a workflow).
    mode: str = field(default_factory=lambda: os.environ.get("RUNTESTS_MODE", "local"))

    signing_secret: str = field(
        default_factory=lambda: os.environ.get("SLACK_SIGNING_SECRET", "")
    )
    default_channel: str = field(
        default_factory=lambda: os.environ.get("SLACK_DEFAULT_CHANNEL", DEFAULT_CHANNEL)
    )

    # ── authorisation allowlists ─────────────────────────────────────────────
    # All three are separate checks and all three are needed. The signature
    # proves the request came from Slack; only `team_id` proves it came from
    # YOUR Slack; and workspace membership is not an entitlement, so channel and
    # user are checked as well. On any real team the workspace includes guests,
    # contractors and Slack Connect users from a customer.
    allowed_team: str = field(default_factory=lambda: os.environ.get("SLACK_TEAM_ID", ""))
    allowed_channels: frozenset[str] = field(
        default_factory=lambda: _csv("RUNTESTS_CHANNELS")
    )
    allowed_users: frozenset[str] = field(default_factory=lambda: _csv("RUNTESTS_USERS"))

    # ── V1: local execution ──────────────────────────────────────────────────
    suite_root: str = field(
        default_factory=lambda: os.environ.get("RUNTESTS_SUITE_ROOT", "tests/sample")
    )

    # ── V2: GitHub Actions dispatch ──────────────────────────────────────────
    github_repo: str = field(default_factory=lambda: os.environ.get("GITHUB_REPO", ""))
    github_workflow: str = field(
        default_factory=lambda: os.environ.get("GITHUB_WORKFLOW_FILE", "runtests.yml")
    )
    github_ref: str = field(default_factory=lambda: os.environ.get("GITHUB_REF_NAME", "main"))
    github_token: str = field(default_factory=lambda: os.environ.get("GITHUB_TOKEN", ""))

    # ── the store ────────────────────────────────────────────────────────────
    # V1 and V2 record what they dispatched so a Slack retry cannot start a
    # second run, and so `results` can answer after a restart. That record used
    # to be a module-level dict, which is per-worker: two uvicorn workers each
    # got their own and the idempotency guarantee quietly disappeared. It is a
    # store now, with the same SQLite default and the same DSN escape hatch as
    # the edge.
    db_path: str = field(
        default_factory=lambda: os.environ.get("RUNTESTS_DB_PATH", "data/runtests.db")
    )
    db_dsn: str = field(default_factory=lambda: os.environ.get("RUNTESTS_DB_DSN", ""))
    db_busy_timeout: float = field(
        default_factory=lambda: _num("RUNTESTS_DB_BUSY_TIMEOUT", 5)
    )

    # ── concurrency caps ─────────────────────────────────────────────────────
    max_active_per_job: int = field(
        default_factory=lambda: int(_num("RUNTESTS_MAX_ACTIVE_PER_JOB", 1))
    )
    max_queued_per_channel: int = field(
        default_factory=lambda: int(_num("RUNTESTS_MAX_QUEUED_PER_CHANNEL", 10))
    )
    max_running_per_channel: int = field(
        default_factory=lambda: int(_num("RUNTESTS_MAX_RUNNING_PER_CHANNEL", 3))
    )

    @property
    def store_dsn(self) -> str:
        """What `open_store` is given. The DSN wins; otherwise it is the path."""
        return self.db_dsn.strip() or self.db_path

    @property
    def store_backend(self) -> str:
        return backend_for(self.store_dsn)

    @property
    def caps(self) -> Caps:
        return Caps(
            max_active_per_job=self.max_active_per_job,
            max_queued_per_channel=self.max_queued_per_channel,
            max_running_per_channel=self.max_running_per_channel,
        )

    #: Development affordance, and the one place this prototype knowingly differs
    #: from the page it implements. With no signing secret configured, signature
    #: verification is SKIPPED and every request is logged as unverified. That is
    #: how `test.sh` works out of the box. It is refused whenever a secret IS
    #: set, so the insecure path cannot survive into a configured deployment —
    #: but a real service should not have this at all.
    @property
    def verify_signatures(self) -> bool:
        return bool(self.signing_secret)


def load() -> Config:
    return Config()
