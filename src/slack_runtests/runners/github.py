"""V2 — dispatch a GitHub Actions workflow instead of running anything here.

WHY THIS EXISTS, AND IT IS THE MOST IMPORTANT DESIGN DECISION IN THE PROJECT:

    The public API never runs a test.

It authenticates, authorises, validates, dispatches, and answers. Everything
that touches your network happens on a self-hosted runner that POLLS GitHub, so
it needs no inbound connectivity and no port open to the internet. In V1 the
process answering the internet is the process running the tests; here they are
different machines, and the one on your network only ever calls out.

You also inherit logs, artifacts, retention, concurrency limits and an approval
gate without writing any of them.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx

from ..slack import SlackNotifier

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """What happened, split into what a user may read and what only a log may.

    The two are separated on purpose. `message` goes to a Slack channel, which
    is a wider audience than the person who typed the command; GitHub's error
    bodies carry repository names and other internal detail, so they go to
    `log_detail` and no further. Getting that backwards is how a refusal
    explains your private repo layout to a room.
    """

    ok: bool
    message: str
    log_detail: str = ""
    dry_run: bool = False


#: A workflow file name is DATA — it comes from a stored job definition, and it
#: is interpolated into a GitHub API path. `../../` in it would address another
#: repository's resources with our token.
#:
#: A2.3 draws the line here: WHICH REPOS may be dispatched to is code/env and
#: never a row; only which workflow INSIDE an allowlisted repo may be data. This
#: is the check that keeps the second half of that true.
WORKFLOW_FILE = re.compile(r"^[A-Za-z0-9._-]{1,100}\.ya?ml$")


async def dispatch_workflow(
    *,
    repo: str,
    workflow: str,
    ref: str,
    token: str,
    correlation_id: str,
    product: str,
    server: str,
    test_scope: str = "",
    select: str | None,
    marker: str | None,
    slack_channel: str,
    slack_user: str,
) -> DispatchResult:
    """Fire a `workflow_dispatch` and say what happened. Posts nothing.

    Split out of `dispatch()` so the EDGE can use it. The edge is the
    internet-facing component and deliberately holds no Slack bot token — a
    compromised public endpoint must not be able to post as the bot — so it
    needs the outcome as a value rather than as a side effect.

    Pass the channel and the user through as INPUTS. It is easy to design this
    whole chain and never notice that the reporter at the far end has no idea
    which conversation started it — the channel id is in the Slack payload and
    nowhere else.
    """
    if not WORKFLOW_FILE.match(workflow or ""):
        # Refused before it reaches a URL. A stored row is a request, never an
        # authorisation (A2.3), and this row's value ends up in a path.
        return DispatchResult(
            ok=False,
            message=f"`{workflow}` is not a valid workflow file name.",
            log_detail=f"refused workflow name {workflow!r}",
        )

    if not (repo and token):
        # Dry-run parity with slack.py: say exactly what would have happened
        # rather than failing or, worse, silently doing nothing.
        return DispatchResult(
            ok=False,
            dry_run=True,
            message=(
                "[github:dry-run] would dispatch "
                f"`{workflow}` on `{repo or '<unset repo>'}@{ref}` "
                f"for `{product}` @ `{server}` (id `{correlation_id}`)"
            ),
            log_detail="GITHUB_REPO/GITHUB_TOKEN unset — not dispatching",
        )

    inputs = {
        # workflow_dispatch allows at most 10 inputs and every value must be a
        # string — an int here is a 422 that reads as a schema problem.
        "correlation_id": correlation_id,
        "product": product,
        "server": server,
        "test_scope": test_scope,
        "select": select or "",
        "marker": marker or "",
        "slack_channel": slack_channel,
        "slack_user": slack_user,
    }

    url = f"{GITHUB_API}/repos/{repo}/actions/workflows/{workflow}/dispatches"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json={"ref": ref, "inputs": inputs},
            )
    except httpx.HTTPError as exc:
        # The exception is NOT put in the user-facing message. httpx stringifies
        # a connection error with the URL it was trying, and that URL contains
        # the repository -- so the obvious `f"...: {exc}"` publishes your private
        # repo layout to a Slack channel on a network blip. Raised by review.
        return DispatchResult(
            ok=False,
            message="Could not reach GitHub to start the run. It has not been dispatched.",
            log_detail=f"dispatch failed: {type(exc).__name__}: {exc}",
        )

    if response.status_code != 204:
        # The body is NOT echoed to the user -- see DispatchResult. A 422 here
        # is usually the workflow not declaring one of the inputs above, which
        # is worth saying, because the alternative reading is "GitHub is broken".
        return DispatchResult(
            ok=False,
            message=(
                f"GitHub refused the run (HTTP {response.status_code}). "
                "Check that the workflow exists on that ref and declares every "
                "input this sends."
            ),
            log_detail=f"dispatch rejected {response.status_code}: {response.text[:400]}",
        )

    # ── The thing this call does NOT give you ────────────────────────────────
    # `workflow_dispatch` answers 204 No Content with NO RUN ID, so the API
    # cannot tell the user where their run is.
    #
    # The tempting fix — poll GET /actions/runs until it appears — is a trap
    # twice over: that listing is eventually consistent so the first call
    # usually finds nothing, and waiting for it here breaches Slack's
    # three-second budget, which makes Slack retry, which queues a SECOND
    # identical run.
    #
    # So do not correlate from the API at all. Let the runner introduce itself:
    # it knows its own run id, and the first thing it posts carries the link.
    return DispatchResult(
        ok=True,
        message=f"⏳ Queued `{product}` on `{server}` — the runner will post here (`{correlation_id}`).",
    )


async def dispatch(
    *,
    repo: str,
    workflow: str,
    ref: str,
    token: str,
    correlation_id: str,
    product: str,
    server: str,
    test_scope: str = "",
    select: str | None,
    marker: str | None,
    slack_channel: str,
    slack_user: str,
) -> bool:
    """`dispatch_workflow`, plus posting the outcome to Slack.

    The STANDALONE server's entry point. It keeps the posting because it is the
    component that owns the conversation; the edge uses `dispatch_workflow`
    directly and reports through its ephemeral reply instead.
    """
    result = await dispatch_workflow(
        repo=repo, workflow=workflow, ref=ref, token=token,
        correlation_id=correlation_id, product=product, server=server,
        test_scope=test_scope, select=select, marker=marker,
        slack_channel=slack_channel, slack_user=slack_user,
    )
    if result.log_detail:
        log.warning("%s", result.log_detail)
    SlackNotifier(channel=slack_channel).post(result.message)
    return result.ok
