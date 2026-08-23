#!/usr/bin/env bash
# Bring both example deployments up for real, and check them.
#
#     bash examples/verify.sh
#
# WHY THIS EXISTS
#
# A configuration example nobody runs is a configuration example that rots, and
# it rots invisibly: the compose file still parses, the README still reads
# correctly, and the first person to discover it is broken is a stranger who
# trusted it. Everything below is a real `docker compose up` against the real
# images.
#
# The check that earns this script's existence is the LAST one. Slack's
# signature is computed over the raw request body bytes, so a proxy that parses
# the form and re-serialises it rejects every real command as unsigned — while
# passing any test that signs whatever it is given. So this signs a FIXED body
# with a known secret, sends it through the proxy, and fails if the far side
# refuses it.
#
# Written for bash 3.2 — that is what macOS ships as /bin/bash.

set -euo pipefail

cd "$(dirname "$0")/.."
EXAMPLES="$(pwd)/examples"

EDGE_IMAGE="${EDGE_IMAGE:-slack-runtests-edge:examples}"
SECRET="verify-signing-secret"
TEAM="T_VERIFY"
CHANNEL="C_VERIFY"

failures=0
checks=0

check() {
  checks=$((checks + 1))
  if [ "$2" = "$3" ]; then
    printf '  ok    %s\n' "$1"
  else
    printf '  FAIL  %s\n        expected: %s\n        actual:   %s\n' "$1" "$2" "$3"
    failures=$((failures + 1))
  fi
}

contains() {
  checks=$((checks + 1))
  case "$3" in
    *"$2"*) printf '  ok    %s\n' "$1" ;;
    *) printf '  FAIL  %s\n        expected to contain: %s\n        got: %s\n' \
         "$1" "$2" "$(printf '%s' "$3" | tail -c 500)"
       failures=$((failures + 1)) ;;
  esac
}

compose() {
  # $1 = example directory, rest = compose args
  local dir="$1"; shift
  ( cd "$EXAMPLES/$dir" && docker compose "$@" )
}

down_all() {
  compose standalone down -v --remove-orphans >/dev/null 2>&1 || true
  compose embedded  down -v --remove-orphans >/dev/null 2>&1 || true
  rm -f "$EXAMPLES/standalone/.env" "$EXAMPLES/embedded/.env"
}
trap down_all EXIT

printf '== building the edge image the examples use ==\n'
docker build --quiet --target edge -t "$EDGE_IMAGE" -f docker/Dockerfile . >/dev/null
printf '  built %s\n' "$EDGE_IMAGE"

# The examples name a published image nobody has pulled. Point them at the one
# just built, without editing the files a reader is meant to copy.
export TESTRUNNER_EDGE_IMAGE="$EDGE_IMAGE"

# ── the examples must refuse an UNEDITED copy ───────────────────────────────
#
# This is the promise the whole "copy this and edit it" arrangement rests on: a
# stranger who copies the example and forgets gets a refusal, not an open
# endpoint. It is checked before anything else, because if it is false the rest
# of the example is a liability.
printf '== an unedited example must refuse to start ==\n'
cp "$EXAMPLES/standalone/.env.example" "$EXAMPLES/standalone/.env"
set +e
unedited="$(compose standalone up -d 2>&1)"
unedited_code=$?
set -e
compose standalone down -v >/dev/null 2>&1 || true

check 'compose refuses an unedited .env' 'nonzero' \
  "$([ "$unedited_code" -ne 0 ] && echo nonzero || echo zero)"
contains 'it names the setting that is missing' 'SLACK_SIGNING_SECRET' "$unedited"

# ── strategy A ──────────────────────────────────────────────────────────────
printf '== strategy A — standalone ==\n'
cat > "$EXAMPLES/standalone/.env" <<ENV
SLACK_SIGNING_SECRET=$SECRET
SLACK_TEAM_ID=$TEAM
RUNTESTS_CHANNELS=$CHANNEL
RUNTESTS_USERS=
EDGE_ADMIN_TOKEN=
ENV
compose standalone up -d --wait --wait-timeout 120 >/dev/null 2>&1 || true

standalone_health="$(curl -fsS --max-time 5 http://127.0.0.1:8500/healthz 2>&1 || echo "unreachable")"
standalone_logs="$(compose standalone logs edge 2>&1 || true)"

contains 'it answers /healthz on localhost' '"ok"'          "$standalone_health"
contains 'it opened a SQLite store'         'store ready: sqlite' "$standalone_logs"

# Published on the LOOPBACK only. `- "8500:8500"` would bind every interface,
# which on a cloud VM is the public one — an unencrypted public endpoint one
# character away from a correct configuration.
bindings="$(docker inspect -f '{{json .HostConfig.PortBindings}}' \
  "$(compose standalone ps -q edge)" 2>/dev/null || echo '{}')"
contains 'it publishes on 127.0.0.1 only' '127.0.0.1' "$bindings"

