"""What this actually sends Slack, received by a real HTTP server.

NEH-1088. The E2E tier was scoped as a staging Slack workspace plus a
self-hosted runner, and that was ruled out: a Slack app, a bot token, a signing
secret and a runner holding a private key is a permanent operational burden for
a prototype, and the standing secrets are a cost that never stops being paid.

THE HALF EVERY OTHER TEST IS BLIND TO
-------------------------------------
The unit tests stub ``fetch`` and assert on what was handed to the stub. That
proves the *call site* — that ``post()`` was reached with the right arguments —
and says nothing about the request. The JSON body, the ``Authorization`` header,
the HTTP method and the URL path are constructed inside ``_call`` and no
assertion has ever seen them.

So this tier does not mock the transport. It starts a real HTTP server on
127.0.0.1, points ``SLACK_API_BASE`` at it, and lets ``SlackNotifier`` make a
genuine request. What arrives is exactly what would have reached Slack.

Recorded fixtures of Slack's *inbound* payloads were the first plan, and they
are strictly weaker: they describe what Slack sends us, never what we send
Slack. A fixture cannot be wrong about a header we never wrote down.

NO SECRET, NO WORKSPACE, NO NETWORK
-----------------------------------
The token is a fake string, the server is local, and nothing leaves the
machine. That is the whole point of preferring this to the staging estate.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from slack_runtests.slack import SlackNotifier

pytestmark = pytest.mark.integration

TOKEN = "xoxb-fake-token-for-tests"


class _FakeSlack(BaseHTTPRequestHandler):
    """Receives one request and records it verbatim.

    Answers in Slack's own shape — ``{"ok": true, "ts": ...}`` — because the
    notifier reads ``ok`` and now reports ``ts``. A fake that answered ``200``
    with an empty body would prove the request was made and nothing about
    whether the response is understood.
    """

    received: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802  (the stdlib spells it this way)
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode()
        type(self).received.append(
            {
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "raw": raw,
            }
        )
        body = json.dumps({"ok": True, "ts": "1787441464.148339"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        """Silence. The server's own logging is noise in a test run."""


@pytest.fixture
def fake_slack(monkeypatch):
    """A real Slack-shaped server on an ephemeral port.

    Port 0 so concurrent runs cannot collide — a hard-coded port is the kind of
    thing that passes alone and fails in CI beside anything else.
    """
    _FakeSlack.received = []
    server = HTTPServer(("127.0.0.1", 0), _FakeSlack)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]

    monkeypatch.setenv("SLACK_API_BASE", f"http://{host}:{port}")
    monkeypatch.setenv("SLACK_BOT_TOKEN", TOKEN)
    try:
        yield _FakeSlack
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _only(received: list[dict]) -> dict:
    assert len(received) == 1, f"expected exactly one request, got {len(received)}"
    return received[0]


# ── the request itself ───────────────────────────────────────────────────────


def test_it_really_reaches_the_api_over_http(fake_slack):
    result = SlackNotifier(channel="#deploy").post("a real request")
    assert result.dry_run is False
    assert len(fake_slack.received) == 1, "nothing arrived — the request was never made"


def test_it_calls_chat_postMessage(fake_slack):
    SlackNotifier(channel="#deploy").post("hello")
    assert _only(fake_slack.received)["path"] == "/chat.postMessage"


def test_it_sends_the_bearer_token(fake_slack):
    # Never asserted anywhere before. A malformed Authorization header is
    # `invalid_auth` at runtime — an error that reads like a revoked token.
    SlackNotifier(channel="#deploy").post("hello")
    assert _only(fake_slack.received)["headers"]["authorization"] == f"Bearer {TOKEN}"


def test_it_declares_json_with_a_charset(fake_slack):
    # Slack documents utf-8 explicitly, and the message text is full of emoji.
    SlackNotifier(channel="#deploy").post("hello")
    got = _only(fake_slack.received)["headers"]["content-type"]
    assert "application/json" in got
    assert "utf-8" in got.lower()


def test_the_body_is_json_carrying_the_channel_and_text(fake_slack):
    SlackNotifier(channel="#deploy").post("the message body")
    payload = json.loads(_only(fake_slack.received)["raw"])
    assert payload["channel"] == "#deploy"
    assert payload["text"] == "the message body"


def test_emoji_survive_the_wire(fake_slack):
    # Every deploy summary leads with one. A mis-encoded body is not an error —
    # it is a message that arrives looking broken, which nobody files a bug for.
    SlackNotifier(channel="#deploy").post("✅ smoke passed — deployed AND verified")
    payload = json.loads(_only(fake_slack.received)["raw"])
    assert payload["text"] == "✅ smoke passed — deployed AND verified"


# ── the response ─────────────────────────────────────────────────────────────


def test_it_reads_the_ts_back(fake_slack):
    # `ts` is Slack's proof the message exists. The notifier surfaces it so a
    # deploy log carries evidence rather than silence.
    result = SlackNotifier(channel="#deploy").post("hello")
    assert result.ts == "1787441464.148339"


def test_a_rejection_is_reported_not_raised(fake_slack, monkeypatch):
    """An API-level `ok: false` must never propagate as an exception.

    The whole reason this library is safe to import from a test suite is that
    reporting cannot fail the thing it reports on.
    """

    class _Rejecting(_FakeSlack):
        def do_POST(self) -> None:  # noqa: N802
            body = json.dumps({"ok": False, "error": "channel_not_found"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), _Rejecting)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("SLACK_API_BASE", f"http://127.0.0.1:{server.server_address[1]}")
    try:
        result = SlackNotifier(channel="#nope").post("hello")
        assert result.dry_run is False
        assert result.ts is None or result.ts == ""
    finally:
        server.shutdown()
        server.server_close()


# ── the guard that keeps the rest honest ─────────────────────────────────────


def test_no_token_means_nothing_is_sent(fake_slack, monkeypatch, capsys):
    """With the token removed, the fake must receive NOTHING.

    Every assertion above reads `received`, so a notifier that silently stopped
    making requests would leave them failing — but this is the inverse guard:
    it proves the dry-run path really is inert on the wire, rather than posting
    somewhere and also printing.
    """
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    result = SlackNotifier(channel="#deploy").post("should not be sent")
    assert result.dry_run is True
    assert fake_slack.received == [], "a dry run reached the network"
    assert "dry-run" in capsys.readouterr().err
