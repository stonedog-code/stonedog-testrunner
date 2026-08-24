"""The four checks every Slack slash command passes, in one place.

This used to live inline in `api.py`. It was moved here the moment a second
deployable (the edge server) needed the same checks, because two copies of an
authorisation boundary is how one of them gets fixed and the other does not.
There is exactly one implementation and both callers import it.

The order matters and is not arbitrary:

    1. signature  — is this from Slack at all?          (cryptography)
    2. workspace  — is it from *our* Slack?             (team_id)
    3. identity   — is this person allowed, here?       (channel, user)
    4. wording    — is what they typed on the allowlist? (argparse choices)

Cheapest and most conclusive first. There is no point parsing the arguments of
a request that was never signed, and no point telling an unauthorised person
that their flags were wrong.
"""

from __future__ import annotations

import argparse
import logging
import urllib.parse
from dataclasses import dataclass, field
from typing import Mapping

from . import signature
from .parsing import Grammar, SlackArgError, parse

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Outcome:
    """What the gate decided, and what the caller should say.

    `status` is HTTP. Note that 200 is the normal answer for a *rejected*
    command: Slack's contract is that a user error is a 200 with an ephemeral
    body, not an HTTP error. Only a failed signature gets a real 4xx, because
    that is the one case where the sender is not a person to be helped.
    """

    ok: bool
    status: int = 200
    message: str | None = None
    args: argparse.Namespace | None = None
    form: dict[str, str] = field(default_factory=dict)
    #: Set when the request was accepted without its signature being checked.
    #: The caller must surface this; see `verify_signatures` below.
    unverified: bool = False


def check(
    body: bytes,
    headers: Mapping[str, str],
    *,
    signing_secret: str,
    allowed_team: str = "",
    allowed_channels: frozenset[str] = frozenset(),
    allowed_users: frozenset[str] = frozenset(),
    grammar: Grammar | None = None,
) -> Outcome:
    """Run all four checks over the RAW request body.

    `body` must be the bytes as received. The signature is computed over
    exactly those bytes, so parsing the form first and re-encoding it produces
    a mismatch for reasons that are invisible in a debugger — and, worse, opens
    a gap between the fields that were signed and the fields being authorised
    on. Both problems disappear by never re-encoding: this function verifies
    the bytes and then parses those same bytes.
    """
    # ── 1. Is this from Slack at all? ────────────────────────────────────────
    unverified = False
    if signing_secret:
        if not signature.is_valid(
            body,
            headers.get("X-Slack-Request-Timestamp", ""),
            headers.get("X-Slack-Signature", ""),
            signing_secret,
        ):
            return Outcome(ok=False, status=401, message="bad signature")
    else:
        # A development affordance, kept deliberately loud. See the README:
        # with no secret configured every request is accepted and every one is
        # logged as unverified, so "correctly inert" cannot be confused with
        # "quietly insecure". A real deployment sets the secret and this branch
        # becomes unreachable.
        unverified = True
        log.warning("SLACK_SIGNING_SECRET unset — request accepted UNVERIFIED")

    form = dict(urllib.parse.parse_qsl(body.decode("utf-8", errors="replace")))

    # ── 2/3. Is this person allowed, here, in this workspace? ────────────────
    # Three separate checks and all three are needed. The signature proves the
    # request came from Slack; only team_id proves it came from YOUR Slack; and
    # workspace membership is not an entitlement — any real workspace contains
    # guests, contractors and Slack Connect users from a customer.
    if allowed_team and form.get("team_id") != allowed_team:
        return Outcome(ok=False, message="This command is not available in this workspace.", form=form)
    if allowed_channels and form.get("channel_id") not in allowed_channels:
        return Outcome(ok=False, message="This command is not enabled in this channel.", form=form)
    if allowed_users and form.get("user_id") not in allowed_users:
        return Outcome(ok=False, message="You are not on the test-automation allowlist.", form=form)

    # ── 4. Is the wording on the allowlist? ──────────────────────────────────
    # `grammar` has no permissive default. An omitted argument produces an empty
    # Grammar, which `parse` refuses outright -- a caller that forgets to pass
    # the allowlists gets a refusal, never a wildcard. The alternative, defaulting
    # to "everything", is the exact shape of the bug NEH-1119 fixed one layer up.
    grammar = grammar if grammar is not None else Grammar.of((), (), ())
    try:
        args = parse(str(form.get("text", "")), grammar)
    except SlackArgError as error:
        return Outcome(
            ok=False,
            message=f"`/runtests`: {error}\n{grammar.usage_hint()}",
            form=form,
        )

    return Outcome(ok=True, args=args, form=form, unverified=unverified)