compose standalone down -v >/dev/null 2>&1 || true

# ── strategy B ──────────────────────────────────────────────────────────────
printf '== strategy B — embedded sidecar ==\n'

# THE HARD CASE, run rather than described. Postgres gets the password raw and
# the DSN gets it percent-encoded — two encodings of one secret, which is the
# trap this example documents. A tame alphanumeric password here would prove
# nothing about the case that actually breaks, and would let a broken
# two-variable path ship looking tested.
PG_PASSWORD_RAW='p@ss/w0rd#1?x'
PG_PASSWORD_ENCODED="$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=""))' "$PG_PASSWORD_RAW")"
printf '  using a password with reserved characters: %s -> %s\n' "$PG_PASSWORD_RAW" "$PG_PASSWORD_ENCODED"
cat > "$EXAMPLES/embedded/.env" <<ENV
SLACK_SIGNING_SECRET=$SECRET
SLACK_TEAM_ID=$TEAM
RUNTESTS_CHANNELS=$CHANNEL
RUNTESTS_USERS=
POSTGRES_USER=testrunner
POSTGRES_PASSWORD=$PG_PASSWORD_RAW
EDGE_DB_PASSWORD_ENCODED=$PG_PASSWORD_ENCODED
POSTGRES_DB=testrunner
APP_PORT=18080
ENV
compose embedded up -d --wait --wait-timeout 180 >/dev/null 2>&1 || true

embedded_logs="$(compose embedded logs edge 2>&1 || true)"
contains 'the sidecar opened a POSTGRES store' 'store ready: postgres' "$embedded_logs"
# The DSN carries a password. It must not reach the log.
case "$embedded_logs" in
  *"$PG_PASSWORD_ENCODED"*|*"$PG_PASSWORD_RAW"*)
    printf '  FAIL  the store password reached the log\n'; failures=$((failures + 1)) ;;
  *) printf '  ok    the store password never reaches the log\n' ;;
esac
checks=$((checks + 1))

# NO published port. The absence is the security property, so it is asserted
# rather than described.
edge_ports="$(docker inspect -f '{{json .HostConfig.PortBindings}}' \
  "$(compose embedded ps -q edge)" 2>/dev/null || echo 'unknown')"
check 'the sidecar publishes NO port' '{}' "$edge_ports"

proxied_health="$(curl -fsS --max-time 5 http://127.0.0.1:18080/api/testrunner/healthz 2>&1 || echo "unreachable")"
contains 'it is reachable only through the app in front' '"ok"' "$proxied_health"

# ── the check this script exists for ────────────────────────────────────────
printf '== the raw-body signature must survive the proxy ==\n'
BODY='team_id=T_VERIFY&channel_id=C_VERIFY&channel_name=testing&user_id=U_VERIFY&command=%2Fruntests&text=-p+webapp&trigger_id=verify-trigger-1'
TS="$(date +%s)"
SIG="v0=$(printf 'v0:%s:%s' "$TS" "$BODY" \
  | openssl dgst -sha256 -hmac "$SECRET" -r | cut -d' ' -f1)"

signed="$(curl -sS --max-time 10 \
  -X POST "http://127.0.0.1:18080/api/testrunner/slack/commands" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -H "X-Slack-Request-Timestamp: $TS" \
  -H "X-Slack-Signature: $SIG" \
  --data-raw "$BODY" 2>&1 || echo "request failed")"

# Accepted means the bytes arrived unchanged. A proxy that re-serialised the
# form would produce "signature check failed" here and nowhere else.
contains 'a correctly signed command is ACCEPTED through the proxy' 'Queued' "$signed"
case "$signed" in
  *signature*|*unauthor*|*401*)
    printf '  FAIL  the proxy altered the body: the signature no longer verifies\n'
    failures=$((failures + 1)) ;;
  *) printf '  ok    the signature still verifies on the far side\n' ;;
esac
checks=$((checks + 1))

# The other direction: a body the signature does NOT cover must be refused, or
# the check above would pass against a server that verifies nothing.
tampered="$(curl -sS --max-time 10 \
  -X POST "http://127.0.0.1:18080/api/testrunner/slack/commands" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -H "X-Slack-Request-Timestamp: $TS" \
  -H "X-Slack-Signature: $SIG" \
  --data-raw "${BODY}&tampered=1" 2>&1 || echo "request failed")"
# ASSERT THE REFUSAL, not merely the absence of acceptance. "does not contain
# Queued" is also true of a 502, a timeout, a crashed container and a typo in
# the URL — so written that way, a security check passes on every kind of
# connection failure. The edge answers a bad signature with 401 and
# {"error":"bad signature"}, and that is what must arrive.
contains 'a tampered body is REFUSED, by name' 'bad signature' "$tampered"

printf '\n== %d check(s), %d failure(s) ==\n' "$checks" "$failures"
[ "$failures" -eq 0 ] || exit 1
