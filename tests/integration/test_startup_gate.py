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


def _launch(app: str, overrides: dict[str, str], tmp_path, *, until: str,
            deadline: float = 60.0) -> str:
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
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - a wedged uvicorn
            process.kill()
            process.wait(timeout=15)
        reader.join(timeout=5)
    return "".join(chunks)


@pytest.mark.parametrize("app", APPS)
def test_a_bare_uvicorn_launch_cannot_go_around_the_gate(app: str, tmp_path) -> None:
    output = _launch(app, {}, tmp_path, until=REFUSED)

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
