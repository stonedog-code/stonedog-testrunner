# Test server

The machine that actually runs the tests, and the machine that does all the
talking. It lives inside your network.

**Nothing listens here.** There is no server in this folder despite the name —
only a loop that makes outbound calls. That is what lets the host sit behind a
firewall permitting no inbound connection at all:

```
  enrol  ─────────▶ edge     once, presenting a public key
  heartbeat ──────▶ edge     every 30s: "still here", and renew my leases
  claim ──────────▶ edge     long-poll: "any work for me?"
  result ─────────▶ edge     "here is what happened"
  ────────────────▶ Slack    four messages per job
```

The feature is described as "the edge sends a command to a test server", and
functionally that is true — the edge decides who gets what. Only the direction
of the wire is inverted, and inverting it is the difference between a closed
network and an open port.

## Run it

```bash
bash run.sh runner                     # from the repo root
```

or as part of the three-server harness:

```bash
docker compose -f docker/compose.yml up --build
```

On first start it generates its keypair and prints the line the edge operator
needs:

```
INFO  test server web-runner-01
INFO    public key  9Zt0…
INFO    fingerprint 1f4c9a02b77e5d31   <- give this to the edge operator
INFO    labels      (any environment)
```

## Registering with the edge

### Production — the operator pre-authorises your key

1. Start the test server once and copy the **public key** it logs.
2. On the edge host, write it to `trusted_runners/<runner_id>.pub`.
3. Restart the test server. It enrols and starts polling.

Leave `RUNNER_ENROLL_TOKEN` unset on both sides. The edge then accepts this
`runner_id` **only** with that exact key, and revoking access is deleting one
file. A restart re-enrols with the same key and is fine; a *different* key for
an existing id is a takeover attempt and is refused.

The private key never leaves this machine. It is written `0600` and the loader
refuses to start if the mode has been widened — a private key that is
world-readable on a shared box is not a private key, and nothing else about the
system would look wrong.

### Lab — a shared bootstrap token

Set the same `RUNNER_ENROLL_TOKEN` on the edge and here. A new `runner_id`
presenting the right token gets its key recorded on first contact. After that
the token is irrelevant: every later request is authenticated by the key.

### Pin the edge, too

The edge signs its replies. Set `EDGE_FINGERPRINT` to the value from
`GET /edge/identity` (the edge also logs it at startup) and this server will
refuse to enrol with anything else. Unset means trust-on-first-use — acceptable
in a lab, not in production. Without the pin, the reply signature proves only
that the same party answered twice.

## Configuration

| Variable | Default | What it does |
|---|---|---|
| `EDGE_URL` | `http://127.0.0.1:8500` | Where the edge is. |
| `RUNNER_ID` | the hostname | This server's name. **Keep it stable across restarts** — it is what job ownership is checked against, so a server that returns under a new id has abandoned whatever it held. |
| `RUNNER_KEY_PATH` | `keys/runner_ed25519.pem` | The private key. Generated `0600` on first run. |
| `RUNNER_LABELS` | *(empty)* | Environments this server accepts, comma-separated. **Empty means any**, which is the shared pool the three-server harness uses. Set it (`staging`) when only this box can reach a particular environment. |
| `EDGE_FINGERPRINT` | *(empty)* | Pin the edge's key. See above. |
| `RUNNER_ENROLL_TOKEN` | *(empty)* | Lab bootstrap only. |
| `RUNNER_WORKDIR` | `.` | Where the suite lives. |
| `RUNTESTS_SUITE_ROOT` | `tests/sample` | Suites are at `<workdir>/<suite_root>/<product>`. |
| `RUNNER_JOB_TIMEOUT` | `900` | A run past this is killed and reported as a timeout. |
| `RUNNER_HEARTBEAT_INTERVAL` | `30` | Overridden by whatever the edge returns at enrolment. |
| `RUNNER_RETRY_SECONDS` | `5` | First backoff after a failed call to the edge. |
| `RUNNER_MAX_RETRY_SECONDS` | `60` | Ceiling on that backoff. A test server that cannot reach the edge must keep trying — it is the only side that can open a connection — but retrying every 5s forever against an edge that is down is a busy loop with a network attached. |
| `SLACK_BOT_TOKEN` | *(empty)* | **Empty means every message prints to this machine's console** instead of being sent. |
| `SLACK_DEFAULT_CHANNEL` | `#testing` | Used when a job carries no channel. |

## What it says in Slack

Four messages per job, from this machine — never from the edge:

| | Example |
|---|---|
| **received** | `📥 Received webapp on staging (a1b2c3d4e5f6) — picked up by runner-2.` |
| **started** | `▶️ Running webapp on staging` + the exact pytest command |
| **complete** | `✅ Finished webapp on staging in 4.2s — passed.` |
| **summary** | `✅ 12 passed · 0 failed · 1 skipped · in 4.2s` (plus failed test ids) |

Four separate messages rather than one edited in place, because these are four
distinct facts and someone scrolling back wants the sequence. Progress *within*
a run is what `SlackNotifier.update()`'s throttling is for; these are
milestones.

**With no `SLACK_BOT_TOKEN` every one of them is printed instead**, saying
exactly what it would have sent and where:

```
[slack:dry-run] post -> #testing
  📥 Received webapp on staging (a1b2c3d4e5f6) — picked up by runner-2.
```

That fallback is the design, not a debugging convenience. Raising when
credentials are absent means nobody calls the reporter from a laptop; silently
doing nothing means you cannot tell "correctly inert" from "misconfigured in CI
and posting nowhere", and you find out weeks later when someone asks why the
channel went quiet.

## Things not to tidy up

- **The counts come from the JUnit XML, not from pytest's stdout.** The V1
  prototype scraped the summary line and said in its own comments that this was
  a compromise. Reading the report pytest already wrote gives exact numbers and
  is the only way to name the failed tests.
- **`validate()` re-checks the job against the same allowlists the edge used.**
  A signature proves *who* sent a job, not that its contents are sane. If the
  edge is ever compromised, or grows a code path that skips a check, this is
  what stands between a job payload and a subprocess on an internal machine.
- **The heartbeat is a thread, not an async task.** The job is a blocking
  subprocess; an event loop would stop being serviced for the whole run, the
  heartbeats would stop, and the edge would requeue a job that is running
  perfectly well. A healthy server declared dead because it was busy is exactly
  the failure this shape avoids.
- **stdout is captured and never posted.** A run's output in a channel is how a
  channel gets muted, and how internal hostnames and stack traces reach a
  searchable, wide audience. Counts in the message, detail in the XML.
- **One job at a time.** A server claiming a second job while the first runs
  would let one machine drain the queue, and would make results harder to
  attribute.
- **A failure reporting the result does not fail the run.** The Slack message
  has already gone out by then, so the person who asked has their answer; what
  is lost is the edge's record, which is worth two retries and not worth
  crashing the loop.

## Known limits

- `subprocess` + `--junit-xml`, not a pytest reporter plugin, so there is no
  live per-test progress — only the four milestones.
- No artifact upload. The JUnit XML stays on the machine that produced it.
- The suite must already be on this machine; there is no checkout step. In a
  real deployment that is a `git fetch` before the run, or a baked image.
