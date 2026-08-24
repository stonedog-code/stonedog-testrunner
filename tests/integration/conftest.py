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
from slack_runtests.store import JobDef, open_store
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

#: The one (product, server) pair the fixture deliberately does NOT define a job
#: for. Both tokens are on the allowlist, so a command naming them passes the
#: grammar and fails resolution -- which is the only way to reach the no-match
#: refusal and its suggestion.
UNSEEDED_PRODUCT = "catalog"
UNSEEDED_SERVER = "local"

#: The one job whose action is `gh-action` rather than `test-server`. Its
#: (product, server) must collide with NOTHING -- a trigger is unique, and it
#: must not take the unseeded pair either, or the no-match case disappears.
#: So the loop skips two pairs and this claims the second.
#: TWO of them, one per test, because RUNTESTS_MAX_ACTIVE_PER_JOB is 1 per
#: (product, server): a test that dispatches leaves that pair "running", so a
#: second test sharing it is refused by the CAP and never reaches the behaviour
#: it meant to check. Found exactly that way.
GH_PAIRS = (("billing", "local"), ("webapp", "local"))


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
        # The per-channel RUNNING cap is raised for this tier only.
        #
        # It defaults to 3, and these tests share one allowlisted channel and one
        # session-scoped edge — so dispatched runs accumulate and later door
        # tests get refused by the cap before reaching the behaviour they are
        # about. That is the cap working correctly on a fixture that never
        # finishes its jobs.
        #
        # Safe to raise HERE because the caps have their own coverage in the
        # conformance tier, where they are asserted against both backends with a
        # barrier so the threads genuinely race. This tier is about the doors.
        "RUNTESTS_MAX_RUNNING_PER_CHANNEL": "100",
        "RUNTESTS_MAX_QUEUED_PER_CHANNEL": "100",
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

        # A command must MATCH A JOB DEFINITION to run anything (A2.2.2), so
        # these tests seed one. Written after the edge is up, on purpose: the
        # edge creates the schema at startup, and seeding first would race it.
        #
        # `test-server` rather than `gh-action`, because this tier is about the
        # QUEUE and the runner door. The gh-action path dispatches to GitHub and
        # is covered by unit tests with the HTTP call faked -- an integration
        # test of it would either hit the real API or prove nothing.
        # The CROSS PRODUCT of the allowlists, because a trigger is the whole
        # tuple and these tests vary the server. Seeding only one server made
        # them fail with the near-miss suggestion -- correctly, which is how the
        # message earned its keep before any test asserted it.
        seed = open_store(str(workdir / "edge.db"))
        try:
            for product in TEST_PRODUCTS.split(","):
                for server in TEST_SERVERS.split(","):
                    for scope in TEST_TEST_SCOPES.split(","):
                        # ONE DELIBERATE HOLE. Seeding every combination leaves
                        # the no-match path unreachable, so the refusal and its
                        # near-miss suggestion would never be exercised -- a
                        # green suite over a branch no test can enter.
                        #
                        # (catalog, local) is allowlisted and undefined, so a
                        # command naming it is refused by RESOLUTION rather than
                        # by the grammar, which is the distinction being tested.
                        if (product, server) == (UNSEEDED_PRODUCT, UNSEEDED_SERVER) \
                                or (product, server) in GH_PAIRS:
                            continue
                        seed.save_job_def(JobDef(
                            id=f"jd-{product}-{server}-{scope}",
                            name=f"{product} {scope} on {server}",
                            product=product, test_scope=scope, server=server,
                            action_kind="test-server", action_target="any",
                        ))
            # ONE gh-action job, so the dispatch path is reachable. With no
            # GITHUB_TOKEN set the dispatch is a dry run that makes no HTTP
            # request, which is what makes this safe to exercise in a test.
            for n, (product, server) in enumerate(GH_PAIRS, start=1):
                seed.save_job_def(JobDef(
                    id=f"jd-gh-{n}", name=f"gh dispatch {n}",
                    product=product, test_scope="smoke", server=server,
                    action_kind="gh-action", action_target="runtests.yml",
                ))
        finally:
            seed.close()

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
