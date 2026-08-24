"""The edge server.

TWO PUBLIC DOORS, AND THEY ARE NOT THE SAME DOOR.

    /slack/commands   Slack knocks. Authenticated by Slack's HMAC over the raw
                      body, then authorised against workspace, channel and user.

    /runner/*         Our own test servers knock. Nothing here is signed by
                      Slack, so this door gets its own lock: Ed25519 per test
                      server, with the same five-minute replay window.

The second door is the one that is easy to forget, and it is the more dangerous
of the two: the test servers are what talk to Slack, so anything that can feed
one a job can put a message in your channel from inside your network.

WHAT THIS PROCESS DOES NOT DO, ON PURPOSE

  * It does not run a test. The process answering the internet is never the
    process executing code.
  * It does not call the Slack API. It answers Slack's HTTP request — that is
    all — and every message in the channel comes from a test server. So no
    Slack bot token needs to exist on the public host at all.
  * It does not open a connection to anything on the internal network. Test
    servers dial out; the edge parks work and waits to be asked. No inbound
    port, no VPN, no firewall hole.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from slack_runtests import gate, identity
from slack_runtests.authz import refuse_or_warn
from stonedog_logs import configure as configure_logging
from slack_runtests.store import EnqueueResult, Job, JobStore, StoreBusy, open_store

from . import auth
from .config import EdgeConfig, load

log = logging.getLogger(__name__)

#: What every log line from this process is tagged with.
SERVICE_NAME = "slack-runtests-edge"


@asynccontextmanager
async def _startup_gate(app: FastAPI):
    """The refusal, where NO launcher can go around it.

    `__main__` performs the same check and prints a readable message, but
    `uvicorn {module}:app` is an entirely ordinary way to start a FastAPI
    process — it is what most Docker images do, and it never touches
    `__main__`. A control that is bypassed by choosing a different launcher is
    not a control; the integration tier here starts uvicorn exactly that way,
    and its passing unchanged is what showed the gap.

    One decision (`refuse_or_warn`), two call sites, on purpose.
    """
    # Before the first log line, and only if nothing else has: a bare
    # `uvicorn module:app` leaves application loggers with no handler at
    # all, so everything below would be written to nowhere. `uvicorn
    # --log-level info` does NOT fix that — it configures uvicorn's own
    # loggers, and an application logger with no handler falls back to
    # `lastResort`, which emits WARNING and above.
    configure_logging(service_name=SERVICE_NAME, only_if_unconfigured=True)
    cfg = getattr(app.state, "config", None) or load()
    app.state.config = cfg
    refusal = refuse_or_warn(
        log,
        signing_secret=cfg.signing_secret,
        allowed_team=cfg.allowed_team,
        allowed_channels=cfg.allowed_channels,
        allowed_users=cfg.allowed_users,
    )
    if refusal is not None:
        # LOGGED, then raised with one line. Raising the whole block gets it
        # wrapped in a Python traceback, which buries the part an operator
        # needs — the list of what to set — inside stack frames that tell them
        # nothing. The traceback still happens; it just no longer carries the
        # message that deserved to be read.
        for line in refusal.splitlines():
            log.critical("%s", line)
        raise RuntimeError(
            "refusing to start: required Slack protections are not configured "
            "(see the lines above)"
        )

    # OPEN THE STORE HERE, not on the first request.
    #
    # It used to be opened lazily, and two things followed that were only
    # visible when a DSN was actually wrong. A store that cannot be opened let
    # the process start and answer /healthz, then returned 500 to the first
    # Slack command — the failure arriving in front of a user, hours later, and
    # reading as the bot being broken. And the startup line said
    # `store: postgres` because it was reading the CONFIGURED backend: exactly
    # the green-over-an-empty-set this line exists to prevent, since its whole
    # job is to distinguish a working Postgres from a silent fallback.
    #
    # Now it is opened before the socket, and the backend is reported by the
    # OPENED STORE rather than by the configuration that asked for it.
    store = getattr(app.state, "store", None)
    if store is None:
        store = open_store(cfg.store_dsn, busy_timeout=cfg.db_busy_timeout)
        app.state.store = store
    log.info("store ready: %s", store.backend)
    yield

app = FastAPI(title="slack-runtests-edge", lifespan=_startup_gate)

#: How often a parked long-poll re-checks the queue. Small enough that a job
#: reaches an idle test server in well under a second; large enough that three
#: idle pollers are not a busy loop against SQLite's write lock.
POLL_INTERVAL = 0.5


# ── wiring ───────────────────────────────────────────────────────────────────

def _config(request: Request) -> EdgeConfig:
    cfg = getattr(request.app.state, "config", None)
    if cfg is None:
        cfg = load()
        request.app.state.config = cfg
    return cfg


def _store(request: Request) -> JobStore:
    store = getattr(request.app.state, "store", None)
    if store is None:
        cfg = _config(request)
        store = open_store(cfg.store_dsn, busy_timeout=cfg.db_busy_timeout)
        request.app.state.store = store
    return store


def _edge_key(request: Request):
    key = getattr(request.app.state, "edge_key", None)
    if key is None:
        key = identity.load_or_create(_config(request).key_path)
        request.app.state.edge_key = key
    return key


def ephemeral(text: str) -> JSONResponse:
    """A reply only the person who typed the command can see.

    Ephemeral by default is the right choice for an ack, an error or a refusal:
    those belong to the user, not to everyone in the channel. The *result* is
    the opposite — that goes to the channel, and a test server posts it.
    """
    return JSONResponse({"response_type": "ephemeral", "text": text})


def signed(request: Request, payload: dict | None, status: int = 200) -> Response:
    """A reply a test server can prove came from this edge.

    Signing only one direction would leave the test servers trusting whatever
    answers their poll. They are the machines that run code and post to Slack,
    so "the job came from the real edge" is not a nicety.
    """
    body = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    ts = str(int(time.time()))
    headers = {
        identity.HEADER_EDGE_TIMESTAMP: ts,
        identity.HEADER_EDGE_SIGNATURE: identity.sign_reply(_edge_key(request), ts, body),
    }
    if payload is None:
        return Response(status_code=status, headers=headers)
    return Response(content=body, media_type="application/json", status_code=status, headers=headers)


async def _authenticate(request: Request, body: bytes):
    """Identify the calling test server, or return the refusal to send back.

    Returns `(runner_row, None)` on success and `(None, response)` on failure.
    Every failure is the same flat 401 with no detail: an unknown runner id and
    a bad signature must be indistinguishable, or the endpoint becomes an
    oracle for which runner ids exist.
    """
    store = _store(request)
    runner_id = request.headers.get(identity.HEADER_RUNNER_ID, "")
    deny = JSONResponse({"error": "unauthorised"}, status_code=401)

    if not auth.valid_runner_id(runner_id):
        return None, deny
    row = store.runner(runner_id)
    if row is None:
        return None, deny
    if not auth.verify_signed(
        public_key_b64=row["public_key"],
        method=request.method,
        path=request.url.path,
        headers=request.headers,
        body=body,
    ):
        log.warning("rejected a signed request from %s", runner_id)
        return None, deny

    store.touch(runner_id)
    return row, None


# ── liveness ─────────────────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Deliberately reveals nothing about configuration or fleet state."""
    return {"status": "ok"}


