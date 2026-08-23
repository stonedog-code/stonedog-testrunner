"""The startup gate: refuse to serve a public endpoint that protects nothing.

WHAT WAS WRONG, AND WHY IT READ AS RIGHT

`gate.py` checks the workspace, the channel and the user like this:

    if allowed_channels and form.get("channel_id") not in allowed_channels:

An empty allowlist is falsy, so the check is skipped — **an unset allowlist
allowed everyone**. The same was true of the signing secret: with none set,
every request was accepted unverified and a warning was logged per request.

Both were deliberate development affordances and both were documented. What was
missing is the other half: nothing distinguished "this is a lab" from "somebody
deployed this and forgot". A per-request warning in a log nobody reads is not a
control, and the failure it hides is the worst one this system has — a slash
command from any workspace, any channel and any user who can reach the URL,
running a suite on a machine inside your network.

THE SHAPE OF THE FIX, AND WHY IT IS ONE FLAG RATHER THAN THREE

The process refuses to start unless it is genuinely protecting something, and
the escape hatch is a single variable whose name is meant to be uncomfortable in
a production configuration file:

    RUNTESTS_INSECURE_DEV=1

One flag rather than one per affordance, because they are not three independent
decisions — they are one statement about what this process is. A deployment that
had to opt out of three separate checks would opt out of them one at a time,
each for a good local reason, and arrive at the same place without ever making
the decision.

WHAT IT DOES NOT DO

It does not change `gate.py`. The per-request checks are unchanged, and this
adds no new branch to the request path — a startup refusal cannot be reached by
a request, so it cannot be got wrong per request. The two live at different
times on purpose: this one answers "should this process exist", and gate answers
"is this caller allowed".
"""

from __future__ import annotations

import logging
import os
from typing import Iterable

#: The one variable that says "this process is a lab and protects nothing".
#: Named to read badly in a deployment: somebody reviewing a production
#: configuration should trip over it.
INSECURE_DEV_ENV = "RUNTESTS_INSECURE_DEV"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def insecure_dev(environ: dict[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(INSECURE_DEV_ENV, "").strip().lower() in _TRUTHY


def missing_protections(
    *,
    signing_secret: str,
    allowed_team: str,
    allowed_channels: Iterable[str],
    allowed_users: Iterable[str],
) -> list[str]:
    """Everything that must be configured before this may face the internet.

    Returned as a list rather than a bool so the refusal can name all of them at
    once. Told about one missing variable at a time, an operator restarts three
    times and learns nothing about the shape of what they are configuring.
    """
    missing: list[str] = []

    if not signing_secret:
        # Without this, anything that can reach the URL can pretend to be Slack.
        # Every other check below is about WHICH Slack caller is allowed, and
        # they are all worthless if the caller is not Slack at all.
        missing.append(
            "SLACK_SIGNING_SECRET — without it every request is accepted "
            "unverified, so nothing proves a command came from Slack at all"
        )

    if not allowed_team:
        # A valid Slack signature proves the request came from Slack. It does
        # not prove it came from YOUR Slack.
        missing.append(
            "SLACK_TEAM_ID — a valid signature proves a request came from "
            "Slack, not that it came from your workspace"
        )

    if not list(allowed_channels) and not list(allowed_users):
        # One or the other, not both: a small team may reasonably allow any
        # channel and restrict people, or the reverse. Requiring both would be
        # a rule people work around rather than follow.
        missing.append(
            "RUNTESTS_CHANNELS or RUNTESTS_USERS (at least one) — workspace "
            "membership is not an entitlement; a workspace contains guests, "
            "contractors and Slack Connect users from a customer"
        )

    return missing


def refuse_or_warn(log: logging.Logger, *, signing_secret: str, allowed_team: str,
                   allowed_channels: Iterable[str], allowed_users: Iterable[str],
                   environ: dict[str, str] | None = None,
                   warn: bool = True) -> str | None:
    """Called once at startup. Returns a refusal message, or None to proceed.

    Returning the message rather than exiting keeps this testable without a
    subprocess, and keeps the decision to exit where a reader expects it — in
    the entry point.

    `warn=False` for the entry-point call. Both `main()` and the app's lifespan
    ask, because either can be the way this process was started — but the
    lifespan runs in BOTH cases, so it is the one that announces. Left to warn
    from both, a `run.sh` start logs the same eight lines twice, and a warning
    printed twice is a warning read once and then filtered.
    """
    missing = missing_protections(
        signing_secret=signing_secret,
        allowed_team=allowed_team,
        allowed_channels=allowed_channels,
        allowed_users=allowed_users,
    )

    if not missing:
        return None

    if insecure_dev(environ):
        if not warn:
            return None
        # Loud, and on every start rather than per request. A warning that
        # appears once per request is one nobody reads; a warning at startup is
        # in the first screen of every deployment's logs.
        log.warning(
            "%s is set — THIS PROCESS IS NOT PROTECTING ANYTHING. %d protection(s) "
            "are absent and are being ignored:", INSECURE_DEV_ENV, len(missing),
        )
        for item in missing:
            log.warning("  · %s", item)
        return None

    return _refusal_text(missing)


def _refusal_text(missing: list[str]) -> str:
    lines = [
        "REFUSING TO START: this process would answer the internet without "
        "protecting anything.",
        "",
        f"{len(missing)} required setting(s) are missing:",
        "",
    ]
    lines += [f"  · {item}" for item in missing]
    lines += [
        "",
        "Set them, or — only on a machine where this protects nothing and reaches "
        "nothing —",
        f"set {INSECURE_DEV_ENV}=1 to start anyway. Do not set it in a deployment.",
    ]
    return "\n".join(lines)


__all__ = ["INSECURE_DEV_ENV", "insecure_dev", "missing_protections", "refuse_or_warn"]
