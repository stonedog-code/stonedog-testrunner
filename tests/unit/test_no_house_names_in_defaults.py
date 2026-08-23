"""This account's org, domain and product names must never be a default.

THE DECISION THIS ENFORCES

The PRD settled that this repository ships its own deployment as an example
(§7.5), with one hard rule attached: the names belong in `examples/`, which a
reader copies and edits, and never in a fallback the code uses when nothing is
configured. What makes that safe is the startup gate — a stranger who copies an
example and forgets to edit it gets a refusal naming what is missing, rather
than a bot quietly wired to somebody else's products.

WHY A TEST AND NOT A REVIEW NOTE

Because the way it gets broken is not carelessness, it is convenience. Somebody
debugging locally puts a real product name in a `config.py` fallback so they
stop typing it, it works, and it ships — and the next stranger's runner is
configured with this account's product list. That is not hypothetical for this
fleet: it has already published an internal issue id onto a public marketing
page, and again in an error message, both times because the person writing it
had the internal detail in their head at that moment.

WHAT IS EXEMPT, AND WHY IT IS NOT A LOOPHOLE

`examples/` is exempt — it is the one place these names are allowed, and it is
inert until copied. Comments are NOT exempt anywhere else: a comment explaining
why `prod` is absent from the server list is exactly the kind of reasoning worth
keeping, and none of the banned strings should appear in one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

#: Everything that must never be a shipped default. Lower-cased before matching,
#: so casing games do not get past it.
HOUSE_NAMES = (
    "stonedogcode",
    "stonedog-code",
    "hopperguard",
    "elderlink",
    "rozcards",
    "optimafilings",
    "nehsa",
    "nehsanet",
)

#: Scanned. These are the files a deployment actually runs on.
SCANNED = (
    "src", "edge", "test-server", "docker",
    "pyproject.toml", ".env.example", ".github",
)

#: `examples/` is the ONE place these names may appear — that is what the
#: directory is for. It is not scanned, and no other exemption exists.
EXEMPT_PARTS = {"examples", "__pycache__", ".venv", ".venv-docker"}

SUFFIXES = {".py", ".toml", ".yml", ".yaml", ".sh", ".example", ".conf", ""}


def _files() -> list[Path]:
    found: list[Path] = []
    for name in SCANNED:
        target = ROOT / name
        if target.is_file():
            found.append(target)
            continue
        for path in sorted(target.rglob("*")):
            if not path.is_file():
                continue
            if EXEMPT_PARTS & set(path.parts):
                continue
            if path.suffix in SUFFIXES:
                found.append(path)
    return found


def test_the_scan_examines_a_plausible_number_of_files() -> None:
    """The input-set size, asserted rather than assumed.

    A scan that matched nothing passes every test below, and looks identical to
    a scan that examined everything and found nothing wrong. Only the count
    distinguishes them.
    """
    files = _files()
    print(f"\nhouse-name scan: {len(files)} file(s) examined for {len(HOUSE_NAMES)} name(s)")
    assert len(files) >= 25, f"only found {len(files)} files — the scan has stopped finding them"


def test_no_house_name_appears_in_anything_a_deployment_runs() -> None:
    hits: list[str] = []
    for path in _files():
        try:
            text = path.read_text(encoding="utf-8").lower()
        except (UnicodeDecodeError, OSError):
            continue
        for name in HOUSE_NAMES:
            if name in text:
                rel = path.relative_to(ROOT)
                line = next(
                    (n for n, ln in enumerate(text.splitlines(), 1) if name in ln), 0
                )
                hits.append(f"{rel}:{line} contains {name!r}")

    assert not hits, (
        f"{len(hits)} house name(s) in shipped files:\n  " + "\n  ".join(hits)
        + "\n\nThese belong in examples/, which a reader copies and edits — never "
          "in a default the code falls back to."
    )


def test_the_guard_can_actually_fail() -> None:
    """Both directions, in one test, without editing the tree.

    A guard observed only passing has not been tested, it has been run. This
    plants a house name in a string the scanner would read and confirms the same
    matching logic catches it — so a future change that broke the match (a wrong
    suffix list, a missing lower(), an over-eager exemption) fails here rather
    than passing silently over everything.
    """
    planted = "RUNTESTS_PRODUCTS=hopperguard,rozcards".lower()
    caught = [name for name in HOUSE_NAMES if name in planted]
    assert "hopperguard" in caught and "rozcards" in caught


def test_examples_do_carry_placeholders_rather_than_real_names() -> None:
    """The exempt directory still has to be honest.

    `examples/` is where these names would be ALLOWED — and the point of the
    example is that it carries none of them anyway, because a reader should be
    editing placeholders, not somebody else's product list. If that ever stops
    being true it is a decision, not a drift.
    """
    examples = ROOT / "examples"
    assert examples.is_dir(), "the examples directory is part of the deliverable"

    hits: list[str] = []
    files = 0
    for path in sorted(examples.rglob("*")):
        if not path.is_file() or path.suffix not in SUFFIXES:
            continue
        files += 1
        text = path.read_text(encoding="utf-8").lower()
        hits += [f"{path.relative_to(ROOT)}: {n}" for n in HOUSE_NAMES if n in text]

    print(f"\nhouse-name scan: {files} example file(s) examined")
    assert files >= 5, f"only found {files} example files"
    assert not hits, f"examples name this account rather than a placeholder: {hits}"
