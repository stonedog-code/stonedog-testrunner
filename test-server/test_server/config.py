"""Every environment variable a test server reads, in one place."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field


def _csv(name: str) -> frozenset[str]:
    raw = os.environ.get(name, "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _default_runner_id() -> str:
    """The hostname, sanitised to the character set the edge accepts.

    A default that is stable across restarts matters: the runner id is what a
    job's result ownership is checked against, so a server that comes back with
    a new id has abandoned whatever it was holding.
    """
    raw = os.environ.get("RUNNER_ID") or socket.gethostname() or "runner"
    return "".join(c if (c.isalnum() or c in "-_") else "-" for c in raw)[:64]


@dataclass(slots=True)
class RunnerConfig:
    edge_url: str = field(default_factory=lambda: os.environ.get("EDGE_URL", "http://127.0.0.1:8500").rstrip("/"))
    runner_id: str = field(default_factory=_default_runner_id)
    key_path: str = field(default_factory=lambda: os.environ.get("RUNNER_KEY_PATH", "keys/runner_ed25519.pem"))

    #: Which environments this server will accept jobs for. EMPTY MEANS ANY,
    #: which is the shared-pool arrangement the three-server harness uses to
    #: prove queueing. Set it when a particular box is the only one that can
    #: reach a particular environment.
    labels: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            x.strip() for x in os.environ.get("RUNNER_LABELS", "").split(",") if x.strip()
        )
    )

    # ── the trigger allowlists, read from THIS machine's environment ─────────
    #
    # Deliberately not taken from the job, and deliberately not fetched from the
    # edge. `agent.validate()` exists because a signature proves WHO sent a job,
    # not that its contents are sane -- so if the allowlist arrived with the job
    # or over the wire from the edge, a compromised edge would simply send a
    # wider one and the re-check would agree with it.
    #
    # Same variable names as the edge, because a command must mean the same
    # thing wherever it is authorised. In a deployment where the two disagree,
    # the runner's is the one that decides what actually executes -- which is
    # the correct way round.
    allowed_products: frozenset[str] = field(
        default_factory=lambda: _csv("RUNTESTS_PRODUCTS")
    )
    allowed_servers: frozenset[str] = field(
        default_factory=lambda: _csv("RUNTESTS_SERVERS")
    )
    allowed_test_scopes: frozenset[str] = field(
        default_factory=lambda: _csv("RUNTESTS_TEST_SCOPES")
    )

    #: One-time bootstrap token. Only consulted the first time this id enrols;
    #: after that the edge knows the key and the token is irrelevant.
    enroll_token: str = field(default_factory=lambda: os.environ.get("RUNNER_ENROLL_TOKEN", ""))

    #: The edge's public key fingerprint, pinned. Leave unset for trust-on-
    #: first-use (fine in a lab); set it in production so a substituted edge is
    #: refused rather than obeyed. This is the check that makes the reply
    #: signature worth having.
    edge_fingerprint: str = field(default_factory=lambda: os.environ.get("EDGE_FINGERPRINT", "").strip())

    # ── running the suite ────────────────────────────────────────────────────
    workdir: str = field(default_factory=lambda: os.environ.get("RUNNER_WORKDIR", "."))
    suite_root: str = field(default_factory=lambda: os.environ.get("RUNTESTS_SUITE_ROOT", "tests/sample"))
    job_timeout: float = field(default_factory=lambda: _num("RUNNER_JOB_TIMEOUT", 15 * 60))

    # ── talking to the edge ──────────────────────────────────────────────────
    heartbeat_interval: float = field(default_factory=lambda: _num("RUNNER_HEARTBEAT_INTERVAL", 30))
    #: Retry backoff when the edge is unreachable. A test server must survive
    #: the edge restarting without an operator touching it — otherwise every
    #: edge deploy is a fleet outage.
    retry_seconds: float = field(default_factory=lambda: _num("RUNNER_RETRY_SECONDS", 5))
    max_retry_seconds: float = field(default_factory=lambda: _num("RUNNER_MAX_RETRY_SECONDS", 60))


def load() -> RunnerConfig:
    return RunnerConfig()