@app.get("/edge/identity")
async def edge_identity(request: Request) -> dict[str, str]:
    """The edge's PUBLIC key, so a test server can pin it.

    Public by definition — there is nothing to protect here. Fetching it over
    the network is trust-on-first-use, which is fine in a lab; for production
    the READMEs tell you to pin the fingerprint in the test server's config so
    a substituted edge is refused.
    """
    key = _edge_key(request)
    pub = identity.public_b64(key)
    return {"public_key": pub, "fingerprint": identity.fingerprint(pub)}


# ── door 1: Slack ────────────────────────────────────────────────────────────

@app.post("/slack/commands")
async def slack_commands(request: Request) -> JSONResponse:
    """Validate, queue, and answer inside Slack's three-second budget.

    Note what is NOT here: no background task, no subprocess, no outbound call
    of any kind. The handler does four checks and one INSERT. That is what
    makes the three-second budget a non-issue rather than something to tune —
    and it is why a slow test server can never make Slack retry.
    """
    cfg = _config(request)
    store = _store(request)

    body = await request.body()
    outcome = gate.check(
        body,
        request.headers,
        signing_secret=cfg.signing_secret,
        allowed_team=cfg.allowed_team,
        allowed_channels=cfg.allowed_channels,
        allowed_users=cfg.allowed_users,
    )
    if not outcome.ok:
        if outcome.status != 200:
            return JSONResponse({"error": outcome.message}, status_code=outcome.status)
        # Slack's contract: a user error is a 200 with an ephemeral body. A 4xx
        # here would make Slack show its own generic failure instead of the
        # reason the command was wrong.
        return ephemeral(str(outcome.message))

    form, args = outcome.form, outcome.args
    assert args is not None

    channel = str(form.get("channel_name") or form.get("channel_id") or cfg.default_channel)
    if channel and not channel.startswith("#") and not channel.startswith("C"):
        channel = f"#{channel}"

    # Idempotency. Slack retries anything slow or non-2xx, so key on
    # `trigger_id` — unique per invocation — and let the PRIMARY KEY refuse the
    # duplicate. Deriving the id rather than storing a separate seen-set means
    # there is no second structure to keep in step.
    trigger = str(form.get("trigger_id") or form.get("text", ""))
    job_id = uuid.uuid5(uuid.NAMESPACE_URL, trigger).hex[:12]

    if args.action == "results":
        record = store.job(job_id) or store.last_for(args.product)
        if not record:
            return ephemeral(f"No recorded run for `{args.product}` yet.")
        return ephemeral(_describe(record))

    job = Job(
        id=job_id,
        product=args.product,
        server=args.server,
        select_expr=args.select,
        marker=args.marker,
        slack_channel=channel,
        slack_user=str(form.get("user_id", "")),
    )
    try:
        outcome = store.enqueue(job, caps=cfg.caps)
    except StoreBusy:
        # A refusal, not a fault. SQLite serialises writers, so a burst of
        # commands produces a lock timeout — and a 500 here would make Slack
        # show its generic failure, which reads to the user as the bot being
        # down rather than as "ask again in a second".
        log.warning("store busy while queueing %s", job_id)
        return ephemeral(
            "⚠️ The runner is busy right now — try that again in a moment."
        )
    if outcome is not EnqueueResult.ACCEPTED:
        return ephemeral(_refusal(outcome, job_id, args.product, channel))

    # Tell the user now if nothing can pick this up. The edge cannot post to
    # Slack — every channel message comes from a test server — so this
    # ephemeral reply is the only chance to say "queued, but nobody is home",
    # and a job silently waiting forever is the worst version of this.
    online = store.online(cfg.offline_after)
    eligible = [r for r in online if not r["labels"] or args.server in r["labels"]]
    if not eligible:
        return ephemeral(
            f"⚠️ Queued `{args.product}` on `{args.server}` (`{job_id}`), but **no test "
            f"server is online** to take it. It will run as soon as one checks in."
        )

    return ephemeral(
        f"⏳ Queued `{args.product}` on `{args.server}` (`{job_id}`) — "
        f"{len(eligible)} test server(s) available. Results will post to {channel}."
    )


