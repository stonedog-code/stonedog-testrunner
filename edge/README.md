# Edge server

The public half. It receives Slack slash commands, proves they are genuine and
allowed, puts the work on a queue, and answers Slack — inside three seconds,
every time, because it does nothing else.

**Two things it deliberately does not do**, and both are the point:

- **It never runs a test.** The process answering the internet is never the
  process executing code.
- **It never calls the Slack API.** It answers Slack's HTTP request and that is
  all. Every message that appears in a channel is posted by a *test server*, so
  no Slack bot token needs to exist on the public host.

It also never opens a connection to anything inside your network. Test servers
dial out; the edge parks work and waits to be asked. No inbound port, no VPN,
no firewall hole.

```
Slack ──HTTPS──▶ EDGE ──(parks the job)──▶ queue
                   ▲                          │
                   └──── test server asks ────┘
```

## Run it

```bash
bash run.sh edge                       # from the repo root, serves on :8500
```

or the whole thing, edge plus three test servers, in containers:

```bash
docker compose -f docker/compose.yml up --build
```

Then drive it the way Slack would:

```bash
bash test.sh                           # a signed slash command
```

## Configuration

Everything is read from the environment in `edge_server/config.py`, and nowhere
else.

### The Slack door

| Variable | Default | What it does |
|---|---|---|
| `SLACK_SIGNING_SECRET` | *(empty)* | **Set this.** Empty means every Slack request is accepted **unverified**, with a warning logged on each one. That is how `test.sh` works out of the box; it is not a deployment setting. |
| `SLACK_TEAM_ID` | *(empty)* | Pin the workspace. A valid Slack signature only proves the request came from *Slack* — this is what proves it came from *your* Slack. |
| `RUNTESTS_CHANNELS` | *(empty)* | Comma-separated channel ids allowed to run tests. |
| `RUNTESTS_USERS` | *(empty)* | Comma-separated user ids allowed to run tests. |
| `SLACK_DEFAULT_CHANNEL` | `#testing` | Where results go if the payload carries no channel. |

The last three are empty by default, which means **no restriction**. On any real
team the workspace contains guests, contractors and Slack Connect users from a
customer, so leaving them empty is a decision, not a default to inherit.

### The test-server door

| Variable | Default | What it does |
|---|---|---|
| `EDGE_KEY_PATH` | `keys/edge_ed25519.pem` | The edge's own private key. Generated 0600 on first start. |
| `EDGE_TRUSTED_KEYS_DIR` | `trusted_runners` | Pre-authorised public keys, one file per test server: `<runner_id>.pub`. **This is the production enrolment path.** |
| `RUNNER_ENROLL_TOKEN` | *(empty)* | A shared bootstrap token that lets an *unknown* test server enrol itself. Convenient in a lab; the edge warns for as long as it is set. |
| `EDGE_ADMIN_TOKEN` | *(empty)* | Bearer token for `GET /admin/fleet`. Empty means that endpoint **404s** — default deny. |

### Queue and liveness

| Variable | Default | What it does |
|---|---|---|
| `EDGE_DB_PATH` | `data/edge.db` | SQLite file holding the queue and the registry. **The default, and the reason a standalone edge needs no database.** |
| `EDGE_DB_DSN` | *(empty)* | The **only** way to select Postgres — `postgresql://user:pass@host/db`. Wins over `EDGE_DB_PATH` when set. Needs the `postgres` extra (`uv sync --extra postgres`). |
| `EDGE_DB_BUSY_TIMEOUT` | `5` | How long a write waits for a lock before the caller is told the runner is busy. Bounded inside Slack's three-second budget on purpose. |
| `RUNTESTS_MAX_ACTIVE_PER_JOB` | `1` | Runs of the same (product, server) in flight at once. `0` disables. |
| `RUNTESTS_MAX_QUEUED_PER_CHANNEL` | `10` | Waiting runs one channel may have. `0` disables. |
| `RUNTESTS_MAX_RUNNING_PER_CHANNEL` | `3` | Runs of one channel executing at once. `0` disables. |
| `RUNNER_HEARTBEAT_INTERVAL` | `30` | How often a test server must check in. |
| `RUNNER_OFFLINE_AFTER` | `90` | No heartbeat for this long ⇒ marked offline and handed no new work. |
| `JOB_LEASE_SECONDS` | `120` | How long a claim is good for. Four heartbeats fit inside it, so a live server renews well before it could be declared dead. |
| `JOB_MAX_ATTEMPTS` | `2` | How many times a job may be requeued before it is abandoned. Stops one poisonous job taking down all three servers in turn. |
| `EDGE_POLL_TIMEOUT` | `25` | How long a long-poll is held open. **Keep this under 30s** — most proxies time an idle connection out there, and a 504 reads to a test server as an edge outage. |

## Registering a test server

Two paths. Both require the enrolling server to **sign the enrolment request
with the key it presents**, so nobody can register a key they do not hold —
which matters because the runner id is what result-ownership is checked
against, and squatting one would be enough to post forged results later.

