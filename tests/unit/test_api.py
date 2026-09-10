"""Endpoint behaviour: authorisation, validation, idempotency.

Uses FastAPI's TestClient, so these exercise the real routing and the real
signature check — not a stubbed handler.
"""

import time
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from slack_runtests import api
from slack_runtests.config import Config
from slack_runtests.signature import sign
from slack_runtests.store import QUEUED, RUNNING, open_store

pytestmark = pytest.mark.unit

SECRET = "test-signing-secret"


def form_body(text: str = "-p alpha", **overrides: str) -> str:
    fields = {
        "team_id": "T_ALLOWED",
        "channel_id": "C_ALLOWED",
        "channel_name": "testing",
        "user_id": "U_ALLOWED",
        "command": "/runtests",
        "text": text,
        "trigger_id": "trigger-1",
    }
    fields.update(overrides)
    return urllib.parse.urlencode(fields)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> TestClient:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    app = api.app
    # A fresh store per test, in this test's own tmp_path. The record of what
    # has been dispatched used to be a module-level dict that had to be cleared
    # here — which is the same per-process state the production defect was
    # about, reproduced in the fixture.
    app.state.store = open_store(str(tmp_path / "runtests.db"))
    app.state.config = Config(
        mode="github",  # never actually spawn pytest from a unit test
        signing_secret=SECRET,
        allowed_team="T_ALLOWED",
        allowed_channels=frozenset({"C_ALLOWED"}),
        allowed_users=frozenset({"U_ALLOWED"}),
        # NEH-1139: the trigger allowlists are configuration now. Fictional
        # values, so this file cannot become a place house names live.
        allowed_products=frozenset({"alpha", "beta"}),
        allowed_servers=frozenset({"sandbox"}),
        allowed_test_scopes=frozenset({"smoke"}),
        github_repo="",  # forces the dry-run path in the dispatcher
        github_token="",
    )
    return TestClient(app)


def post(client: TestClient, body: str, *, signed: bool = True) -> object:
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if signed:
        ts = str(int(time.time()))
        headers["X-Slack-Request-Timestamp"] = ts
        headers["X-Slack-Signature"] = sign(body.encode(), ts, SECRET)
    return client.post("/slack/commands", content=body, headers=headers)


def test_healthz_reveals_nothing_about_configuration(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_a_valid_signed_command_is_accepted(client: TestClient) -> None:
    response = post(client, form_body())
    assert response.status_code == 200
    assert "Queued" in response.json()["text"]


def test_an_unsigned_request_is_rejected(client: TestClient) -> None:
    response = post(client, form_body(), signed=False)
    assert response.status_code == 401


def test_a_signature_over_different_bytes_is_rejected(client: TestClient) -> None:
    # Sign one body, send another. This is the check that a naive
    # "verify then re-parse" implementation can get wrong.
    body = form_body()
    ts = str(int(time.time()))
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": sign(body.encode(), ts, SECRET),
    }
    response = client.post(
        "/slack/commands", content=form_body(text="-p beta"), headers=headers
    )
    assert response.status_code == 401


def test_another_workspace_is_refused(client: TestClient) -> None:
    # The signature proves it came from Slack. Only team_id proves it came from
    # YOUR Slack.
    response = post(client, form_body(team_id="T_SOMEONE_ELSE"))
    assert response.status_code == 200
    assert "not available in this workspace" in response.json()["text"]


def test_a_disallowed_channel_is_refused(client: TestClient) -> None:
    response = post(client, form_body(channel_id="C_RANDOM"))
    assert "not enabled in this channel" in response.json()["text"]


def test_a_user_outside_the_allowlist_is_refused(client: TestClient) -> None:
    # Workspace membership is not an entitlement: guests, contractors and Slack
    # Connect users are all "in" the workspace.
    response = post(client, form_body(user_id="U_GUEST"))
    assert "not on the test-automation allowlist" in response.json()["text"]


def test_a_bad_command_is_a_readable_200_not_a_500(client: TestClient) -> None:
    # A 500 shows the user Slack's generic "dispatch_failed" instead of the
    # reason their command was wrong.
    response = post(client, form_body(text="-p ../../etc"))
    assert response.status_code == 200
    text = response.json()["text"]
    assert "invalid choice" in text
    assert "Try:" in text


def test_a_retried_command_does_not_start_a_second_run(client: TestClient) -> None:
    # Slack retries anything slow or non-2xx. Keyed on trigger_id, so the same
    # invocation arriving twice must be a no-op — otherwise one slow morning is
    # four identical runs against the same box.
    first = post(client, form_body())
    second = post(client, form_body())
    assert "Queued" in first.json()["text"]
    assert "already queued" in second.json()["text"]
    assert len(client.app.state.store.recent()) == 1


def test_a_different_trigger_id_does_start_a_second_run(client: TestClient) -> None:
    # A different PRODUCT as well as a different trigger: the default cap allows
    # one run of a given (product, server) at a time, and this test is about the
    # idempotency key, not about the cap. The cap has its own tests.
    post(client, form_body())
    post(client, form_body(text="-p beta", trigger_id="trigger-2"))
    assert len(client.app.state.store.recent()) == 2


def test_the_record_of_a_dispatch_survives_a_new_app_instance(
    client: TestClient, tmp_path
) -> None:
    """The failure the module-level dict had, asserted rather than described.

    A dict is per-worker and per-process: a second uvicorn worker had no idea
    the first had already dispatched, so a Slack retry landing on it started a
    second identical run. Re-opening the store is the closest a unit test gets
    to a second worker, and it is enough to pin the property.
    """
    post(client, form_body())

    reopened = open_store(str(tmp_path / "runtests.db"))
    assert reopened.job(_correlation_of(client)) is not None
    assert reopened.recent()[0]["state"] == RUNNING


