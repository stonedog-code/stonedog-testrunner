"""The loop: enrol, heartbeat, ask for work, run it, report it.

THE SHAPE OF THIS FILE IS THE SECURITY ARGUMENT.

There is no server here. Nothing listens. Every function that touches the
network is an outbound call, so this machine can sit behind a firewall that
permits no inbound connection at all and the system still works. That is what
"the runners reach out; nothing reaches in" means in practice, and it is why
the direction of the wire is inverted relative to how the feature is described
("the edge sends a command to a test server"). The edge still decides who gets
what; it just parks the decision and waits to be asked.

ONE JOB AT A TIME, ON PURPOSE. A test server that claims a second job while the
first is running would make its own results harder to attribute and would let
one machine drain the queue. Three machines each holding one job is also what
makes the three-server harness prove anything about queueing.
"""

from __future__ import annotations

import logging
from typing import Iterable
import subprocess
import shutil
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from slack_runtests import identity
from slack_runtests.slack import announce_configuration
from slack_runtests.parsing import EXPRESSION

from .client import EdgeClient, EdgeError
from .config import RunnerConfig, load
from .reporter import JobReporter

log = logging.getLogger(__name__)


def build_argv(product: str, server: str, select: str, marker: str,
               suite_root: str, junit_path: str) -> list[str]:
    """Assemble the pytest command as a LIST, never a shell string.

    A list goes straight to `subprocess.run` with no shell, so a value
    containing a space or a `;` stays one argument and can never be
    reinterpreted as syntax. The edge already allowlisted every value; this is
    the second lock, and `validate()` below is the reason it is not merely
    decorative — the job arrived over a network and is not trusted just because
    it was signed.
    """
    launcher = ["uv", "run", "pytest"] if shutil.which("uv") else [sys.executable, "-m", "pytest"]
    argv = [*launcher, f"{suite_root}/{product}", f"--server={server}"]
    if select:
        argv += ["-k", select]
    if marker:
        argv += ["-m", marker]
    argv += ["-q", f"--junit-xml={junit_path}"]
    return argv


def validate(
    job: dict,
    *,
    allowed_products: Iterable[str],
    allowed_servers: Iterable[str],
    allowed_test_scopes: Iterable[str] = (),
) -> str | None:
    """Re-check the job against the same allowlists the edge used.

    A signature proves WHO sent a job, not that its contents are sane. If the
    edge is ever compromised, or a future version of it grows a new code path
    that skips a check, this is what stands between a job payload and a
    subprocess on an internal machine. Returns an error string, or None.
    """
    # An EMPTY allowlist refuses everything here, rather than allowing it.
    # `x not in frozenset()` is True, so this falls the safe way by construction
    # -- but it is worth saying out loud, because the opposite reading ("no
    # allowlist configured, so no restriction") is exactly the bug NEH-1119
    # fixed one layer up and NEH-1139 fixed here.
    if job.get("product") not in frozenset(allowed_products):
        return f"product {job.get('product')!r} is not on the allowlist"
    if job.get("server") not in frozenset(allowed_servers):
        return f"server {job.get('server')!r} is not on the allowlist"
    scopes = frozenset(allowed_test_scopes)
    if scopes and job.get("test_scope") is not None and job.get("test_scope") not in scopes:
        return f"test_scope {job.get('test_scope')!r} is not on the allowlist"
    for field in ("select", "marker"):
        value = job.get(field) or ""
        if value and not EXPRESSION.match(value):
            return f"{field} contains disallowed characters"
    return None


