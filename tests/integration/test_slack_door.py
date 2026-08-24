"""The three requests that define the Slack door, against a real server.

    1. A valid test origin, valid structure     -> accepted
    2. An invalid origin                        -> rejected
    3. A valid origin, invalid structure        -> refused

Together they pin the two halves of the boundary. The first proves the door
opens for the traffic it is for — a check that matters more than it looks,
because a door that never opens also passes tests 2 and 3. The second proves it
is shut to anything that cannot prove it is Slack. The third proves that proving
you are Slack is *not enough*: what you typed still has to be on the allowlist.

WHAT "ORIGIN" MEANS HERE, AND WHAT IT DOES NOT

Test 2 sends a request signed with the wrong secret — a sender that cannot prove
it is Slack at all. There is a second, different notion of a bad origin: a
correctly-signed request from *another Slack workspace*, which the edge refuses
on `team_id`. That is a distinct control with a distinct failure shape (an
ephemeral refusal, not a 401) and it has its own unit test; it is called out
here so nobody reads these three as covering it.
"""

from __future__ import annotations

import time

import pytest
from harness import post, slack_body

pytestmark = pytest.mark.integration


# ── 1 ────────────────────────────────────────────────────────────────────────

def test_valid_origin_and_valid_structure_is_accepted(edge) -> None:
    """A genuine request from the allowlisted workspace, channel and user."""
    response = post(edge, slack_body("-p webapp -s staging"))

    assert response.status_code == 200, response.text
    payload = response.json()
    # Ephemeral: an acknowledgement belongs to the person who typed the command,
    # not to everyone in the channel. The *result* is the opposite, and a test
    # server posts that.
    assert payload["response_type"] == "ephemeral"
    assert "Queued" in payload["text"]
    assert "webapp" in payload["text"] and "staging" in payload["text"]


# ── 2 ────────────────────────────────────────────────────────────────────────

def test_invalid_origin_is_rejected(edge) -> None:
    """Correctly formed, correctly addressed — and signed by the wrong party.

    401 rather than a friendly message, and deliberately so: a sender that
    cannot prove it is Slack is not a person to help, and telling it why it
    failed is telling it how to succeed.
    """
    response = post(edge, slack_body("-p webapp -s staging"), secret="not-the-real-signing-secret")

    assert response.status_code == 401
    body = response.text.lower()
    # The refusal must not leak the shape of the check. Nothing about the real
    # secret, the expected signature, or the allowlists.
    assert "not-the-real-signing-secret" not in body
    assert "queued" not in body


# ── 3 ────────────────────────────────────────────────────────────────────────

def test_valid_origin_with_invalid_structure_is_refused(edge) -> None:
    """Genuinely from Slack, and asking for something that is not allowed.

    `-p ../../etc` is a path traversal and `-s prod` is an environment that is
    deliberately absent from the allowlist. Both are refused by the parser
    before any value reaches a path or a runner.

    NOTE THE STATUS CODE. This asserts 200, not 4xx, and that is not a
    concession: Slack's contract is that a user error is a 200 with an
    ephemeral body. Returning an HTTP error would make Slack show its own
    generic "dispatch_failed" instead of the reason the command was wrong —
    which is unhelpful exactly when the user most needs help. So the assertion
    that matters is on the body.
    """
    response = post(edge, slack_body("-p ../../etc -s prod"))

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["response_type"] == "ephemeral"
    text = payload["text"]

    assert "Queued" not in text, "a refused command must not have been queued"
    assert "/runtests" in text
    assert "Try:" in text, "a refusal should show the usage hint"
    # The message names the allowed products, which is what makes it useful
    # rather than merely correct. It does echo the rejected value back, and
    # that is fine HERE and only here: the reply is ephemeral, so the only
    # person who sees their own input reflected is the person who typed it.
    # The same string in a channel message would be a different question.
    assert "webapp" in text and "billing" in text


# ── 4. the command must MATCH A JOB DEFINITION (A2.2.2) ─────────────────────
#
# Proving you are Slack is not enough, and being on the allowlist is not enough
# either. The allowlist says a value MAY be named; a job definition says what
# happens when it is. These four cases are the difference.


def test_an_allowlisted_command_with_no_matching_job_is_refused(edge) -> None:
    """`catalog` is on the product allowlist; no job is defined for `full`.

    This is the case the allowlist alone cannot catch — every token is
    permitted, and there is still nothing to run.
    """
    response = post(edge, slack_body("-p catalog -s local"))
    assert response.status_code == 200, response.text
    text = response.json()["text"]
    assert "No job matches" in text
    assert "Queued" not in text, "it must not queue a job it could not resolve"


def test_the_refusal_names_what_WOULD_have_matched(edge) -> None:
    """A2.2.2 requires the suggestion, and it is the whole difference between a
    refusal somebody can act on and one that reads as the bot being broken.

    Asserted on CONTENT, not merely on being a refusal: "No job matches" with an
    empty suggestion list satisfies the test above and helps nobody.
    """
    response = post(edge, slack_body("-p webapp -s nowhere --test_scope smoke"))
    text = response.json()["text"]
    # `nowhere` is not on the server allowlist, so this is refused by the
    # grammar before resolution — and the grammar names the allowed values.
    assert "nowhere" in text and "choose from" in text