def _correlation_of(client: TestClient) -> str:
    return client.app.state.store.recent()[0]["id"]


def test_running_the_same_product_twice_at_once_is_refused(client: TestClient) -> None:
    """A cap refusal says which limit was hit — it is not a duplicate."""
    post(client, form_body())
    response = post(client, form_body(trigger_id="trigger-2"))

    text = response.json()["text"]
    assert "already running" in text
    assert "already queued" not in text


def test_a_channel_cannot_queue_past_its_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Queued, not running — so this needs the queueing path the edge uses."""
    from slack_runtests.store import Caps, Job

    store = open_store(str(tmp_path / "caps.db"))
    caps = Caps(max_queued_per_channel=1)

    def job(job_id: str, product: str) -> Job:
        return Job(id=job_id, product=product, server="staging", select_expr=None,
                   marker=None, slack_channel="#testing", slack_user="U1")

    assert store.enqueue(job("a", "webapp"), caps=caps).accepted
    assert not store.enqueue(job("b", "billing"), caps=caps).accepted
    assert store.counts() == {QUEUED: 1}


def test_results_reports_the_last_run(client: TestClient) -> None:
    post(client, form_body(text="-p alpha"))
    response = post(client, form_body(text="results -p alpha", trigger_id="t-results"))
    assert "Last `alpha` run" in response.json()["text"]


def test_results_with_no_prior_run_says_so(client: TestClient) -> None:
    response = post(client, form_body(text="results -p beta"))
    assert "No recorded run" in response.json()["text"]


# ── `results` and the flags it used to demand (NEH-1166) ─────────────────────
#
# The `client` fixture above allows exactly ONE server, so `-s` is DEFAULTED
# rather than required and this friction was invisible to every test in this
# file. That is the measurement worth keeping: the existing
# `test_results_reports_the_last_run` passed before this change and after it,
# and could never have caught the defect.
#
# These use a TWO-server deployment, which is where `-s` becomes required.


@pytest.fixture
def multi_server_client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> TestClient:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    app = api.app
    app.state.store = open_store(str(tmp_path / "multi.db"))
    app.state.config = Config(
        mode="github",
        signing_secret=SECRET,
        allowed_team="T_ALLOWED",
        allowed_channels=frozenset({"C_ALLOWED"}),
        allowed_users=frozenset({"U_ALLOWED"}),
        allowed_products=frozenset({"alpha", "beta"}),
        allowed_servers=frozenset({"sandbox", "staging"}),
        allowed_test_scopes=frozenset({"smoke", "full"}),
        github_repo="",
        github_token="",
    )
    return TestClient(app)


def test_results_needs_no_server_even_when_a_run_would(multi_server_client) -> None:
    c = multi_server_client
    # A run here genuinely requires -s and --test_scope …
    refused = post(c, form_body(text="-p alpha", trigger_id="t-run-bad"))
    assert "required" in refused.json()["text"] or "-s" in refused.json()["text"]

    # … and `results` does not.
    post(c, form_body(text="-p alpha -s sandbox --test_scope smoke", trigger_id="t-run"))
    got = post(c, form_body(text="results -p alpha", trigger_id="t-res")).json()["text"]
    assert "Last `alpha` run" in got


def test_results_narrows_to_the_server_it_was_given(multi_server_client) -> None:
    c = multi_server_client
    post(c, form_body(text="-p alpha -s sandbox --test_scope smoke", trigger_id="t1"))
    post(c, form_body(text="-p alpha -s staging --test_scope smoke", trigger_id="t2"))

    # Unnarrowed: the newest run, whichever box.
    any_ = post(c, form_body(text="results -p alpha", trigger_id="t3")).json()["text"]
    assert "on `staging`" in any_

    # Narrowed to the OLDER run's server. Before this change the flag was
    # required and then discarded, so this returned `staging` — the same answer
    # as the line above, which is what made the flag unfalsifiable.
    old = post(c, form_body(text="results -p alpha -s sandbox", trigger_id="t4")).json()["text"]
    assert "on `sandbox`" in old


def test_results_for_a_server_with_no_runs_says_so_rather_than_falling_back(
    multi_server_client,
) -> None:
    c = multi_server_client
    post(c, form_body(text="-p alpha -s staging --test_scope smoke", trigger_id="t1"))
    got = post(c, form_body(text="results -p alpha -s sandbox", trigger_id="t2")).json()["text"]
    assert "No recorded run" in got
    assert "sandbox" in got


def test_a_test_scope_on_results_is_answered_rather_than_silently_ignored(
    multi_server_client,
) -> None:
    # A run records its product and its server and NOT its test scope, so the
    # flag CANNOT narrow the answer. Accepting it and saying nothing is the
    # original defect; the reply says which scoping was applied instead.
    c = multi_server_client
    post(c, form_body(text="-p alpha -s sandbox --test_scope smoke", trigger_id="t1"))
    got = post(
        c, form_body(text="results -p alpha --test_scope full", trigger_id="t2")
    ).json()["text"]
    assert "Last `alpha` run" in got
    assert "did not narrow this answer" in got

    # And it is absent when the flag was not given — otherwise the note would
    # be noise on every reply and nobody would read it.
    quiet = post(c, form_body(text="results -p alpha", trigger_id="t3")).json()["text"]
    assert "did not narrow this answer" not in quiet
