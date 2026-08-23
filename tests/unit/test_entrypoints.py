"""The servers announce their Slack configuration BEFORE they start serving.

This file exists because of a real defect, and it is aimed squarely at it.
`slack.py` had the console fallback and a passing test for it from the start —
but the V1 API server never said anything about Slack at startup, so an operator
who ran it with no token got a working test runner that posted nowhere and no
hint of it until the first run. Measured 2026-08-22: startup printed only
uvicorn's own four lines.

The lesson generalises: a test of the helper alone could not have caught that,
because the helper was correct. What was missing was the CALL. So these tests
assert the wiring, and they assert it happens before the server starts — a
warning logged after `uvicorn.run()` blocks would never be printed at all.
"""

import logging

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    # These tests are about what is ANNOUNCED at startup, not about whether the
    # process is allowed to start. Both servers now refuse an unconfigured
    # start, so without this every test in this file would be exercising the
    # refusal instead of the announcement it is named for.
    monkeypatch.setenv("RUNTESTS_INSECURE_DEV", "1")


def test_the_v1_server_warns_about_slack_before_it_starts_serving(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import uvicorn

    from slack_runtests import __main__ as entry

    ordering: list[str] = []

    def fake_run(*_args, **_kwargs) -> None:
        # Records the moment the server would begin blocking. Anything logged
        # after this point is logged after the process is already serving.
        ordering.append("uvicorn.run")

    monkeypatch.setattr(uvicorn, "run", fake_run)

    class Recorder(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if "SLACK_BOT_TOKEN" in record.getMessage():
                ordering.append("announced")

    handler = Recorder()
    logging.getLogger().addHandler(handler)
    try:
        with caplog.at_level(logging.INFO):
            assert entry.main() == 0
    finally:
        logging.getLogger().removeHandler(handler)

    assert ordering == ["announced", "uvicorn.run"], (
        f"expected the Slack warning before the server starts, got {ordering}"
    )


def test_the_edge_announces_which_store_it_was_configured_with(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path
) -> None:
    """Same lesson, second surface — and the two backends fail in opposite ways.

    A SQLite file inside a container with no persistent volume loses the queue
    on the next redeploy. A Postgres DSN that quietly fell back to a file would
    look identical in every log line that follows. Neither failure announces
    itself, so the startup line is the only place the difference is visible.

    Note the wording: this line reports what was CONFIGURED. `store ready:` —
    logged by the app's lifespan once a store has actually been opened — is the
    one that reports reality, and the two are deliberately different strings.
    An earlier version had only this line and said `store: postgres` for a DSN
    that could not be reached at all.
    """
    import uvicorn

    from edge_server import __main__ as entry

    monkeypatch.setenv("EDGE_DB_PATH", str(tmp_path / "edge.db"))
    monkeypatch.setenv("EDGE_KEY_PATH", str(tmp_path / "edge.pem"))
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)

    with caplog.at_level(logging.INFO):
        assert entry.main() == 0

    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("store configured: sqlite") for m in messages), messages
    assert any("caps:" in m for m in messages), messages


def test_a_postgres_password_is_never_written_to_the_log() -> None:
    """The startup line names the store. It must not name the store's password.

    A credential in a log line is a credential in every aggregator that reads
    it, and it survives long after the deployment that printed it.
    """
    from edge_server.__main__ import _redacted

    line = _redacted("postgresql://edge:s3cr3t@db.internal:5432/testrunner")

    assert "s3cr3t" not in line
    assert "edge" in line and "db.internal:5432" in line
    # A DSN with no password is not mangled on the way through.
    assert _redacted("postgresql://db.internal/testrunner") == (
        "postgresql://db.internal/testrunner"
    )


# ── the startup gate, at the entry points ────────────────────────────────────
#
# `tests/unit/test_authz_startup.py` covers the decision. These two cover the
# WIRING — that each entry point asks, and asks before `uvicorn.run` blocks.
# The decision being right is worth nothing if a server never consults it, and
# a refusal printed after the socket is open is not a refusal.

@pytest.mark.parametrize(
    "module,extra_env",
    [
        ("slack_runtests.__main__", {}),
        ("edge_server.__main__", {"EDGE_KEY_PATH": "", "EDGE_DB_PATH": ""}),
    ],
)
def test_an_unconfigured_server_exits_instead_of_serving(
    module: str, extra_env: dict, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    import importlib

    import uvicorn

    monkeypatch.delenv("RUNTESTS_INSECURE_DEV", raising=False)
    for name in ("SLACK_SIGNING_SECRET", "SLACK_TEAM_ID", "RUNTESTS_CHANNELS",
                 "RUNTESTS_USERS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in extra_env.items():
        monkeypatch.setenv(name, value or str(tmp_path / name.lower()))

    served: list[str] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: served.append("served"))

    entry = importlib.import_module(module)
    assert entry.main() == 2, "an unconfigured server must exit non-zero"
    assert served == [], "it must refuse BEFORE uvicorn.run, which blocks"
    assert "REFUSING TO START" in capsys.readouterr().err


@pytest.mark.parametrize(
    "module,extra_env",
    [
        ("slack_runtests.__main__", {}),
        ("edge_server.__main__", {"EDGE_KEY_PATH": "", "EDGE_DB_PATH": ""}),
    ],
)
def test_a_configured_server_serves(
    module: str, extra_env: dict, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The other direction — the gate must not simply refuse everything."""
    import importlib

    import uvicorn

    monkeypatch.delenv("RUNTESTS_INSECURE_DEV", raising=False)
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "s3cr3t")
    monkeypatch.setenv("SLACK_TEAM_ID", "T_ALLOWED")
    monkeypatch.setenv("RUNTESTS_CHANNELS", "C_ALLOWED")
    for name, value in extra_env.items():
        monkeypatch.setenv(name, value or str(tmp_path / name.lower()))

    served: list[str] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: served.append("served"))

    entry = importlib.import_module(module)
    assert entry.main() == 0
    assert served == ["served"]