def test_a_near_miss_on_one_token_is_suggested_by_name(edge) -> None:
    """Differing in exactly ONE token is a suggestion, and it names the job.

    `catalog` on `local` is undefined; `catalog` on `staging` and on `dev` are
    defined and differ in one token. Asserted on CONTENT — "No job matches" with
    an empty suggestion list would satisfy the previous test and help nobody.
    """
    response = post(edge, slack_body("-p catalog -s local"))
    text = response.json()["text"]
    assert "Did you mean" in text, text
    assert "catalog" in text
    # The suggestion is the CANONICAL flag form, which is what a person reads in
    # a definition even though positionals are what they type.
    assert "--product catalog" in text


def test_a_resolved_job_still_goes_through_the_queue(edge) -> None:
    """The `test-server` action kind is unchanged behaviour.

    v1 and v2 differ by a ROW, not a deployment: this job's action is
    `test-server`, so it queues exactly as it always did. A `gh-action` job
    dispatches instead, and that path is unit-tested with the HTTP call faked —
    an integration test of it would either hit the real GitHub API or prove
    nothing.
    """
    response = post(edge, slack_body("-p webapp -s dev --test_scope smoke"))
    text = response.json()["text"]
    assert "Queued" in text
    assert "webapp" in text and "dev" in text


# ── 5. a gh-action job must not run twice from one command ──────────────────


def test_a_gh_action_command_is_acknowledged_without_waiting(edge) -> None:
    """The ack, and the fact that it is an ack rather than a result.

    The dispatch happens in a background task so the reply is inside Slack's
    three-second budget however slow GitHub is. What comes back names the job
    and the correlation id; the RESULT is posted later, by the workflow.
    """
    response = post(edge, slack_body("-p billing -s local"))
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["response_type"] == "ephemeral"
    assert "Dispatching" in payload["text"], payload["text"]
    assert "gh dispatch 1" in payload["text"], "the job's name, so the user knows which ran"


def test_slacks_retry_does_not_dispatch_the_workflow_a_second_time(edge) -> None:
    """THE BUG THIS EXISTS FOR, found in review.

    Slack retries any command it does not hear back from in three seconds, and
    the first version of the gh-action branch returned BEFORE reaching the
    store — so nothing refused a repeat, and a slow GitHub answer meant one
    command ran the suite TWICE.

    The fix is to record the dispatch before making it: `job_id` is derived from
    `trigger_id`, so the PRIMARY KEY refuses the second. This sends the SAME
    trigger_id twice, which is exactly what a Slack retry is.
    """
    # Its OWN (product, server): the cap is 1 active per pair, so sharing one
    # with the test above would get this refused by the cap before it could
    # reach the duplicate path.
    body = slack_body("-p webapp -s local", trigger_id="retry-me-once")

    first = post(edge, body)
    second = post(edge, body)

    assert "Dispatching" in first.json()["text"], first.json()["text"]
    # The second is refused as a duplicate, NOT acknowledged again. A second
    # "Dispatching" here would mean two workflow runs from one command.
    assert "already queued" in second.json()["text"], second.json()["text"]
    assert "Dispatching" not in second.json()["text"]


# ── 6. a dispatch that never happened must not read as queued (NEH-1156) ────


def test_results_reports_a_run_that_never_started(edge) -> None:
    """THE POINT OF NEH-1156, proven where a user would see it.

    The dispatch happens in a background task, AFTER the ack has gone — so a
    GitHub-side failure has no reply to travel back on, and the edge holds no
    bot token to correct it with. Logging it was the whole of the old behaviour,
    and a log is somewhere the person waiting cannot look: the run simply never
    reported, which is silence reading as success.

    This edge has no GITHUB_TOKEN, so the dispatch is a dry run — it makes no
    request and reports not-ok, which is exactly the shape of a refused
    dispatch. `results` must then say the run never started.

    Asserted on what `results` SAYS, not on the log. A test reading the log
    would pass over a message nobody sees, which is the defect rather than
    the fix.
    """
    post(edge, slack_body("-p billing -s local", trigger_id="never-started-1"))

    # `-s` is required on `results` too, because the grammar is one parser for
    # every action even though `results` only reads the product. That is friction
    # rather than a defect, and it is NEH-1166.

    # The dispatch runs after the response, so the state lands a moment later.
    for _ in range(50):
        text = post(edge, slack_body("results -p billing -s local")).json()["text"]
        if "never started" in text:
            break
        time.sleep(0.1)

    assert "never started" in text, text
    # And it does NOT read as a test failure. Somebody told "failed" goes
    # looking for a broken test that does not exist.
    assert "failed" not in text.lower(), text


def test_a_queued_test_server_job_is_not_reported_as_never_started(edge) -> None:
    """The other direction. A job waiting for a runner is still going to run,
    and reporting it as never started would be the opposite lie."""
    # `catalog`, not `webapp`: `last_for` looks up by PRODUCT ALONE, so a
    # product that also has a gh-action job returns whichever run was most
    # recent regardless of server. Using one here made this assert against the
    # other test's dry-run dispatch — a fixture collision that reads as a bug in
    # the code under test.
    post(edge, slack_body("-p catalog -s staging", trigger_id="still-queued-1"))
    text = post(edge, slack_body("results -p catalog -s staging")).json()["text"]
    assert "never started" not in text, text
    assert "queued" in text, text
