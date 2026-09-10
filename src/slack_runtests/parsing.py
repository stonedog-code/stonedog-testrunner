"""Parse the slash-command argument string.

Slack hands you the whole argument string as one field, `text`. The obvious
thing to do is split on whitespace and read positionally:

    parts = text.split()
    product = parts[1]

That works, and it stops working the moment there is a fourth argument. Nobody
remembers whether the server or the product comes first, an optional argument
cannot be added without changing the meaning of every invocation already in
people's muscle memory, and `parts[1]` silently accepts anything at all —
including `../../etc`.

Flags fix all three, and `argparse`'s `choices=` gives an allowlist and the help
text from the same line.

THE ALLOWLISTS ARE CONFIGURATION, NOT LITERALS (PRD §4.1, A2.10)

They used to be module constants:

    PRODUCTS = ("webapp", "billing", "catalog")

which meant no other organisation could use this tool without editing Python.
They now arrive as a `Grammar`, built from `RUNTESTS_PRODUCTS`,
`RUNTESTS_SERVERS` and `RUNTESTS_TEST_SCOPES`, and the process refuses to start
when any of them is unset — see `authz.missing_protections`. An empty allowlist
is a startup failure, never "allow everything": this is the fleet's
green-over-an-empty-set rule applied to an authorisation boundary, and a config
that silently permits every product is worse than no config at all.

A2.10 draws the line that matters for the job store landing next: the allowlist
is the *security boundary*, a job is a *routing decision*. A job may only name
values already in the allowlist, and the enumerations are never derived from the
job list — deriving them would let adding a job widen the boundary.

WHY THERE IS NO DEFAULT SERVER ANY MORE

`--server` used to default to `"staging"`. Once the allowlist is the operator's,
a compiled-in default is a value that may not be in it — so the parser would
either refuse a command nobody typed a mistake in, or quietly accept a value
outside the allowlist. Both are worse than asking.

The one exception is an allowlist with exactly ONE value, where the default is
that value: there is nothing to be ambiguous about, and requiring people to name
the only possible answer is friction with no safety attached.

`results` DOES NOT INHERIT THE TRIGGER GRAMMAR (NEH-1166)

One parser served every action, so asking "how did the last billing run go"
was refused for a missing `-s` the handler never read. The friction was the
smaller half. The larger half is that naming the WRONG server returned the same
answer, so the flag looked like a filter and was decoration — and a filter you
cannot observe failing is worse than no filter.

`-s` is now optional for `results` and, when given, genuinely narrows the
lookup. `--test_scope` is optional too and CANNOT narrow it, because the `jobs`
table records a run's product and server and no test scope; rather than
accepting it silently, the reply says so. Both flags are still allowlisted when
present, and both are still REQUIRED for a run — that is the security boundary,
and relaxing it for `results` must not relax it generally.
"""

from __future__ import annotations

import argparse
import re
import shlex
from dataclasses import dataclass
from typing import Iterable

#: -k and -m are pytest expressions. They cannot reach a shell from here — the
#: runner builds argv as a list — but in V2 they travel through a GitHub Actions
#: input, and THAT can. Constrain them at the door; the workflow's `env:` mapping
#: is the second lock on the same door and both are kept.
EXPRESSION = re.compile(r"^[A-Za-z0-9_ -]{1,80}$")

ACTIONS = ("run", "results")

#: Appended to a `results` reply when `--test_scope` was supplied.
#:
#: The `jobs` table records a run's product and server and NOT its test scope,
#: so the flag cannot narrow the answer. Saying so is the point: the defect
#: NEH-1166 named is that naming the wrong value returned the same answer, which
#: teaches people a flag matters when it does not. The flag is accepted rather
#: than refused because `results` is most often typed by editing a `run` command
#: that already carries it.
SCOPE_NOT_APPLIED = (
    "\n_A run records its product and its server, not a test scope, "
    "so `--test_scope` did not narrow this answer._"
)


class SlackArgError(Exception):
    """A bad command, to be shown to the user rather than logged as a 500."""


@dataclass(frozen=True, slots=True)
class Grammar:
    """The three allowlisted tokens of a trigger, as this deployment defines them.

    Sorted tuples rather than sets, because argparse prints `choices` into the
    error message a user reads. A set's iteration order is stable within a
    process but arbitrary between them, so the same mistake would produce a
    differently-ordered list of options on different days.
    """

    products: tuple[str, ...]
    servers: tuple[str, ...]
    test_scopes: tuple[str, ...]

    @classmethod
    def of(
        cls,
        products: Iterable[str],
        servers: Iterable[str],
        test_scopes: Iterable[str],
    ) -> "Grammar":
        return cls(
            products=tuple(sorted(products)),
            servers=tuple(sorted(servers)),
            test_scopes=tuple(sorted(test_scopes)),
        )

    def usage_hint(self) -> str:
        """A hint built from THIS deployment's values.

        The hint was `Try: /runtests -p webapp -s staging -k smoke` — three
        values from the old compiled-in tuples. Shipped to a stranger it names
        products they do not have, which reads as the tool being misconfigured
        rather than as an example.
        """
        product = self.products[0] if self.products else "<product>"
        parts = [f"Try: `/runtests -p {product}"]
        if len(self.servers) > 1:
            parts.append(f" -s {self.servers[0]}")
        if self.test_scopes:
            parts.append(f" --test_scope {self.test_scopes[0]}")
        return "".join(parts) + "`"