def counts_from_junit(path: Path) -> tuple[int, int, int, float, list[str]]:
    """Exact counts from the JUnit XML, not scraped from stdout.

    The V1 prototype parsed pytest's summary line and said in its own comments
    that it was a compromise. Here there is a `--junit-xml` on disk that pytest
    wrote, so read that: it carries per-test outcomes, which is what makes
    naming the failed tests possible at all.

    An unreadable report yields zeros and no names rather than a wrong number;
    the exit code is what decides pass/fail either way.
    """
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return 0, 0, 0, 0.0, []

    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    total = failures = errors = skipped = 0
    duration = 0.0
    failed_ids: list[str] = []
    for suite in suites:
        total += int(suite.get("tests", 0) or 0)
        failures += int(suite.get("failures", 0) or 0)
        errors += int(suite.get("errors", 0) or 0)
        skipped += int(suite.get("skipped", 0) or 0)
        duration += float(suite.get("time", 0) or 0)
        for case in suite.iter("testcase"):
            if case.find("failure") is not None or case.find("error") is not None:
                name = case.get("name", "?")
                cls = case.get("classname", "")
                failed_ids.append(f"{cls}::{name}" if cls else name)

    failed = failures + errors
    return max(total - failed - skipped, 0), failed, skipped, duration, failed_ids


def execute(cfg: RunnerConfig, client: EdgeClient, job: dict) -> None:
    """Run one job and say so four times. Never raises into the poll loop.

    An exception escaping here would kill the loop and take the test server
    offline, which the edge would eventually notice as a missed heartbeat — but
    only after the lease expired, and only after the person in Slack had waited.
    Failing loudly *in the channel* is the better outcome.
    """
    job_id = str(job.get("job_id", ""))
    reporter = JobReporter(str(job.get("slack_channel") or ""), cfg.runner_id)
    reporter.received(job)

    problem = validate(
        job,
        allowed_products=cfg.allowed_products,
        allowed_servers=cfg.allowed_servers,
        allowed_test_scopes=cfg.allowed_test_scopes,
    )
    if problem is not None:
        log.error("refused job %s: %s", job_id, problem)
        reporter.completed(job, exit_code=4, duration=0.0)
        reporter.summary(0, 0, 0, 0.0, [])
        _safe_result(client, job_id, exit_code=4, passed=0, failed=0, skipped=0,
                     duration=0.0, summary=f"refused: {problem}")
        return

    workdir = Path(cfg.workdir).resolve()
    suite = workdir / cfg.suite_root / job["product"]
    junit = workdir / f"results-{job_id}.xml"
    argv = build_argv(job["product"], job["server"], job.get("select", ""),
                      job.get("marker", ""), cfg.suite_root, str(junit))

    try:
        client.started(job_id)
    except EdgeError as exc:
        # Not fatal: the edge missing a state transition is cosmetic, whereas
        # abandoning the run because of it would be a real outage.
        log.warning("could not report start of %s: %s", job_id, exc)

    if not suite.is_dir():
        reporter.started(job, argv)
        reporter.completed(job, exit_code=5, duration=0.0)
        reporter.summary(0, 0, 0, 0.0, [])
        _safe_result(client, job_id, exit_code=5, passed=0, failed=0, skipped=0,
                     duration=0.0, summary=f"no suite at {cfg.suite_root}/{job['product']}")
        return

    reporter.started(job, argv)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv, cwd=workdir, capture_output=True, text=True,
            timeout=cfg.job_timeout, check=False,
        )
        exit_code = completed.returncode
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - started
        reporter.completed(job, exit_code=3, duration=duration)
        reporter.summary(0, 0, 0, duration, [])
        _safe_result(client, job_id, exit_code=3, passed=0, failed=0, skipped=0,
                     duration=duration, summary="timed out and was killed")
        return
    except OSError as exc:
        reporter.completed(job, exit_code=3, duration=0.0)
        reporter.summary(0, 0, 0, 0.0, [])
        _safe_result(client, job_id, exit_code=3, passed=0, failed=0, skipped=0,
                     duration=0.0, summary=f"could not start pytest: {exc}")
        return

    duration = time.monotonic() - started
    passed, failed, skipped, suite_time, failed_ids = counts_from_junit(junit)

    # stdout is captured and deliberately NOT posted, only logged locally.
    log.info("job %s exited %s in %.1fs", job_id, exit_code, duration)

    # ONE duration, used everywhere. The JUnit report also carries a `time`
    # (the suite's own, 0.04s here) and reporting that in the summary while the
    # completion message showed wall clock (0.8s, including interpreter start)
    # meant two adjacent Slack messages disagreeing about the same run. Both
    # numbers were correct and the pair was confusing, which is worse than
    # either alone. Wall clock wins: it is what the person actually waited.
    log.info("job %s suite time %.3fs, wall clock %.1fs", job_id, suite_time, duration)

    reporter.completed(job, exit_code=exit_code, duration=duration)
    reporter.summary(passed, failed, skipped, duration, failed_ids)
    _safe_result(client, job_id, exit_code=exit_code, passed=passed, failed=failed,
                 skipped=skipped, duration=duration,
                 summary=", ".join(failed_ids[:10]))