def _refusal(outcome: EnqueueResult, job_id: str, product: str, channel: str) -> str:
    """Say which limit was hit, in the user's terms.

    A cap refusal and a duplicate are different facts and must not share a
    message: one means "you already asked for this", the other means "too much
    of this is already happening". Telling somebody the wrong one sends them
    looking in the wrong place, and a cap that reads as a bug gets raised as one.
    """
    if outcome is EnqueueResult.DUPLICATE:
        return f"That run is already queued (`{job_id}`)."
    if outcome is EnqueueResult.JOB_AT_CAPACITY:
        return (
            f"⚠️ `{product}` is already running. Wait for it to finish, or "
            f"ask for `results -p {product}`."
        )
    if outcome is EnqueueResult.CHANNEL_QUEUE_FULL:
        return (
            f"⚠️ {channel} already has as many runs waiting as it is allowed to "
            f"queue. They will clear as test servers pick them up."
        )
    if outcome is EnqueueResult.CHANNEL_BUSY:
        return (
            f"⚠️ {channel} already has as many runs going at once as it is "
            f"allowed. Wait for one to finish."
        )
    return f"Could not queue that run (`{job_id}`)."


def _describe(record: dict) -> str:
    state = record["state"]
    if record.get("finished_at"):
        return (
            f"`{record['product']}` on `{record['server']}` — id `{record['id']}`, "
            f"{state}: {record['passed']} passed · {record['failed']} failed · "
            f"{record['skipped']} skipped in {record['duration']:.1f}s "
            f"(ran on `{record['runner_id']}`)."
        )
    age = int(time.time() - record["created_at"])
    where = f" on `{record['runner_id']}`" if record.get("runner_id") else ""
    return f"`{record['product']}` on `{record['server']}` — id `{record['id']}`, {state}{where}, queued {age}s ago."


