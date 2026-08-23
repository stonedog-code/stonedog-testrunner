"""The second public door — the one test servers use.

This door has no Slack signature to lean on, and it is the more dangerous of
the two: the test servers are what talk to Slack and what run code, so anything
that can feed one a job can execute inside the network and post to the channel
about it. These tests are the door's lock, checked from the outside.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from edge_server import app as edge_app
from edge_server.config import EdgeConfig
from slack_runtests.store import open_store
from slack_runtests import identity

pytestmark = pytest.mark.unit

ENROLL_TOKEN = "lab-token"


@pytest.fixture
def trusted_dir(tmp_path):
    path = tmp_path / "trusted_runners"
    path.mkdir()
    return path


@pytest.fixture
def client(tmp_path, trusted_dir) -> TestClient:
    app = edge_app.app
    app.state.config = EdgeConfig(
        signing_secret="unused-here",
        db_path=str(tmp_path / "edge.db"),
        trusted_keys_dir=str(trusted_dir),
        enroll_token=ENROLL_TOKEN,
        admin_token="",
        poll_timeout=0,        # do not hold a unit test open for 25 seconds
        lease_seconds=60,
    )
    app.state.store = open_store(str(tmp_path / "edge.db"))
    app.state.edge_key = identity.generate()
    return TestClient(app)


def send(client: TestClient, path: str, payload: dict, key, runner_id: str,
         *, sign_with=None, timestamp: str | None = None):
    """POST a signed runner request. `sign_with` forges a mismatched signature."""
    body = json.dumps(payload, separators=(",", ":")).encode()
    ts = timestamp or str(int(time.time()))
    return client.post(
        path,
        content=body,
        headers={
            "Content-Type": "application/json",
            identity.HEADER_RUNNER_ID: runner_id,
            identity.HEADER_TIMESTAMP: ts,
            identity.HEADER_SIGNATURE: identity.sign(sign_with or key, "POST", path, ts, body),
        },
    )


def enrol_body(runner_id: str, key, labels=(), token: str = "") -> dict:
    return {
        "runner_id": runner_id,
        "public_key": identity.public_b64(key),
        "labels": list(labels),
        "enroll_token": token,
    }


# ── enrolment ────────────────────────────────────────────────────────────────

def test_a_preauthorised_key_enrols(client, trusted_dir) -> None:
    key = identity.generate()
    (trusted_dir / "runner-1.pub").write_text(identity.public_b64(key))

    response = send(client, "/runner/enroll", enrol_body("runner-1", key), key, "runner-1")

    assert response.status_code == 200
    assert response.json()["runner_id"] == "runner-1"


def test_a_key_that_is_not_the_preauthorised_one_is_refused(client, trusted_dir) -> None:
    """An operator authorised a specific key, not a specific name."""
    authorised = identity.generate()
    attacker = identity.generate()
    (trusted_dir / "runner-1.pub").write_text(identity.public_b64(authorised))

    response = send(client, "/runner/enroll", enrol_body("runner-1", attacker), attacker, "runner-1")

    assert response.status_code == 401


def test_an_unknown_server_without_the_token_is_refused(client) -> None:
    key = identity.generate()

    response = send(client, "/runner/enroll", enrol_body("stranger", key), key, "stranger")

    assert response.status_code == 401


def test_an_unknown_server_with_the_bootstrap_token_enrols(client) -> None:
    key = identity.generate()

    response = send(
        client, "/runner/enroll", enrol_body("runner-9", key, token=ENROLL_TOKEN), key, "runner-9"
    )

    assert response.status_code == 200


def test_enrolment_requires_proof_of_possession_of_the_key(client) -> None:
    """Presenting someone else's public key must not work.

    Without this an attacker with the bootstrap token could enrol a key it does
    not hold in order to squat a runner id — and the runner id is what result
    ownership is checked against, so squatting one is enough to post forged
    results later.
    """
    claimed = identity.generate()
    held = identity.generate()

    response = send(
        client, "/runner/enroll",
        enrol_body("runner-9", claimed, token=ENROLL_TOKEN),
        claimed, "runner-9", sign_with=held,
    )

    assert response.status_code == 401


def test_an_existing_server_cannot_be_taken_over_with_a_new_key(client) -> None:
    """A restart re-enrols with the same key and is fine. A new key is a takeover."""
    original = identity.generate()
    send(client, "/runner/enroll", enrol_body("runner-1", original, token=ENROLL_TOKEN), original, "runner-1")

    attacker = identity.generate()
    response = send(
        client, "/runner/enroll",
        enrol_body("runner-1", attacker, token=ENROLL_TOKEN), attacker, "runner-1",
    )

    assert response.status_code == 401


def test_a_restart_re_enrols_cleanly(client) -> None:
    key = identity.generate()
    first = send(client, "/runner/enroll", enrol_body("runner-1", key, token=ENROLL_TOKEN), key, "runner-1")
    second = send(client, "/runner/enroll", enrol_body("runner-1", key, token=ENROLL_TOKEN), key, "runner-1")

    assert first.status_code == 200 and second.status_code == 200


@pytest.mark.parametrize("runner_id", ["../../etc/passwd", "a/b", "", "x" * 65, "has space", "..%2f"])
def test_a_runner_id_that_is_not_a_plain_name_is_refused(client, runner_id) -> None:
    """The id reaches a filesystem path in the trusted-keys lookup.

    Left unchecked, `../../etc/passwd` is a file-read primitive on the public
    edge — the same class of mistake the Slack parser's allowlist exists to
    prevent, on a different door.
    """
    key = identity.generate()
    response = send(
        client, "/runner/enroll", enrol_body(runner_id, key, token=ENROLL_TOKEN), key, runner_id or "x"
    )

    assert response.status_code in (400, 401)


# ── everything after enrolment ───────────────────────────────────────────────

@pytest.fixture
def enrolled(client):
    key = identity.generate()
    send(client, "/runner/enroll", enrol_body("runner-1", key, token=ENROLL_TOKEN), key, "runner-1")
    return key


def test_an_unsigned_request_is_refused(client, enrolled) -> None:
    response = client.post("/runner/jobs/claim", content=b"{}",
                           headers={identity.HEADER_RUNNER_ID: "runner-1"})

    assert response.status_code == 401


def test_a_request_from_an_unknown_server_is_refused(client, enrolled) -> None:
    """Same flat 401 as a bad signature — the endpoint must not be an oracle
    for which runner ids exist."""
    stranger = identity.generate()
    response = send(client, "/runner/jobs/claim", {}, stranger, "runner-does-not-exist")

    assert response.status_code == 401


def test_a_signature_from_the_wrong_key_is_refused(client, enrolled) -> None:
    response = send(client, "/runner/jobs/claim", {}, enrolled, "runner-1",
                    sign_with=identity.generate())

    assert response.status_code == 401


def test_a_stale_signature_is_refused(client, enrolled) -> None:
    old = str(int(time.time()) - identity.MAX_AGE_SECONDS - 60)
    response = send(client, "/runner/jobs/claim", {}, enrolled, "runner-1", timestamp=old)

    assert response.status_code == 401


def test_claiming_an_empty_queue_answers_204(client, enrolled) -> None:
    response = send(client, "/runner/jobs/claim", {}, enrolled, "runner-1")

    assert response.status_code == 204


def test_the_edge_signs_its_replies(client, enrolled) -> None:
    """So a test server cannot be fed a job by whatever answers its poll."""
    response = send(client, "/runner/heartbeat", {}, enrolled, "runner-1")

    assert response.status_code == 200
    edge_public = identity.public_b64(client.app.state.edge_key)
    assert identity.verify_reply(
        edge_public,
        response.headers[identity.HEADER_EDGE_TIMESTAMP],
        response.headers[identity.HEADER_EDGE_SIGNATURE],
        response.content,
    )
    assert not identity.verify_reply(
        identity.public_b64(identity.generate()),
        response.headers[identity.HEADER_EDGE_TIMESTAMP],
        response.headers[identity.HEADER_EDGE_SIGNATURE],
        response.content,
    )


def test_a_result_cannot_be_posted_for_another_servers_job(client, enrolled) -> None:
    from slack_runtests.store import Job

    store = client.app.state.store
    store.enqueue(Job(id="job-1", product="webapp", server="staging", select_expr=None,
                      marker=None, slack_channel="#testing", slack_user="U1"))
    send(client, "/runner/jobs/claim", {}, enrolled, "runner-1")

    thief = identity.generate()
    send(client, "/runner/enroll", enrol_body("runner-2", thief, token=ENROLL_TOKEN), thief, "runner-2")
    response = send(
        client, "/runner/jobs/job-1/result",
        {"exit_code": 0, "passed": 999, "failed": 0, "skipped": 0, "duration": 1.0, "summary": ""},
        thief, "runner-2",
    )

    assert response.status_code == 409
    assert store.job("job-1")["passed"] is None


# ── operator view ────────────────────────────────────────────────────────────

def test_the_fleet_view_404s_when_no_admin_token_is_configured(client) -> None:
    """Default deny. Which internal machines exist is reconnaissance, so the
    endpoint should not even admit to existing."""
    assert client.get("/admin/fleet").status_code == 404
    assert client.get("/admin/fleet", headers={"Authorization": "Bearer anything"}).status_code == 404


def test_the_fleet_view_needs_the_right_token(client, enrolled) -> None:
    client.app.state.config.admin_token = "operator-token"

    assert client.get("/admin/fleet", headers={"Authorization": "Bearer wrong"}).status_code == 404

    ok = client.get("/admin/fleet", headers={"Authorization": "Bearer operator-token"})
    assert ok.status_code == 200
    assert [r["runner_id"] for r in ok.json()["runners"]] == ["runner-1"]


def test_the_edge_publishes_its_public_key(client) -> None:
    body = client.get("/edge/identity").json()

    assert body["public_key"] == identity.public_b64(client.app.state.edge_key)
    assert body["fingerprint"] == identity.fingerprint(body["public_key"])