def _safe_result(client: EdgeClient, job_id: str, **outcome) -> None:
    """Report the outcome, and do not die if the edge is briefly unreachable.

    The Slack message has already gone out at this point, so the person who
    asked has their answer. What is lost here is the edge's record — worth two
    retries, not worth crashing the loop over.
    """
    for attempt in range(3):
        try:
            client.result(job_id, **outcome)
            return
        except EdgeError as exc:
            log.warning("result for %s not accepted (try %s): %s", job_id, attempt + 1, exc)
            time.sleep(2 * (attempt + 1))
    log.error("gave up reporting result for %s", job_id)


def _heartbeat_loop(client: EdgeClient, interval: float, stop: threading.Event) -> None:
    """Say "still here" on a timer, in its own thread.

    A daemon thread rather than an async task because the job itself is a
    blocking subprocess: an event loop would stop being serviced for the whole
    run, the heartbeats would stop, and the edge would requeue a job that is
    running perfectly well. That failure — a healthy server declared dead
    because it was busy — is the one this shape exists to avoid.
    """
    while not stop.wait(interval):
        try:
            client.heartbeat()
        except EdgeError as exc:
            log.warning("heartbeat failed: %s", exc)


def run_forever(cfg: RunnerConfig | None = None) -> int:
    cfg = cfg or load()
    key = identity.load_or_create(cfg.key_path)
    public = identity.public_b64(key)

    log.info("test server %s", cfg.runner_id)
    log.info("  public key  %s", public)
    log.info("  fingerprint %s   <- give this to the edge operator", identity.fingerprint(public))
    log.info("  labels      %s", ",".join(cfg.labels) or "(any environment)")

    client = EdgeClient(cfg.edge_url, cfg.runner_id, key, cfg.edge_fingerprint)

    # No channel here on purpose: a test server is told its channel by each
    # job, so it has none to name at startup.
    announce_configuration(log)

    # Enrol, retrying with backoff. The edge restarting must not require anyone
    # to touch the test servers, so "cannot reach the edge" is a wait, not an
    # exit. A refusal is different and is also a wait — an operator may be
    # about to add the key — but it is logged at error level every time.
    settings: dict = {}
    delay = cfg.retry_seconds
    while True:
        try:
            settings = client.enroll(public, cfg.labels, cfg.enroll_token)
            break
        except EdgeError as exc:
            log.error("enrolment: %s — retrying in %ss", exc, int(delay))
            time.sleep(delay)
            delay = min(delay * 2, cfg.max_retry_seconds)

    poll_timeout = float(settings.get("poll_timeout", 25))
    interval = float(settings.get("heartbeat_interval", cfg.heartbeat_interval))

    stop = threading.Event()
    beat = threading.Thread(target=_heartbeat_loop, args=(client, interval, stop), daemon=True)
    beat.start()

    log.info("waiting for work (long-poll %ss, heartbeat %ss)", poll_timeout, interval)
    delay = cfg.retry_seconds
    try:
        while True:
            try:
                job = client.claim(poll_timeout)
                delay = cfg.retry_seconds
            except EdgeError as exc:
                log.warning("claim: %s — retrying in %ss", exc, int(delay))
                time.sleep(delay)
                delay = min(delay * 2, cfg.max_retry_seconds)
                continue
            if job is None:
                continue
            execute(cfg, client, job)
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        stop.set()
    return 0