### Production — pre-authorise the key

On the test server, start it once and read the two lines it logs:

```
INFO  test server web-runner-01
INFO    public key  9Zt0…44 characters of base64…=
INFO    fingerprint 1f4c9a02b77e5d31   <- give this to the edge operator
```

On the edge, save that public key under the runner's id:

```bash
mkdir -p trusted_runners
printf '%s' '9Zt0…' > trusted_runners/web-runner-01.pub
```

That is the whole handshake. Leave `RUNNER_ENROLL_TOKEN` unset, and the edge
will accept `web-runner-01` **only** when it presents that exact key. Revoking
it is deleting the file.

Confirm with the fingerprint rather than by eye — comparing two 44-character
base64 blobs visually is how the wrong key gets enrolled:

```bash
python -c "from slack_runtests import identity; print(identity.fingerprint(open('trusted_runners/web-runner-01.pub').read()))"
```

### Lab — a bootstrap token

Set `RUNNER_ENROLL_TOKEN` on the edge and the same value on each test server.
The first time a new id appears with the right token, its key is recorded and
the token stops mattering: every later request is authenticated by the key
alone. So a leaked token cannot impersonate a server that already enrolled —
the key on file will not match — but it *can* enrol new ones, which is why it
does not belong in production.

`docker/compose.yml` uses this path so three fresh containers come up with no
manual step.

### The other direction

Test servers verify the edge too. `GET /edge/identity` returns the edge's public
key and fingerprint; pin it on each test server with `EDGE_FINGERPRINT` so a
substituted edge is refused rather than obeyed. Unpinned is trust-on-first-use,
which is fine in a lab and not in production.

## Endpoints

| Method | Path | Who calls it |
|---|---|---|
| `POST` | `/slack/commands` | Slack. HMAC over the raw body, 5-minute replay window, then workspace/channel/user allowlists, then the wording allowlist. |
| `POST` | `/runner/enroll` | A test server, once. |
| `POST` | `/runner/heartbeat` | A test server, every `RUNNER_HEARTBEAT_INTERVAL`. Also renews its leases. |
| `POST` | `/runner/jobs/claim` | A test server. Long-poll; a job or `204`. |
| `POST` | `/runner/jobs/{id}/started` | A test server. |
| `POST` | `/runner/jobs/{id}/result` | A test server — **only the one holding the job**. |
| `GET` | `/edge/identity` | Anyone. It is a public key. |
| `GET` | `/healthz` | Anything. Reveals nothing about configuration. |
| `GET` | `/admin/fleet` | An operator with `EDGE_ADMIN_TOKEN`. `404` when unset. |

Every `/runner/*` request carries `X-Runner-Id`, `X-Runner-Timestamp` and
`X-Runner-Signature` — Ed25519 over `method · path · timestamp · sha256(body)`.
The method and path are signed on purpose: signing only the body would let a
captured, valid heartbeat be replayed against `…/result`.

Every `/runner/*` **reply** carries `X-Edge-Timestamp` and `X-Edge-Signature`.

## Things not to tidy up

- **A rejected command answers `200`, not `4xx`.** Slack's contract is that a
  user error is a 200 with an ephemeral body. Returning an HTTP error makes
  Slack show its own generic failure instead of the reason. Only a bad
  signature gets a real `401`, because that sender is not a person to help.
- **`/admin/fleet` 404s rather than 401s when no token is set.** Which internal
  machines exist and when they were last seen is reconnaissance; the endpoint
  should not admit to existing.
- **Result ownership is in the SQL**, not in a checker function — `WHERE id=?
  AND runner_id=?`. A boundary enforced by the query cannot be bypassed by a
  code path that forgets to call it.
- **`prod` is not a valid `-s` value** and is not missing by accident. A
  production run belongs behind an approval, not a chat box.

## Known limits

- With no `SLACK_SIGNING_SECRET` the edge accepts unverified requests and warns
  on every one. Deliberate, documented, and refused as soon as a secret is set.
- SQLite is right for one edge process and a handful of test servers, and it is
  the default for exactly that reason. **Set `EDGE_DB_DSN` when the process has
  no durable disk** — a container service with no persistent volume deletes the
  file on every redeploy, along with the queue, the in-flight leases and the
  whole run history — **or when more than one edge process shares a queue.**
  SQLite also has one writer, so a burst of commands produces a bounded wait and
  then an honest "the runner is busy"; Postgres has no such limit.
- **The two backends are one interface and one test suite.** `tests/conformance`
  runs every guarantee against both, and CI stands up a real Postgres to do it.
  A backend that silently fails to claim atomically is precisely what a
  single-backend suite cannot see — measured, not assumed: the first run of that
  suite found the Postgres cap letting four rows through where SQLite let three.
- There is no TLS here. Terminate it in front — see the `Gate 0` note in the
  architecture diagram.
