"""The READMEs and the code must agree about configuration, in both directions.

WHY THIS IS A TEST AND NOT A CONVENTION

A README is the one artefact in a repository that nothing compares to the code.
This fleet has already shipped one quoting a version twenty-one releases out of
date, with every gate green throughout, because no check existed that could
notice. Configuration is the worst place for that drift: a variable documented
but never read is a setting somebody carefully configures and that does nothing,
and a variable read but never documented is one nobody knows to set — which, for
this project, is how you end up with an unprotected public endpoint.

WHAT IT DOES NOT DO

It does not parse Markdown. It looks for a backticked NAME in the first cell of
a table row, which is the shape every configuration table in this repo already
uses, and nothing else. Generating the tables from `config.py` instead was
considered and rejected: the "What it does" column carries the reasoning — why
the poll timeout is under 30s, why an admin token being empty means 404 — and
that is the valuable half. It belongs in prose a person edits, not in a
docstring a generator emits.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

#: Where configuration is read. Every `os.environ` access under these is a
#: variable that must be documented somewhere.
SOURCE_DIRS = ("src", "edge", "test-server")

#: Where configuration is documented.
DOC_FILES = ("README.md", "edge/README.md", "test-server/README.md", ".env.example")

#: EVERY NAME MATCHED MUST CONTAIN AN UNDERSCORE. Without that the doc scanner
#: also matches `GET` and `POST` from the endpoints table, and the code scanner
#: matches `HOST` and `PORT` — so the two sides disagree about four names that
#: are not configuration at all. The cost is that a future single-word variable
#: would go unnoticed; every one this project has is `AREA_THING`, and a rule
#: that reports four false failures is a rule somebody deletes.
_NAME = r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+"

#: `os.environ.get("X")`, `os.environ["X"]`, and this repo's `_csv`/`_num` helpers.
READS = re.compile(r'(?:os\.environ(?:\.get)?[\(\[]|_csv\(|_num\()\s*"(' + _NAME + r')"')
#: A name held in a constant — `INSECURE_DEV_ENV = "RUNTESTS_INSECURE_DEV"`.
#: `authz.py` reads its variable through one, so without this the scanner cannot
#: see the single most important setting in the repo.
READS_VIA_CONSTANT = re.compile(r'^[A-Z][A-Z0-9_]*_ENV\s*=\s*"(' + _NAME + r')"', re.MULTILINE)
#: A backticked name in the FIRST cell of a Markdown table row.
DOCUMENTED = re.compile(r"^\|\s*`(" + _NAME + r")`\s*\|")
#: `NAME=` at the start of a line, which is what .env.example is made of.
ENV_EXAMPLE = re.compile(r"^(" + _NAME + r")=", re.MULTILINE)

#: Variables deliberately absent from the tables, each with the reason.
#: An exemption list is a place defects hide, so it is short, and every entry
#: says why rather than simply naming a variable.
UNDOCUMENTED_ON_PURPOSE = {
    # The Python logging convention, not something this project defines.
    "LOG_LEVEL": "the Python logging convention",
    # A test seam: it exists so the suite can point the Slack client at a local
    # server. Documenting it in a deployment table would invite somebody to set
    # it in one.
    "SLACK_API_BASE": "a test seam, never a deployment setting",
}

# HOST, PORT and RELOAD are uvicorn's own and are not matched at all — they
# have no underscore. See the note on `_NAME`.


def _read_sources() -> tuple[set[str], int]:
    names: set[str] = set()
    files = 0
    for directory in SOURCE_DIRS:
        for path in sorted((ROOT / directory).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            files += 1
            text = path.read_text(encoding="utf-8")
            names.update(READS.findall(text))
            names.update(READS_VIA_CONSTANT.findall(text))
    return names, files


def _read_docs() -> tuple[set[str], int]:
    names: set[str] = set()
    files = 0
    for name in DOC_FILES:
        path = ROOT / name
        if not path.exists():
            continue
        files += 1
        text = path.read_text(encoding="utf-8")
        if name.endswith(".env.example"):
            names.update(ENV_EXAMPLE.findall(text))
        else:
            for line in text.splitlines():
                match = DOCUMENTED.match(line)
                if match:
                    names.add(match.group(1))
    return names, files


def test_the_scan_actually_finds_something() -> None:
    """The positive control, and the reason the counts are printed.

    Every assertion below is trivially satisfied by a scan that matched nothing:
    two empty sets agree perfectly. So the size of each input set is asserted to
    be plausible before anything is compared, and printed either way.
    """
    read, source_files = _read_sources()
    documented, doc_files = _read_docs()

    print(f"\nconfig parity: {len(read)} variable(s) read across {source_files} source file(s)")
    print(f"config parity: {len(documented)} variable(s) documented across {doc_files} file(s)")

    assert source_files >= 10, f"only scanned {source_files} source files"
    assert doc_files == len(DOC_FILES), f"only found {doc_files} of {len(DOC_FILES)} doc files"
    assert len(read) >= 25, f"only found {len(read)} environment reads — the regex has stopped matching"
    assert len(documented) >= 25, f"only found {len(documented)} documented names"


def test_every_variable_the_code_reads_is_documented() -> None:
    """A setting nobody knows to set is, in this project, an open endpoint."""
    read, _ = _read_sources()
    documented, _ = _read_docs()

    missing = sorted(read - documented - set(UNDOCUMENTED_ON_PURPOSE))
    assert not missing, (
        f"{len(missing)} variable(s) are read but documented nowhere: {missing}. "
        f"Add them to a configuration table, or to UNDOCUMENTED_ON_PURPOSE with "
        f"a reason."
    )


def test_every_documented_variable_is_actually_read() -> None:
    """The other direction, and the one that rots silently.

    A variable that was renamed in the code and left in the table is a setting
    somebody configures carefully and that does nothing at all — with no error,
    because an unread environment variable is not an error.
    """
    read, _ = _read_sources()
    documented, _ = _read_docs()

    stale = sorted(documented - read)
    assert not stale, (
        f"{len(stale)} documented variable(s) are read by no code: {stale}. "
        f"Either the code was renamed and the docs were not, or the setting is gone."
    )


def test_the_exemptions_are_all_still_real() -> None:
    """An exemption list is a place defects hide, so it is not allowed to rot
    either. A name exempted here that the code no longer reads is a line nobody
    will ever remove unless something says so."""
    read, _ = _read_sources()
    dead = sorted(set(UNDOCUMENTED_ON_PURPOSE) - read)
    assert not dead, f"exempted but no longer read anywhere: {dead}"
