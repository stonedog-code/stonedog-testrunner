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


def build_parser(grammar: Grammar) -> _Parser:
    parser = _Parser(prog="/runtests", add_help=False)
    parser.add_argument("action", nargs="?", default="run", choices=ACTIONS)
    parser.add_argument("-p", "--product", required=True, choices=grammar.products)

    # Required unless there is exactly one allowed value — see the module
    # docstring. `required` and `default` are mutually exclusive in argparse, so
    # this is one branch rather than a clever expression.
    if len(grammar.servers) == 1:
        parser.add_argument("-s", "--server", default=grammar.servers[0],
                            choices=grammar.servers)
    else:
        parser.add_argument("-s", "--server", required=True, choices=grammar.servers)

    if len(grammar.test_scopes) == 1:
        parser.add_argument("--test_scope", default=grammar.test_scopes[0],
                            choices=grammar.test_scopes)
    else:
        parser.add_argument("--test_scope", required=True, choices=grammar.test_scopes)

    parser.add_argument("-k", "--select", default=None)
    parser.add_argument("-m", "--marker", default=None)
    return parser


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

    args = build_parser(grammar).parse_args(shlex.split(text))
    for name in ("select", "marker"):
        value = getattr(args, name)
        if value is not None and not EXPRESSION.match(value):
            raise SlackArgError(
                f"--{name} may only contain letters, numbers, spaces, _ and -"
            )
    return args
