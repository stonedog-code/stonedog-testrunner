"""Start a real edge server, over a real socket — or use one that is already up.

WHY NOT `TestClient`

The unit tier already drives the app in-process with `TestClient`, and that is
the right tool there. Using it here too would produce tests that look like
integration tests and prove nothing new: no uvicorn, no socket, no environment
parsing, no ASGI server behaviour. These tests exist to check that the thing you
actually deploy answers correctly, so they speak HTTP to a process.

WHY "START IT IF IT IS NOT RUNNING" IS A TRAP, AND HOW IT IS AVOIDED

Spawning a server from a test is how suites end up flaky and how developers end
up with orphaned processes on port 8500. Three rules keep it honest:

  1. AN EPHEMERAL PORT, never a fixed one. Two suites, or a suite and a dev
     server, must not fight over 8500.
  2. WAIT FOR READY, with a deadline and a loud failure. A fixed `sleep` is the
     single most common cause of a flaky integration suite: it is either too
     short on a busy machine or wasted time on an idle one.
  3. ONLY KILL WHAT WE STARTED. `RUNTESTS_EDGE_URL` points at a server someone
     else is running — the fixture uses it and does not touch its lifecycle.
     Without this rule the suite eventually kills the dev server in terminal 1,
     which is a memorable way to lose an afternoon.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from harness import (
    TEST_CHANNEL_ID,
    TEST_PRODUCTS,
    TEST_SERVERS,
    TEST_SIGNING_SECRET,
    TEST_TEAM_ID,
    TEST_TEST_SCOPES,
    TEST_USER_ID,
    EdgeUnderTest,
)

READY_TIMEOUT = 25.0


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _healthy(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/healthz", timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _wait_ready(url: str, process: subprocess.Popen, deadline: float) -> None:
    """Poll until the server answers, or fail with something diagnosable.

    If the child has already exited, say so and show its output. A readiness
    loop that only ever reports "timed out" hides the actual error — a bad
    import, a port already bound — behind a generic message.
    """
    while time.monotonic() < deadline:
        if process.poll() is not None:
            out = (process.stdout.read() if process.stdout else "") or ""
            raise RuntimeError(
                f"edge server exited with code {process.returncode} before becoming ready:\n{out[-4000:]}"
            )
        if _healthy(url):
            return
        time.sleep(0.15)
    raise RuntimeError(f"edge server at {url} did not become ready within {READY_TIMEOUT}s")


@pytest.fixture(scope="session")
def edge(tmp_path_factory: pytest.TempPathFactory):
    """A running edge server, with known Slack credentials.

    Set `RUNTESTS_EDGE_URL` (and `SLACK_SIGNING_SECRET` to match) to run these
    against a server you started yourself.
    """
    external = os.environ.get("RUNTESTS_EDGE_URL", "").strip().rstrip("/")
    if external:
        if not _healthy(external):
            pytest.fail(f"RUNTESTS_EDGE_URL={external} is set but /healthz does not answer")
        secret = os.environ.get("SLACK_SIGNING_SECRET", "")
        if not secret:
            pytest.fail(
                "RUNTESTS_EDGE_URL is set, so this suite cannot know how to sign a "
                "request. Set SLACK_SIGNING_SECRET to the same value that server uses."
            )
        # Not ours: use it, never terminate it. `trusted_keys_dir` stays None
        # because we do not know where that server keeps its keys — the tests
        # that need to enrol a test server skip rather than guess.
        yield EdgeUnderTest(url=external, signing_secret=secret, managed=False)
        return

    workdir: Path = tmp_path_factory.mktemp("edge")
    port = _free_port()
    url = f"http://127.0.0.1:{port}"

    trusted_keys_dir = workdir / "trusted_runners"

    env = {
        **os.environ,
        "SLACK_SIGNING_SECRET": TEST_SIGNING_SECRET,
        "SLACK_TEAM_ID": TEST_TEAM_ID,
        "RUNTESTS_CHANNELS": TEST_CHANNEL_ID,
        "RUNTESTS_USERS": TEST_USER_ID,
        "RUNTESTS_PRODUCTS": TEST_PRODUCTS,
        "RUNTESTS_SERVERS": TEST_SERVERS,
        "RUNTESTS_TEST_SCOPES": TEST_TEST_SCOPES,
        "EDGE_DB_PATH": str(workdir / "edge.db"),
        "EDGE_KEY_PATH": str(workdir / "edge_ed25519.pem"),
        "EDGE_TRUSTED_KEYS_DIR": str(trusted_keys_dir),
        # No bootstrap token and no admin token, deliberately. The runner-door
        # tests enrol through the PRE-AUTHORISED key path — an operator drops a
        # `.pub` file in `EDGE_TRUSTED_KEYS_DIR` — which is what production
        # does. Enabling the lab bootstrap token here would test a
        # configuration nobody deploys, and would hide a regression that made
        # the token the only working route.
        "RUNNER_ENROLL_TOKEN": "",
        "EDGE_ADMIN_TOKEN": "",
        # The claim endpoint is a LONG POLL: with nothing to hand out it holds
        # the request open for `EDGE_POLL_TIMEOUT` seconds before answering 204.
        # The 25s default is right for a deployment (under a proxy's idle
        # timeout) and wrong for a suite, where "nothing left to dispatch" is an
        # assertion we make on purpose and would otherwise pay 25s for.
        "EDGE_POLL_TIMEOUT": "1",
        "LOG_LEVEL": "WARNING",
    }
    env.pop("SLACK_BOT_TOKEN", None)

    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "edge_server.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        # Its own process group, so terminating it cannot reach back into the
        # pytest process, and so any child it spawned goes with it.
        start_new_session=True,
    )

    try:
        _wait_ready(url, process, time.monotonic() + READY_TIMEOUT)
        yield EdgeUnderTest(
            url=url,
            signing_secret=TEST_SIGNING_SECRET,
            managed=True,
            trusted_keys_dir=trusted_keys_dir,
        )
    finally:
        if process.poll() is None:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                process.wait(timeout=5)
