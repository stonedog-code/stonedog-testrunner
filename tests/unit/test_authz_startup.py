"""The startup gate, asserted in BOTH directions.

A guard that has only ever been observed passing has not been tested, it has
been run. So every case here has a partner: a configured process starts, and an
unconfigured one does not; the opt-out lets it start, and its absence does not.

The defect this exists for is worth stating once more, because it read as
correct for as long as it existed: `gate.py` skips a check whose allowlist is
empty, so **an unset allowlist allowed everyone**. That is the opposite of what
an empty allowlist looks like it means, and nothing anywhere said so.
"""

from __future__ import annotations

import logging

import pytest

from slack_runtests.authz import (
    INSECURE_DEV_ENV, insecure_dev, missing_protections, refuse_or_warn,
)

pytestmark = pytest.mark.unit

CONFIGURED = {
    "signing_secret": "s3cr3t",
    "allowed_team": "T_ALLOWED",
    "allowed_channels": frozenset({"C_ALLOWED"}),
    "allowed_users": frozenset(),
}


# ── what counts as protected ─────────────────────────────────────────────────

def test_a_configured_process_is_missing_nothing() -> None:
    """The positive control. Without it every assertion below is trivially true
    of a function that always reports everything as missing."""
    assert missing_protections(**CONFIGURED) == []


def test_an_unconfigured_process_is_missing_all_three() -> None:
    missing = missing_protections(
        signing_secret="", allowed_team="", allowed_channels=(), allowed_users=(),
    )
    assert len(missing) == 3
    joined = " ".join(missing)
    assert "SLACK_SIGNING_SECRET" in joined
    assert "SLACK_TEAM_ID" in joined
    assert "RUNTESTS_CHANNELS" in joined and "RUNTESTS_USERS" in joined


@pytest.mark.parametrize(
    "override,expected",
    [
        ({"signing_secret": ""}, "SLACK_SIGNING_SECRET"),
        ({"allowed_team": ""}, "SLACK_TEAM_ID"),
        ({"allowed_channels": frozenset()}, "RUNTESTS_CHANNELS"),
    ],
)
def test_each_protection_is_checked_on_its_own(override: dict, expected: str) -> None:
    """One missing setting must be found even when the others are present.

    Checked separately because a single `if not (a and b and c)` would pass this
    file's other tests while being unable to say WHICH one is absent — and an
    operator told only "misconfigured" restarts three times learning nothing.
    """
    missing = missing_protections(**{**CONFIGURED, **override})
    assert len(missing) == 1
    assert expected in missing[0]


def test_users_alone_is_enough_and_so_is_channels_alone() -> None:
    """One or the other, not both.

    A small team may reasonably allow any channel and restrict people, or the
    reverse. A rule that demanded both would be worked around rather than
    followed, and a worked-around rule protects nothing while looking strict.
    """
    assert missing_protections(**{**CONFIGURED, "allowed_channels": frozenset(),
                                  "allowed_users": frozenset({"U1"})}) == []
    assert missing_protections(**{**CONFIGURED, "allowed_channels": frozenset({"C1"}),
                                  "allowed_users": frozenset()}) == []


# ── the refusal itself ───────────────────────────────────────────────────────

def test_an_unconfigured_process_is_refused(caplog: pytest.LogCaptureFixture) -> None:
    refusal = refuse_or_warn(
        logging.getLogger("t"), signing_secret="", allowed_team="",
        allowed_channels=(), allowed_users=(), environ={},
    )
    assert refusal is not None
    assert "REFUSING TO START" in refusal


def test_a_configured_process_is_not_refused() -> None:
    """The other direction. A gate that refuses everything is not a gate."""
    assert refuse_or_warn(logging.getLogger("t"), **CONFIGURED, environ={}) is None


def test_the_refusal_names_every_missing_setting_at_once() -> None:
    """Three restarts to learn three things is how a person concludes the tool
    is broken rather than that their configuration is incomplete."""
    refusal = refuse_or_warn(
        logging.getLogger("t"), signing_secret="", allowed_team="",
        allowed_channels=(), allowed_users=(), environ={},
    )
    assert refusal is not None
    for name in ("SLACK_SIGNING_SECRET", "SLACK_TEAM_ID", "RUNTESTS_CHANNELS"):
        assert name in refusal


def test_the_refusal_says_how_to_start_anyway() -> None:
    """A refusal with no way past it is one somebody works around by editing
    the source, which is worse than the opt-out it refused to mention."""
    refusal = refuse_or_warn(
        logging.getLogger("t"), signing_secret="", allowed_team="",
        allowed_channels=(), allowed_users=(), environ={},
    )
    assert INSECURE_DEV_ENV in refusal


# ── the opt-out ──────────────────────────────────────────────────────────────

def test_the_opt_out_starts_the_process_and_says_what_it_ignored(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        refusal = refuse_or_warn(
            logging.getLogger("t"), signing_secret="", allowed_team="",
            allowed_channels=(), allowed_users=(),
            environ={INSECURE_DEV_ENV: "1"},
        )

    assert refusal is None
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "NOT PROTECTING ANYTHING" in logged
    # Every ignored protection is named, not just counted. "3 protections
    # ignored" tells an operator nothing they can act on.
    for name in ("SLACK_SIGNING_SECRET", "SLACK_TEAM_ID", "RUNTESTS_CHANNELS"):
        assert name in logged


def test_the_opt_out_is_silent_when_nothing_is_missing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Set on a fully configured process, it has nothing to warn about — and a
    warning with no missing protection behind it is the noise that teaches
    people to ignore this line."""
    with caplog.at_level(logging.WARNING):
        assert refuse_or_warn(
            logging.getLogger("t"), **CONFIGURED, environ={INSECURE_DEV_ENV: "1"}
        ) is None
    assert caplog.records == []


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_the_opt_out_accepts_what_a_person_would_type(value: str) -> None:
    assert insecure_dev({INSECURE_DEV_ENV: value}) is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", " "])
def test_anything_else_is_not_the_opt_out(value: str) -> None:
    """`RUNTESTS_INSECURE_DEV=false` must mean false.

    A truthiness check on the mere presence of the variable would read
    `=false` as "on" — which is how somebody disables a thing and gets the
    opposite, in the one place where the opposite is an open public endpoint.
    """
    assert insecure_dev({INSECURE_DEV_ENV: value}) is False


def test_an_absent_variable_is_not_the_opt_out() -> None:
    assert insecure_dev({}) is False


def test_the_entry_point_call_does_not_duplicate_the_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`main()` and the lifespan both ask; only one of them announces.

    The lifespan runs whichever way the process was started, so it is the one
    that warns. Left to warn from both, `bash run.sh edge` prints the same eight
    lines twice — and a warning printed twice is a warning read once and then
    filtered out.
    """
    with caplog.at_level(logging.WARNING):
        assert refuse_or_warn(
            logging.getLogger("t"), signing_secret="", allowed_team="",
            allowed_channels=(), allowed_users=(),
            environ={INSECURE_DEV_ENV: "1"}, warn=False,
        ) is None
    assert caplog.records == []


def test_suppressing_the_warning_does_not_suppress_the_refusal() -> None:
    """`warn=False` is about noise, not about permission.

    A flag that quietly turned the gate off for one of its two callers would be
    the whole defect back again, reachable from the entry point everybody uses.
    """
    refusal = refuse_or_warn(
        logging.getLogger("t"), signing_secret="", allowed_team="",
        allowed_channels=(), allowed_users=(), environ={}, warn=False,
    )
    assert refusal is not None and "REFUSING TO START" in refusal
