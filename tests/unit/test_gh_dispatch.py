"""`dispatch_workflow` — the call the edge makes for a `gh-action` job.

Faked at the HTTP boundary, deliberately. A test that reached the real GitHub
API would need a token, a repository and a workflow to exist, and would then be
asserting GitHub's behaviour rather than ours. What is ours is: what goes in the
request, what comes back as a result, and what is safe to show a user.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from slack_runtests.runners.github import (
    WORKFLOW_FILE, DispatchResult, dispatch_workflow,
)

pytestmark = pytest.mark.unit


def run(coro):
    """Drive one coroutine.

    `asyncio.run` rather than pytest-asyncio: this file needs three lines of
    async support and adding a plugin would put a dependency in the lockfile,
    the CI image and every contributor's environment for that.
    """
    return asyncio.run(coro)

BASE = dict(
    repo="acme/alpha", workflow="runtests.yml", ref="main", token="ghp_x",
    correlation_id="abc123", product="alpha", server="sandbox",
    test_scope="smoke", select=None, marker=None,
    slack_channel="#testing", slack_user="U1",
)


class FakeClient:
    """Records the one request, answers with a canned status."""

    def __init__(self, status: int = 204, raise_exc: Exception | None = None):
        self.status, self.raise_exc, self.calls = status, raise_exc, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        if self.raise_exc:
            raise self.raise_exc
        return httpx.Response(self.status, request=httpx.Request("POST", url))


@pytest.fixture
def fake(monkeypatch):
    def _install(status: int = 204, raise_exc: Exception | None = None) -> FakeClient:
        client = FakeClient(status, raise_exc)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)
        return client
    return _install


# ── the request ──────────────────────────────────────────────────────────────

def test_a_dispatch_is_accepted_and_says_so(fake) -> None:
    """The positive control. Without it every refusal below is trivially true
    of a function that refuses everything."""
    fake(204)
    result = run(dispatch_workflow(**BASE))
    assert result.ok
    assert "abc123" in result.message


def test_the_workflow_and_repo_land_in_the_url(fake) -> None:
    client = fake(204)
    run(dispatch_workflow(**BASE))
    url = client.calls[0]["url"]
    assert url.endswith("/repos/acme/alpha/actions/workflows/runtests.yml/dispatches")


def test_every_input_the_workflow_declares_is_sent(fake) -> None:
    """A missing input is a 422 that reads as a schema problem on GitHub's side.

    Asserted as a SET, not one by one: `assert "product" in inputs` still passes
    over an payload that dropped four others.
    """
    client = fake(204)
    run(dispatch_workflow(**BASE))
    inputs = client.calls[0]["json"]["inputs"]
    assert set(inputs) == {
        "correlation_id", "product", "server", "test_scope",
        "select", "marker", "slack_channel", "slack_user",
    }
    # workflow_dispatch allows at most 10, and every value must be a string.
    assert len(inputs) <= 10
    assert all(isinstance(v, str) for v in inputs.values())


def test_the_slack_context_travels_with_the_run(fake) -> None:
    """The far end has no other way to know which conversation started it."""
    client = fake(204)
    run(dispatch_workflow(**BASE))
    inputs = client.calls[0]["json"]["inputs"]
    assert inputs["slack_channel"] == "#testing"
    assert inputs["slack_user"] == "U1"


def test_the_token_is_sent_as_a_bearer_and_not_in_the_url(fake) -> None:
    client = fake(204)
    run(dispatch_workflow(**BASE))
    call = client.calls[0]
    assert call["headers"]["Authorization"] == "Bearer ghp_x"
    assert "ghp_x" not in call["url"]


# ── the workflow name is DATA, and it reaches a URL path (A2.3) ──────────────

@pytest.mark.parametrize(
    "workflow",
    ["../../../../etc/passwd", "../other-repo/x.yml", "runtests.yml/../../x",
     "", "runtests", "runtests.txt", "a" * 200 + ".yml"],
)
def test_a_workflow_name_that_is_not_a_plain_yaml_file_is_refused(
    fake, workflow: str
) -> None:
    """A stored row is a request, never an authorisation.

    `action_target` comes from a job definition an admin can edit, and it is
    interpolated into a GitHub API path — `../../` in it would address another
    repository's resources with our token. Refused before any request is made,
    which is the assertion below.
    """
    client = fake(204)
    result = run(dispatch_workflow(**{**BASE, "workflow": workflow}))
    assert not result.ok
    assert client.calls == [], "it must refuse before making a request"


def test_a_plain_yaml_name_is_allowed(fake) -> None:
    """The other direction: the guard must not refuse everything."""
    for name in ("runtests.yml", "run-tests.yaml", "a_b.c-1.yml"):
        assert WORKFLOW_FILE.match(name), name


# ── failures, and what may be said about them ───────────────────────────────

def test_a_refusal_from_github_does_not_echo_the_body_to_the_user(fake) -> None:
    """A channel is a wider audience than the person who typed the command, and
    GitHub's error bodies carry repository names and other internal detail."""
    fake(422)
    result = run(dispatch_workflow(**BASE))
    assert not result.ok
    assert "422" in result.message
    # The message names what to check, because "GitHub refused" alone sends
    # somebody to look at GitHub's status page.
    assert "input" in result.message.lower()