# ── door 2: test servers ─────────────────────────────────────────────────────

@app.post("/runner/enroll")
async def enroll(request: Request) -> Response:
    """Register a test server's public key.

    Two accepted paths, and the request must be SIGNED BY THE KEY IT PRESENTS
    in both. That proof of possession is what stops someone enrolling a key
    they do not hold in order to squat a runner id — the id is what result
    ownership is checked against, so squatting one would be enough to post
    forged results later.
    """
    cfg = _config(request)
    store = _store(request)
    body = await request.body()

    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        return JSONResponse({"error": "bad request"}, status_code=400)

    runner_id = str(payload.get("runner_id", ""))
    public_key = str(payload.get("public_key", ""))
    labels = [str(x) for x in payload.get("labels", []) if str(x).strip()]
    token = str(payload.get("enroll_token", ""))

    if not auth.valid_runner_id(runner_id) or not public_key:
        return JSONResponse({"error": "bad request"}, status_code=400)

    # Proof of possession, before any decision about whether to trust the key.
    if not auth.verify_signed(
        public_key_b64=public_key,
        method=request.method,
        path=request.url.path,
        headers=request.headers,
        body=body,
    ):
        return JSONResponse({"error": "unauthorised"}, status_code=401)

    known = store.runner(runner_id)
    preauth = auth.preauthorised_key(cfg.trusted_keys_dir, runner_id)

    if known is not None:
        # Re-enrolment by an existing test server: it must present the SAME key
        # it is already known by. A restart is normal and must work; a new key
        # for an existing id is a takeover and must not.
        if known["public_key"] != public_key:
            log.warning("refused re-enrolment of %s with a different key", runner_id)
            return JSONResponse({"error": "unauthorised"}, status_code=401)
    elif preauth is not None:
        if preauth != public_key:
            log.warning("refused %s: key does not match the pre-authorised one", runner_id)
            return JSONResponse({"error": "unauthorised"}, status_code=401)
    elif cfg.enroll_token and identity_token_ok(token, cfg.enroll_token):
        log.warning(
            "enrolled NEW test server %s via the bootstrap token — "
            "production should pre-authorise keys instead", runner_id
        )
    else:
        log.warning("refused unknown test server %s (no pre-authorised key, no token)", runner_id)
        return JSONResponse({"error": "unauthorised"}, status_code=401)

    store.enrol(runner_id, public_key, labels)
    log.info(
        "test server %s enrolled (key %s, labels=%s)",
        runner_id, identity.fingerprint(public_key), ",".join(labels) or "any",
    )
    return signed(request, {
        "runner_id": runner_id,
        "edge_public_key": identity.public_b64(_edge_key(request)),
        "heartbeat_interval": cfg.heartbeat_interval,
        "poll_timeout": cfg.poll_timeout,
        "lease_seconds": cfg.lease_seconds,
    })


def identity_token_ok(presented: str, expected: str) -> bool:
    """Constant-time compare for the bootstrap token."""
    import hmac

    return bool(presented) and hmac.compare_digest(presented, expected)


@app.post("/runner/heartbeat")
async def heartbeat(request: Request) -> Response:
    """"Still here." Renews the lease on everything this test server holds.

    Folding lease renewal into the heartbeat rather than giving it its own call
    means there is one liveness signal, not two that can disagree — a test
    server that is alive enough to heartbeat but not to renew is a state nobody
    would think to handle.
    """
    body = await request.body()
    row, refusal = await _authenticate(request, body)
    if refusal is not None:
        return refusal
    assert row is not None

    cfg = _config(request)
    store = _store(request)
    renewed = store.renew(row["runner_id"], cfg.lease_seconds)
    return signed(request, {"ok": True, "leases_renewed": renewed})


