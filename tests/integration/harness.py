"""Fixed identities and request-building for the integration tier.

Separate from `conftest.py` so the test module can import it by name. A test
file cannot do a relative import from a conftest — there is no package — and
`import conftest` works but reads like a mistake.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from slack_runtests import identity
from slack_runtests.signature import sign

#: The "valid test Slack origin": real-looking identifiers that belong to
#: nobody. The edge is started with exactly these on its allowlists, so a
#: request carrying them is legitimate *because it was configured to be* —
#: which is the same mechanism a real workspace uses, not a test-only bypass.
TEST_SIGNING_SECRET = "integration-test-signing-secret"
TEST_TEAM_ID = "T_INTEGRATION"
TEST_CHANNEL_ID = "C_INTEGRATION"

# The trigger allowlists these servers are launched with (NEH-1139). Fictional
# on purpose: a real product name in a test fixture is a real product name
# shipped in this repository, which test_no_house_names_in_defaults.py exists to
# prevent -- and it would also make the fixture a claim about the author's
# estate rather than about the code.
#
# One server and one test scope, so a command needs neither flag: the parser
# defaults a sole allowed value, and these tests are about the doors rather than
# about the grammar.
# These match the FIXTURE SUITE on disk -- `fixture_suite/webapp/` -- because
# the runner builds `tests/{product}` as a path, so the allowlist and the
# directory have to agree. They are generic placeholders rather than house
# names, which is what `test_no_house_names_in_defaults.py` actually bans, so
# there is nothing here to launder into a shipped default.
#
# The UNIT tests use a deliberately different vocabulary (`alpha`, `sandbox`)
# for a reason worth keeping: a test that imports the allowlist and then asserts
# the allowlist accepts it cannot fail. Here the values come from the fixture on
# disk instead, which is a real constraint rather than a restatement.
TEST_PRODUCTS = "webapp,billing,catalog"
TEST_SERVERS = "staging,dev,local"
TEST_TEST_SCOPES = "smoke"
TEST_CHANNEL_NAME = "testing"
TEST_USER_ID = "U_INTEGRATION"


@dataclass
class EdgeUnderTest:
    url: str
    signing_secret: str
    #: True when the fixture spawned the process and is responsible for it.
    managed: bool
    #: Where the edge reads pre-authorised test-server keys from — the
    #: production enrolment path, and the only one these tests use. `None` when
    #: the edge is somebody else's process (RUNTESTS_EDGE_URL): we do not know
    #: where its keys live and must not guess at a path on a running server.
    trusted_keys_dir: Path | None = None


def slack_body(text: str, **overrides: str) -> bytes:
    """A payload shaped exactly like Slack's, with a fresh trigger per call.

    The trigger id must be new every time: the edge keys idempotency on it, so
    a fixed value would make the second call in a session collide with the
    first and get "already queued" instead of "queued" — a failure that looks
    like a broken assertion and is really a stale fixture.
    """
    fields = {
        "token": "gIkuvaNzQIHg97ATvDxqgjtO",
        "team_id": TEST_TEAM_ID,
        "team_domain": "example",
        "channel_id": TEST_CHANNEL_ID,
        "channel_name": TEST_CHANNEL_NAME,
        "user_id": TEST_USER_ID,
        "user_name": "qa.bot",
        "command": "/runtests",
        "text": text,
        "api_app_id": "A0123456789",
        "response_url": "https://hooks.slack.com/commands/1234/5678",
        "trigger_id": f"integration-{uuid.uuid4().hex}",
    }
    fields.update(overrides)
    return urllib.parse.urlencode(fields).encode()


def post(edge: EdgeUnderTest, body: bytes, *, secret: str | None = None,
         timestamp: str | None = None) -> httpx.Response:
    """POST a signed slash command over a real socket.

    `secret` defaults to the one the server was started with — pass a different
    one to forge a request that is well-formed but not from Slack.
    """
    ts = timestamp or str(int(time.time()))
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": sign(body, ts, secret if secret is not None else edge.signing_secret),
    }
    return httpx.post(f"{edge.url}/slack/commands", content=body, headers=headers, timeout=15)


# ── the second door: a real test server, with a real Ed25519 identity ────────

@dataclass
class RunnerIdentity:
    """A test server's key, and the headers that prove a request came from it.

    The edge's runner door is not protected by Slack's HMAC — nothing about a
    request from an internal machine is signed by Slack — so it has its own
    lock: Ed25519 per test server, over method, path, timestamp and body. This
    class is the client half of that, built from the same `identity` module the
    real test server uses rather than from a test-only shortcut. A helper that
    signed differently from production would pass while the deployed pair
    disagreed.
    """

    runner_id: str
    key: object  # Ed25519PrivateKey

    @classmethod
    def create(cls, runner_id: str) -> "RunnerIdentity":
        return cls(runner_id=runner_id, key=identity.generate())

    @property
    def public_key(self) -> str:
        return identity.public_b64(self.key)  # type: ignore[arg-type]

    def headers(self, method: str, path: str, body: bytes) -> dict[str, str]:
        ts = str(int(time.time()))
        return {
            "Content-Type": "application/json",
            identity.HEADER_RUNNER_ID: self.runner_id,
            identity.HEADER_TIMESTAMP: ts,
            identity.HEADER_SIGNATURE: identity.sign(self.key, method, path, ts, body),  # type: ignore[arg-type]
        }

    def preauthorise(self, trusted_keys_dir: Path) -> None:
        """Drop `<runner_id>.pub` where an operator would put it.

        This is production enrolment: the edge accepts a new test server only
        when its key is already on disk. The bootstrap token is the other path
        and these tests deliberately leave it unset, so a regression that made
        the token the only working route would fail here rather than pass.
        """
        trusted_keys_dir.mkdir(parents=True, exist_ok=True)
        (trusted_keys_dir / f"{self.runner_id}.pub").write_text(self.public_key)

    def post(self, edge: EdgeUnderTest, path: str, payload: dict | None = None,
             *, key_override: object = None) -> httpx.Response:
        """A signed POST to the runner door, over a real socket.

        `key_override` signs with a different key while still presenting this
        runner id — a forged request that is well-formed and unauthorised.
        """
        body = b"" if payload is None else json.dumps(payload).encode()
        headers = self.headers("POST", path, body)
        if key_override is not None:
            signer = RunnerIdentity(runner_id=self.runner_id, key=key_override)
            headers = signer.headers("POST", path, body)
            headers[identity.HEADER_RUNNER_ID] = self.runner_id
        return httpx.post(f"{edge.url}{path}", content=body, headers=headers, timeout=30)


def edge_public_key(edge: EdgeUnderTest) -> str:
    """The edge's own public key, fetched the way a test server fetches it."""
    response = httpx.get(f"{edge.url}/edge/identity", timeout=15)
    response.raise_for_status()
    return str(response.json()["public_key"])


def reply_is_signed_by_edge(response: httpx.Response, edge_key: str) -> bool:
    """Verify the edge signed what it sent back.

    Signing only one direction would leave a test server trusting whatever
    answered its poll — and a test server is the thing that runs code and posts
    to Slack, so "this job really came from the edge" is not a nicety.
    """
    return identity.verify_reply(
        edge_key,
        response.headers.get(identity.HEADER_EDGE_TIMESTAMP, ""),
        response.headers.get(identity.HEADER_EDGE_SIGNATURE, ""),
        response.content,
    )