def test_an_unreachable_github_is_a_refusal_not_a_crash(fake) -> None:
    fake(raise_exc=httpx.ConnectError("no route"))
    result = run(dispatch_workflow(**BASE))
    assert not result.ok
    assert "Could not reach GitHub" in result.message
    # And it says the run did NOT start. "Could not reach GitHub" alone leaves
    # the user unsure whether to try again.
    assert "not been dispatched" in result.message


def test_a_network_error_does_not_publish_the_repository_to_the_channel() -> None:
    """httpx stringifies a connection error with the URL it was trying, and that
    URL contains the repository. The obvious f-string publishes your private
    repo layout to a Slack channel on a network blip.

    Raised by review; the exception text goes to `log_detail` instead.
    """
    import httpx as _httpx

    class Boom:
        async def __aenter__(self): return self
        async def __aexit__(self, *e): return False
        async def post(self, url, headers=None, json=None):
            raise _httpx.ConnectError(f"failed to connect to {url}")

    import slack_runtests.runners.github as mod
    original = mod.httpx.AsyncClient
    mod.httpx.AsyncClient = lambda **kw: Boom()
    try:
        result = run(dispatch_workflow(**BASE))
    finally:
        mod.httpx.AsyncClient = original

    assert BASE["repo"] not in result.message, result.message
    assert BASE["repo"] in result.log_detail, "the operator still needs it"


def test_no_token_is_a_DRY_RUN_that_says_what_it_would_have_done(fake) -> None:
    """Parity with slack.py's console fallback: never silently do nothing."""
    client = fake(204)
    result = run(dispatch_workflow(**{**BASE, "token": ""}))
    assert not result.ok and result.dry_run
    assert "dry-run" in result.message
    assert "runtests.yml" in result.message
    assert client.calls == [], "a dry run must make no request"


def test_the_operator_detail_and_the_user_message_are_different_fields(fake) -> None:
    """The whole reason DispatchResult has two of them."""
    fake(500)
    result = run(dispatch_workflow(**BASE))
    assert result.log_detail and result.message
    assert result.log_detail != result.message


# ── the dispatch and the workflow must agree, or every run 422s ─────────────

def test_the_inputs_sent_match_the_inputs_the_shipped_workflow_declares(fake) -> None:
    """Two files in different languages that must say the same thing.

    `workflow_dispatch` rejects an input it does not declare, so a dispatch that
    sends one extra key fails EVERY run with a 422 — and the message reads as a
    GitHub problem rather than as two files one line out of step.

    Compared as sets, in both directions:
      · sent but not declared  -> 422 on every dispatch
      · declared but not sent  -> the workflow reads an empty value, silently

    The second is the quieter failure and the reason this is not a one-way check.
    """
    import pathlib
    import yaml

    client = fake(204)
    run(dispatch_workflow(**BASE))
    sent = set(client.calls[0]["json"]["inputs"])

    root = pathlib.Path(__file__).resolve().parents[2]
    spec = yaml.safe_load((root / ".github/workflows/runtests.yml").read_text())
    # PyYAML parses a bare `on:` key as the boolean True.
    trigger = spec.get("on") or spec.get(True)
    declared = set(trigger["workflow_dispatch"]["inputs"])

    assert sent == declared, (
        f"sent but not declared: {sorted(sent - declared)}; "
        f"declared but not sent: {sorted(declared - sent)}"
    )
    # The input-set size, so an empty parse cannot pass as agreement.
    assert len(sent) >= 6, f"only {len(sent)} inputs — did the payload collapse?"