@app.post("/runner/jobs/claim")
async def claim(request: Request) -> Response:
    """Long-poll for work. Answers with a job, or 204 when the wait runs out.

    The edge holds the request open rather than answering "nothing" instantly,
    because a test server polling every second is a test server that is mostly
    asleep when work arrives. Holding it open makes dispatch feel immediate
    while the connection is still opened by the *test server* — which is the
    whole point: no inbound door opens on the internal network.

    The timeout is under 30s on purpose. A long-poll that outlives a proxy's
    idle timeout comes back as a 504, which reads as an edge outage.
    """
    body = await request.body()
    row, refusal = await _authenticate(request, body)
    if refusal is not None:
        return refusal
    assert row is not None

    cfg = _config(request)
    store = _store(request)
    labels = [x for x in row["labels"].split(",") if x]
    deadline = time.monotonic() + cfg.poll_timeout

    while True:
        try:
            job = await asyncio.to_thread(
                _claim_once, store, row["runner_id"], labels, cfg
            )
        except StoreBusy:
            # Contention, not an outage. A long-poll that answered 503 here
            # would teach a test server to back off from a store that is simply
            # busy — the right response is to ask again on the next tick.
            log.info("store busy while %s polled for work", row["runner_id"])
            job = None
        if job is not None:
            log.info("job %s -> %s", job.id, row["runner_id"])
            return signed(request, job.as_dispatch())
        if time.monotonic() >= deadline:
            return signed(request, None, status=204)
        await asyncio.sleep(POLL_INTERVAL)


def _claim_once(store: JobStore, runner_id: str, labels: list[str], cfg: EdgeConfig) -> Job | None:
    """One claim attempt, on a worker thread. Split out so it can be named in a trace."""
    return store.claim(
        runner_id, labels, cfg.lease_seconds, cfg.max_attempts, caps=cfg.caps
    )


@app.post("/runner/jobs/{job_id}/started")
async def started(job_id: str, request: Request) -> Response:
    body = await request.body()
    row, refusal = await _authenticate(request, body)
    if refusal is not None:
        return refusal
    assert row is not None

    try:
        ok = _store(request).mark_running(job_id, row["runner_id"])
    except StoreBusy:
        return _busy_response(request)
    return signed(request, {"ok": ok}, status=200 if ok else 409)


@app.post("/runner/jobs/{job_id}/result")
async def result(job_id: str, request: Request) -> Response:
    """Record the outcome — from the test server that holds the job, or not at all.

    The ownership check lives in the SQL (`WHERE ... AND runner_id=?`), so it
    cannot be bypassed by a code path that forgets to call a checker.
    """
    body = await request.body()
    row, refusal = await _authenticate(request, body)
    if refusal is not None:
        return refusal
    assert row is not None

    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        return JSONResponse({"error": "bad request"}, status_code=400)

    try:
        ok = _store(request).finish(
            job_id,
            row["runner_id"],
            exit_code=int(payload.get("exit_code", 1)),
            passed=int(payload.get("passed", 0)),
            failed=int(payload.get("failed", 0)),
            skipped=int(payload.get("skipped", 0)),
            duration=float(payload.get("duration", 0.0)),
            summary=str(payload.get("summary", "")),
        )
    except StoreBusy:
        # 503 with a Retry-After, deliberately: this is the one call carrying
        # information the edge cannot reconstruct. Answering 200 would throw a
        # real result away, and answering 500 would tell the test server the
        # payload was bad when it was fine.
        return _busy_response(request)
    if not ok:
        log.warning("rejected result for %s from %s — not its job", job_id, row["runner_id"])
    return signed(request, {"ok": ok}, status=200 if ok else 409)


def _busy_response(request: Request) -> Response:
    """Tell a test server to come back, in the one word HTTP has for it."""
    response = signed(request, {"error": "busy"}, status=503)
    response.headers["Retry-After"] = "2"
    return response


# ── operator view ────────────────────────────────────────────────────────────

@app.get("/admin/fleet")
async def fleet(request: Request) -> Response:
    """Who is enrolled, who is online, and what the queue looks like.

    404s unless EDGE_ADMIN_TOKEN is set and presented. Default deny, because
    which internal machines exist and when they were last seen is exactly the
    reconnaissance a public endpoint should not hand out.
    """
    cfg = _config(request)
    presented = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not cfg.admin_token or not identity_token_ok(presented, cfg.admin_token):
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    store = _store(request)
    return JSONResponse({
        "runners": store.runners(cfg.offline_after),
        "queue": store.counts(),
        "recent": store.recent(10),
    })
