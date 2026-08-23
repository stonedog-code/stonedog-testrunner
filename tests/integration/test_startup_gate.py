"""The startup gate under a REAL launcher, because that is where it was absent.

`tests/unit/test_authz_startup.py` proves the decision and
`tests/unit/test_entrypoints.py` proves each `main()` consults it. Neither can
prove the thing that actually matters here: **the gate must hold however the
process is started.**

It originally lived only in `__main__`, and `uvicorn edge_server.app:app` — the
most ordinary way there is to start a FastAPI process, and what most Docker
images do — went straight around it. The tell was that this repo's own
integration tier launches the edge exactly that way and did not notice the gate
existed at all.

So these spawn a real `python -m uvicorn` against the real app, with a real
environment, and read what the process did.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

pytestmark = pytest.mark.integration

#: Everything the gate requires. Stripped for the refusal cases and restored for
#: the control — because a gate that refuses every start is not a gate, and
#: nothing in the refusal cases alone could tell the difference.
PROTECTIONS = ("SLACK_SIGNING_SECRET", "SLACK_TEAM_ID", "RUNTESTS_CHANNELS",
               "RUNTESTS_USERS", "RUNTESTS_INSECURE_DEV")

APPS = [
    pytest.param("edge_server.app:app", id="edge"),
    pytest.param("slack_runtests.api:app", id="v1-api"),
]

#: uvicorn's own words for "the lifespan ran and the socket is open". Waiting
#: for this rather than for a fixed sleep is what keeps the control honest: a
#: process that never started would otherwise look the same as one that did.
STARTED = "Application startup complete"
REFUSED = "Application startup failed"


def _settle(process: subprocess.Popen, reader: threading.Thread, *,
            expect_exit: bool) -> None:
    """Stop a launched process, distinguishing "it should have exited" from
    "it is meant to keep running".

    A refusal is only a few milliseconds behind the message that announces it,
    so a harness that terminates the moment the text appears records SIGTERM
    (143) instead of the process's own exit code — a flake in the test, and one
    that would have been read as a flake in the product.
    """
    if expect_exit:
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            pass
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:  # pragma: no cover - a wedged uvicorn
        process.kill()
        process.wait(timeout=15)
    reader.join(timeout=5)


def _launch(app: str, overrides: dict[str, str], tmp_path, *, until: str,
            expect_exit: bool = False, deadline: float = 60.0) -> str:
    """Start a real uvicorn, read until `until` appears or it exits, then stop it.

    Terminated by its own handle, never by matching a pattern: `pkill -f uvicorn`
    from a test would also match the pytest process that spawned it.
    """
    env = {k: v for k, v in os.environ.items() if k not in PROTECTIONS}
    env["EDGE_DB_PATH"] = str(tmp_path / "edge.db")
    env["RUNTESTS_DB_PATH"] = str(tmp_path / "runtests.db")
    env["EDGE_KEY_PATH"] = str(tmp_path / "edge.pem")
    env["EDGE_TRUSTED_KEYS_DIR"] = str(tmp_path / "trusted")
    env.update(overrides)

    process = subprocess.Popen(
        # NO --log-level here, deliberately. `uvicorn --log-level info`
        # configures uvicorn's OWN loggers and nothing else, so it does not fix
        # this and would only hide that it does not: an application logger with
        # no handler falls back to `lastResort`, which emits WARNING and above,
        # so every INFO line goes nowhere. `logsetup.ensure_configured()` in the
        # app's lifespan is what makes them appear, and this launch is exactly
        # the case it exists for.
        [sys.executable, "-m", "uvicorn", app, "--host", "127.0.0.1", "--port", "0"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        start_new_session=True,
    )
    chunks: list[str] = []
    reader = threading.Thread(target=lambda: chunks.extend(process.stdout), daemon=True)
    reader.start()
    try:
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            if until in "".join(chunks) or process.poll() is not None:
                break
            time.sleep(0.05)
    finally:
        _settle(process, reader, expect_exit=expect_exit)
    return "".join(chunks)


@pytest.mark.parametrize("app", APPS)
def test_a_bare_uvicorn_launch_cannot_go_around_the_gate(app: str, tmp_path) -> None:
    output = _launch(app, {}, tmp_path, until=REFUSED, expect_exit=True)

    assert "REFUSING TO START" in output, output[-2000:]
    assert REFUSED in output, output[-2000:]
    assert STARTED not in output, "it must not have opened a socket first"
    # Every missing protection is named. A refusal saying only "misconfigured"
    # costs an operator three restarts to learn three things.
    for name in ("SLACK_SIGNING_SECRET", "SLACK_TEAM_ID", "RUNTESTS_CHANNELS"):
        assert name in output


@pytest.mark.parametrize("app", APPS)
def test_a_configured_launch_starts(app: str, tmp_path) -> None:
    """The control. Without it every assertion above is equally satisfied by an
    app that refuses to start under any circumstances whatsoever."""
    output = _launch(app, {
        "SLACK_SIGNING_SECRET": "s3cr3t",
        "SLACK_TEAM_ID": "T_ALLOWED",
        "RUNTESTS_CHANNELS": "C_ALLOWED",
    }, tmp_path, until=STARTED)

    assert "REFUSING TO START" not in output, output[-2000:]
    assert STARTED in output, output[-2000:]


@pytest.mark.parametrize("app", APPS)
def test_the_opt_out_starts_it_and_names_what_it_ignored(app: str, tmp_path) -> None:
    output = _launch(app, {"RUNTESTS_INSECURE_DEV": "1"}, tmp_path, until=STARTED)

    assert STARTED in output, output[-2000:]
    assert "NOT PROTECTING ANYTHING" in output, output[-2000:]
    for name in ("SLACK_SIGNING_SECRET", "SLACK_TEAM_ID", "RUNTESTS_CHANNELS"):
        assert name in output


# ── the launcher must not rescue a half-configured deployment ────────────────
#
# `run.sh` sets RUNTESTS_INSECURE_DEV so a fresh checkout works with no
# configuration at all. Its first version set it whenever ANY protection was
# absent — so a deployment with a signing secret, a team id and a typo in
# RUNTESTS_CHANNELS was opted into insecure mode BY ITS OWN LAUNCHER, restoring
# the exact fail-open this whole change closes, and doing it to the one operator
# who was visibly trying to configure the thing.
#
# Found by review, not by a test, which is why these exist.

def _run_sh(overrides: dict[str, str], tmp_path, *, until: str,
            expect_exit: bool = False,
            deadline: float = 45.0) -> tuple[int | None, str]:
    env = {k: v for k, v in os.environ.items() if k not in PROTECTIONS}
    env["EDGE_DB_PATH"] = str(tmp_path / "edge.db")
    env["EDGE_KEY_PATH"] = str(tmp_path / "edge.pem")
    env["EDGE_TRUSTED_KEYS_DIR"] = str(tmp_path / "trusted")
    env["PORT"] = "0"
    env.update(overrides)

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    process = subprocess.Popen(
        ["bash", "run.sh", "edge"], cwd=root, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        start_new_session=True,
    )
    chunks: list[str] = []
    reader = threading.Thread(target=lambda: chunks.extend(process.stdout), daemon=True)
    reader.start()
    try:
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            if until in "".join(chunks) or process.poll() is not None:
                break
            time.sleep(0.05)
    finally:
        _settle(process, reader, expect_exit=expect_exit)
    return process.returncode, "".join(chunks)


@pytest.mark.parametrize(
    "partial",
    [
        {"SLACK_SIGNING_SECRET": "s3cr3t"},
        {"SLACK_TEAM_ID": "T_ALLOWED"},
        {"SLACK_SIGNING_SECRET": "s3cr3t", "SLACK_TEAM_ID": "T_ALLOWED"},
        {"SLACK_SIGNING_SECRET": "s3cr3t", "RUNTESTS_CHANNELS": "C_ALLOWED"},
    ],
    ids=["secret-only", "team-only", "secret+team", "secret+channels"],
)
def test_run_sh_refuses_a_half_configured_start(partial: dict, tmp_path) -> None:
    """One protection set is somebody configuring this. The launcher must not
    decide for them; the gate must refuse and name what is still missing."""
    code, output = _run_sh(partial, tmp_path, until="REFUSING TO START",
                           expect_exit=True)

    assert "REFUSING TO START" in output, output[-2000:]
    assert "RUNTESTS_INSECURE_DEV=1 for local dev" not in output, (
        "run.sh opted a half-configured start into insecure mode"
    )
    assert code == 2, f"expected exit 2, got {code}"


def test_run_sh_still_works_on_a_fresh_checkout(tmp_path) -> None:
    """The other direction. With NOTHING set, `bash run.sh edge` must still
    start — that is the whole reason the opt-out is applied at all, and a gate
    that broke the documented local command would simply be turned off."""
    _, output = _run_sh({}, tmp_path, until=STARTED)

    assert "setting RUNTESTS_INSECURE_DEV=1 for local dev" in output, output[-2000:]
    assert STARTED in output, output[-2000:]
    assert "NOT PROTECTING ANYTHING" in output, output[-2000:]


def test_run_sh_leaves_a_fully_configured_start_alone(tmp_path) -> None:
    _, output = _run_sh({
        "SLACK_SIGNING_SECRET": "s3cr3t",
        "SLACK_TEAM_ID": "T_ALLOWED",
        "RUNTESTS_CHANNELS": "C_ALLOWED",
    }, tmp_path, until=STARTED)

    assert STARTED in output, output[-2000:]
    assert "NOT PROTECTING ANYTHING" not in output
    assert "RUNTESTS_INSECURE_DEV" not in output


# ── a store that cannot be opened must refuse to start ───────────────────────
#
# Found by planting a defect in the embedded example: a Postgres DSN whose
# password was not URL-encoded. The edge started, answered /healthz, logged
# `store: postgres`, and returned 500 to the first Slack command — the failure
# arriving in front of a user, hours later, reading as the bot being broken.
#
# Two things were wrong and both were about honesty rather than connectivity.
# The store was opened lazily, on the first request. And the startup line
# reported the CONFIGURED backend, so it said `postgres` about a database it had
# never reached — a line whose only job is to distinguish a working Postgres
# from a silent fallback, and which could not.

@pytest.mark.parametrize("app", APPS)
def test_an_unreachable_store_refuses_to_start(app: str, tmp_path) -> None:
    dsn = "postgresql://nobody:nothing@127.0.0.1:1/nonexistent?connect_timeout=2"
    output = _launch(app, {
        "SLACK_SIGNING_SECRET": "s3cr3t",
        "SLACK_TEAM_ID": "T_ALLOWED",
        "RUNTESTS_CHANNELS": "C_ALLOWED",
        "EDGE_DB_DSN": dsn,
        "RUNTESTS_DB_DSN": dsn,
    }, tmp_path, until=REFUSED, expect_exit=True, deadline=90)

    assert REFUSED in output, output[-2000:]
    assert STARTED not in output, "it must not have opened a socket first"
    assert "cannot reach the Postgres store" in output, output[-2000:]


@pytest.mark.parametrize("app", APPS)
def test_the_store_that_was_OPENED_is_the_one_reported(app: str, tmp_path) -> None:
    """`store configured:` is an intention; `store ready:` is a fact.

    They are different strings on purpose. Only the second is written after
    something was actually opened, and only the second is what the deployment
    scripts assert.
    """
    output = _launch(app, {
        "SLACK_SIGNING_SECRET": "s3cr3t",
        "SLACK_TEAM_ID": "T_ALLOWED",
        "RUNTESTS_CHANNELS": "C_ALLOWED",
    }, tmp_path, until=STARTED)

    assert "store ready: sqlite" in output, output[-2000:]