class _Parser(argparse.ArgumentParser):
    """argparse that raises instead of exiting.

    Stock argparse calls `sys.exit()` on a bad flag. Inside a web handler that
    is a 500, and the user sees Slack's generic "dispatch_failed" instead of the
    reason their command was wrong — which is unhelpful precisely when they most
    need help.
    """

    def error(self, message: str):  # type: ignore[override]
        raise SlackArgError(message)

    def exit(self, status: int = 0, message: str | None = None):  # type: ignore[override]
        raise SlackArgError(message or "bad command")


def build_parser(grammar: Grammar, *, require_trigger: bool = True) -> _Parser:
    """The command grammar.

    `require_trigger=False` drops the requirement on `-s` and `--test_scope`.
    It exists for `results`, which needs neither — see `parse` (NEH-1166).
    """
    parser = _Parser(prog="/runtests", add_help=False)
    parser.add_argument("action", nargs="?", default="run", choices=ACTIONS)
    parser.add_argument("-p", "--product", required=True, choices=grammar.products)

    # Required unless there is exactly one allowed value — see the module
    # docstring. `required` and `default` are mutually exclusive in argparse, so
    # this is one branch rather than a clever expression.
    #
    # ...and never required when the action does not USE them. A flag that is
    # demanded and then ignored teaches people it matters when it does not, and
    # naming the wrong one still returns the same answer, which is worse than
    # asking for nothing.
    if not require_trigger:
        parser.add_argument("-s", "--server", default=None, choices=grammar.servers)
        parser.add_argument("--test_scope", default=None, choices=grammar.test_scopes)
    elif len(grammar.servers) == 1:
        parser.add_argument("-s", "--server", default=grammar.servers[0],
                            choices=grammar.servers)
        _add_test_scope(parser, grammar)
    else:
        parser.add_argument("-s", "--server", required=True, choices=grammar.servers)
        _add_test_scope(parser, grammar)

    parser.add_argument("-k", "--select", default=None)
    parser.add_argument("-m", "--marker", default=None)
    return parser


def _add_test_scope(parser: _Parser, grammar: Grammar) -> None:
    if len(grammar.test_scopes) == 1:
        parser.add_argument("--test_scope", default=grammar.test_scopes[0],
                            choices=grammar.test_scopes)
    else:
        parser.add_argument("--test_scope", required=True, choices=grammar.test_scopes)


def parse(text: str, grammar: Grammar) -> argparse.Namespace:
    """Parse `text` into a validated namespace, or raise SlackArgError.

    `shlex.split` is what makes `-k "smoke and not slow"` arrive as ONE argument
    instead of four. It is also why the expression regex below is applied after
    splitting rather than to the raw string.

    `grammar` is required rather than defaulted. A default would be a compiled-in
    allowlist by another name, and the one thing this module must not have is a
    way to end up permissive because a caller forgot an argument.
    """
    if not grammar.products or not grammar.servers or not grammar.test_scopes:
        # Belt and braces. `authz.missing_protections` refuses this at startup,
        # so reaching here means something constructed a Grammar directly. An
        # empty `choices` tuple makes argparse reject EVERY value, which would
        # read as "the command is wrong" rather than "the server is misconfigured".
        raise SlackArgError(
            "this deployment has no product, server or test-scope allowlist "
            "configured, so no command can be authorised"
        )

    tokens = shlex.split(text)

    # TWO PASSES, and the first one is not a guess at token positions.
    #
    # `results` needs no server and no test scope, so requiring them is friction
    # with nothing attached (NEH-1166). But which action a command names is
    # argparse's answer to give, not a scan's: `action` is a `nargs="?"`
    # positional, so `-p billing results` is a legal spelling and
    # `tokens[0] == "results"` gets it wrong. Parse permissively, read the
    # action argparse resolved, and only then re-parse under the strict grammar
    # if this really is a run.
    #
    # The permissive pass still enforces `-p` and every `choices` allowlist, so
    # nothing reaches the second pass that the first would have refused, and a
    # bad value is reported by the same message either way.
    peek = build_parser(grammar, require_trigger=False).parse_args(tokens)
    if peek.action == "results":
        args = peek
    else:
        args = build_parser(grammar).parse_args(tokens)
    for name in ("select", "marker"):
        value = getattr(args, name)
        if value is not None and not EXPRESSION.match(value):
            raise SlackArgError(
                f"--{name} may only contain letters, numbers, spaces, _ and -"
            )
    return args
