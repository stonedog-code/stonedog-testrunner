"""Report test status back to Slack — or to the console when there is no token.

USAGE FROM A TEST
-----------------
    from slack_runtests.slack import notify, SlackNotifier

    notify("Smoke suite starting")                 # -> #testing
    notify("Deploy verified", channel="#releases")

    # For a run you want to keep updating in place:
    slack = SlackNotifier()                        # -> #testing
    slack.start("Starting webapp @ staging")
    slack.progress(passed=12, failed=0, total=50)
    slack.finish(passed=48, failed=2, skipped=0, duration=91.4)

THE CONSOLE FALLBACK IS THE POINT
---------------------------------
With no `SLACK_BOT_TOKEN` set, nothing is sent and every message is printed
saying exactly what WOULD have gone where:

    [slack:dry-run] -> #testing
      Smoke suite starting

This is not a debugging convenience bolted on afterwards; it is what makes the
library safe to import unconditionally from a test suite. The alternatives are
both bad:

  * raising when credentials are absent means every test that reports status
    fails on a laptop, so people stop calling it;
  * silently doing nothing means you cannot tell "correctly inert" from
    "misconfigured in CI and posting nowhere" — and you find out weeks later
    when someone asks why the channel went quiet.

Printing makes the inert case visible and self-explanatory.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

#: Where messages go when the caller does not say. Referenced by the API, the
#: workflow and the tests, so there is exactly one default in the system.
DEFAULT_CHANNEL = "#testing"

#: Where the Web API lives. Overridable so a test can point it at a local fake.
#:
#: A hard-coded vendor URL is untestable by construction: the only way to prove
#: what this actually puts on the wire — the JSON body, the bearer header, the
#: method — is to let something local receive it. The unit tests stub `fetch`
#: and assert on what was handed to the stub, which proves the CALL SITE and
#: nothing about the request.
#:
#: Read at call time, never captured at import, for the same reason the token is.
def _slack_api() -> str:
    return os.environ.get("SLACK_API_BASE", "https://slack.com/api").rstrip("/")


SLACK_API = "https://slack.com/api"

#: chat.update is rate limited PER METHOD PER APP. A 500-test suite editing once
#: per test spends the run being throttled — and because the limit is per app,
#: what it breaks is every other thing this bot posts, not just this run.
MIN_SECONDS_BETWEEN_EDITS = 5.0


def _token() -> str | None:
    """The bot token, or None. Read at call time, never cached at import.

    Import-time reads make the library untestable and surprise anyone who sets
    the variable after importing — which is exactly what a pytest fixture or a
    `monkeypatch.setenv` does.
    """
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    return token or None


def _normalise_channel(channel: str | None) -> str:
    """Accept '#testing', 'testing', or a channel id ('C0123ABC') alike.

    Slack's own APIs are inconsistent about the leading '#', and a caller who
    passes the wrong form gets `channel_not_found` — an error that reads like a
    permissions problem and is not.
    """
    if not channel:
        return DEFAULT_CHANNEL
    channel = channel.strip()
    if not channel:
        return DEFAULT_CHANNEL
    # A raw channel ID is passed through untouched: they start with C/G/D and
    # carry no '#'.
    if channel[0] in "CGD" and channel[1:].isalnum() and channel.isupper():
        return channel
    return channel if channel.startswith("#") else f"#{channel}"


def configured() -> bool:
    """True when a bot token is present, so messages will really be sent.

    The public name for what `SlackNotifier.enabled` answers per instance. A
    caller that wants to say something at startup has no notifier yet, and
    building a throwaway one just to read a boolean reads like a mistake.
    """
    return _token() is not None


def announce_configuration(logger: logging.Logger, channel: str | None = None) -> bool:
    """Say at STARTUP whether Slack is configured. Returns what it found.

    Every message already prints itself when there is no token, but that first
    line arrives whenever the first run happens — which may be hours after the
    process started, and is exactly when nobody is watching the console. An
    operator needs to know the service is inert at the moment they start it, not
    at the moment it silently fails to report.

    It names the console prefix and the destination too, so whoever reads the
    startup line knows what to search for later.

    One implementation, called by every component that posts, for the same
    reason `gate.py` is shared: two copies of an operator-facing warning drift,
    and the one that drifts is the one nobody reads again.
    """
    # `channel=None` is not the same as "the default channel". The test server
    # learns its channel from each job, so at startup it genuinely does not know
    # one — and naming #testing there would be a confident falsehood in the very
    # line an operator is trusting to tell them what is going on.
    where = f"to {_normalise_channel(channel)}" if channel else "to the channel each job names"

    if configured():
        logger.info("Slack configured — messages will be sent %s", where)
        return True
    logger.warning(
        "SLACK_BOT_TOKEN unset — Slack is NOT configured. Nothing will be sent %s; "
        "every message is printed to this console instead, as "
        "'[slack:dry-run] <verb> -> <channel>' followed by the text.",
        where,
    )
    return False


@dataclass(slots=True)
class SentMessage:
    """What a post produced.

    `ts` is Slack's message timestamp and doubles as its id — it is what
    `update()` needs. In dry-run mode it is a fake but stable value so calling
    code can be exercised end to end without a token.
    """

    channel: str
    ts: str
    dry_run: bool


class SlackNotifier:
    """Post and update a single status message.

    The post-then-edit shape is deliberate. Posting a new message per progress
    update turns a channel into a firehose and gets the bot muted; editing one
    message keeps the whole run in one place that stays scrollable.
    """

    def __init__(
        self,
        channel: str | None = None,
        token: str | None = None,
        stream: Any = None,
    ) -> None:
        self.channel = _normalise_channel(channel)
        self._token = token if token is not None else _token()
        self._stream = stream if stream is not None else sys.stderr
        self._ts: str | None = None
        self._last_edit = 0.0
        self._started = time.monotonic()

    @property
    def enabled(self) -> bool:
        """True when a real token is present. False means console-only."""
        return self._token is not None

    # ── low-level ────────────────────────────────────────────────────────────

    def _emit_dry_run(self, verb: str, text: str) -> None:
        """Print what would have been sent, and where.

        Goes to stderr rather than stdout so it cannot corrupt a test suite's
        machine-readable output (a --junit-xml stream, a JSON report piped to
        another tool). It is a diagnostic, not a result.
        """
        head = f"[slack:dry-run] {verb} -> {self.channel}"
        body = "\n".join(f"  {line}" for line in text.splitlines())
        print(f"{head}\n{body}", file=self._stream, flush=True)

    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST to the Slack Web API using only the standard library.

        `slack_sdk` would be the right dependency in production. It is avoided
        here so this prototype has no import that could fail on a machine that
        has not run `uv sync` — the console path must work from anywhere.
        """
        request = urllib.request.Request(
            f"{_slack_api()}/{method}",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {self._token}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = json.loads(response.read().decode())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            # NEVER let a reporting failure fail the test run. The suite's
            # verdict must not depend on whether a chat service was reachable —
            # that would turn a Slack outage into a red build.
            log.warning("Slack %s failed: %s", method, exc)
            return {"ok": False, "error": str(exc)}

        if not body.get("ok"):
            log.warning("Slack %s rejected: %s", method, body.get("error"))
        return body

    # ── public ───────────────────────────────────────────────────────────────

    def post(self, text: str, blocks: list[dict] | None = None) -> SentMessage:
        """Send a new message and remember its ts for later edits."""
        if not self.enabled:
            self._emit_dry_run("post", text)
            self._ts = self._ts or f"dry-run-{int(self._started * 1000)}"
            return SentMessage(self.channel, self._ts, dry_run=True)

        payload: dict[str, Any] = {"channel": self.channel, "text": text}
        if blocks:
            payload["blocks"] = blocks
        body = self._call("chat.postMessage", payload)
        self._ts = body.get("ts") or self._ts or ""
        return SentMessage(self.channel, self._ts, dry_run=False)

    def update(
        self, text: str, blocks: list[dict] | None = None, force: bool = False
    ) -> SentMessage | None:
        """Edit the message this notifier already posted.

        Returns None when the edit was skipped by the rate limiter, so a caller
        can tell "throttled" from "sent". Silently swallowing that distinction
        is how a progress display appears stuck.
        """
        if self._ts is None:
            # No message to edit yet. Posting instead is friendlier than raising
            # — a caller who only ever calls update() still gets output.
            return self.post(text, blocks)

        now = time.monotonic()
        if not force and now - self._last_edit < MIN_SECONDS_BETWEEN_EDITS:
            return None
        self._last_edit = now

        if not self.enabled:
            self._emit_dry_run("update", text)
            return SentMessage(self.channel, self._ts, dry_run=True)

        payload: dict[str, Any] = {"channel": self.channel, "ts": self._ts, "text": text}
        if blocks:
            payload["blocks"] = blocks
        self._call("chat.update", payload)
        return SentMessage(self.channel, self._ts, dry_run=False)

    # ── convenience for a test run ───────────────────────────────────────────

    def start(self, text: str) -> SentMessage:
        self._started = time.monotonic()
        return self.post(text)

    def progress(self, passed: int, failed: int, total: int) -> SentMessage | None:
        done = passed + failed
        bar = f"{done}/{total}" if total else str(done)
        return self.update(f"Running… {bar}  ✅ {passed}  ❌ {failed}")

    def finish(
        self,
        passed: int,
        failed: int,
        skipped: int = 0,
        duration: float | None = None,
        run_url: str | None = None,
        failed_ids: list[str] | None = None,
    ) -> SentMessage | None:
        """Post the final summary.

        Deliberately a SUMMARY, never the suite's stdout. A run's output pasted
        into a channel is how a channel gets muted, and it is also how internal
        hostnames and stack traces end up in a searchable, wide-audience place.
        Counts and a link; the artifact holds the detail.
        """
        icon = "✅" if failed == 0 else "❌"
        parts = [f"{icon} {passed} passed", f"{failed} failed"]
        if skipped:
            parts.append(f"{skipped} skipped")
        if duration is not None:
            parts.append(f"in {duration:.1f}s")
        text = "  ·  ".join(parts)
        if failed_ids:
            shown = ", ".join(failed_ids[:5])
            more = f" (+{len(failed_ids) - 5} more)" if len(failed_ids) > 5 else ""
            text += f"\nFailed: {shown}{more}"
        if run_url:
            text += f"\n<{run_url}|run log>"
        return self.update(text, force=True)


def notify(text: str, channel: str | None = None) -> SentMessage:
    """One-shot message. The simplest thing a test can call.

    Builds a notifier per call on purpose: a module-level singleton would cache
    the channel and the token from whichever test ran first, which is precisely
    the cross-test coupling that makes a suite order-dependent.
    """
    return SlackNotifier(channel=channel).post(text)
