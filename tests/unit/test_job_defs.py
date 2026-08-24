"""The job-definition model: shape rules and the near-miss suggestion (A2.2).

The STORE behaviour — uniqueness, exact matching, ordering — is in the
conformance tier, because it has to hold on both backends and only that tier
runs against both. What is here is the part that is pure: what makes a
definition well-formed, and what a no-match refusal should suggest.
"""

from __future__ import annotations

import dataclasses

import pytest

from slack_runtests.store import ActionKind, JobDef, StoreError, near_misses, validate_job_def

pytestmark = pytest.mark.unit


def make(**over) -> JobDef:
    base = dict(
        id="jd-1", name="alpha smoke", description="",
        product="alpha", test_scope="smoke", server="sandbox",
        action_kind=ActionKind.GH_ACTION.value, action_target="alpha_smoke.yml",
    )
    base.update(over)
    return JobDef(**base)


# ── shape ────────────────────────────────────────────────────────────────────

def test_a_well_formed_definition_is_accepted() -> None:
    """The positive control. Without it every refusal below is trivially true
    of a validator that rejects everything."""
    validate_job_def(make())


def test_the_trigger_is_the_whole_tuple() -> None:
    assert make().trigger == ("alpha", "smoke", "sandbox")


def test_the_canonical_spelling_is_flags_not_positionals() -> None:
    """Flags are what a person READS in a definition; positionals are what they
    type (A2.2.1). This is the spelling that says which is which."""
    text = make().trigger_text()
    assert "--product alpha" in text
    assert "--test_scope smoke" in text
    assert "--server sandbox" in text


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", ""),
        ("name", ""),
        ("name", "   "),
        ("product", ""),
        ("test_scope", ""),
        ("server", "  "),
        ("action_target", ""),
    ],
)
def test_a_missing_field_is_refused(field: str, value: str) -> None:
    with pytest.raises(StoreError):
        validate_job_def(dataclasses.replace(make(), **{field: value}))


def test_an_unknown_action_kind_is_refused_and_the_message_lists_the_real_ones() -> None:
    """A2.3: the action TYPE is code, only its target may be a row.

    The message names the valid kinds because the caller is an operator typing
    into a form, and "invalid action_kind" alone makes them guess.
    """
    with pytest.raises(StoreError) as caught:
        validate_job_def(dataclasses.replace(make(), action_kind="curl"))
    assert "gh-action" in str(caught.value)
    assert "test-server" in str(caught.value)


def test_both_real_action_kinds_are_accepted() -> None:
    for kind in ActionKind:
        validate_job_def(dataclasses.replace(make(), action_kind=kind.value,
                                             action_target="something"))


def test_validation_does_NOT_check_the_allowlist() -> None:
    """A2.3: a stored row is a request, never an authorisation.

    `product="not-on-any-allowlist"` is a well-formed definition. Whether it may
    RUN is decided at execution against live configuration, because
    configuration drifts and the check that matters is the one at use. Checking
    it here as well would look thorough and would be the store starting to
    become the boundary — at which point a row valid at save time carries
    authority it was never meant to have.
    """
    validate_job_def(dataclasses.replace(make(), product="not-on-any-allowlist"))


# ── the no-match suggestion (A2.2.2) ─────────────────────────────────────────

def test_a_definition_differing_in_one_token_is_a_near_miss() -> None:
    """A2.2.2: a command matching no job is refused WITH what would have
    matched. Silently ignored reads to the user as the bot being down."""
    defs = [make()]
    assert [d.id for d in near_misses(("alpha", "full", "sandbox"), defs)] == ["jd-1"]


def test_a_definition_differing_in_two_tokens_is_NOT_a_near_miss() -> None:
    """Suggesting everything is the same as suggesting nothing.

    A definition differing in two of three is a different job, and a refusal
    listing every definition in the store is one nobody reads.
    """
    defs = [make()]
    assert near_misses(("alpha", "full", "staging"), defs) == []
    assert near_misses(("beta", "full", "staging"), defs) == []


def test_near_misses_are_ordered_by_name() -> None:
    """The same mistake must produce the same message twice running."""
    defs = [
        make(id="c", name="charlie", server="c-server"),
        make(id="a", name="alfa", server="a-server"),
        make(id="b", name="bravo", server="b-server"),
    ]
    assert [d.name for d in near_misses(("alpha", "smoke", "nowhere"), defs)] == [
        "alfa", "bravo", "charlie",
    ]


def test_an_empty_store_suggests_nothing_rather_than_erroring() -> None:
    assert near_misses(("alpha", "smoke", "sandbox"), []) == []


def test_an_exact_match_is_not_reported_as_a_near_miss() -> None:
    """Zero differing tokens is a match, and the caller uses `job_def_for` for
    that. Counting it here would make a refusal suggest the job it just ran."""
    defs = [make()]
    assert near_misses(("alpha", "smoke", "sandbox"), defs) == []
