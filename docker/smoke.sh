#!/usr/bin/env bash
# Build both images and check what they ARE, not that the build exited 0.
#
#     bash docker/smoke.sh
#
# WHY THIS EXISTS
#
# A Dockerfile that builds proves almost nothing. The two targets differ by a
# handful of COPY lines and two `uv sync` flags, and every way of getting those
# wrong produces an image that builds perfectly and is wrong at runtime:
#
#   * an edge with pytest and the sample suite in it — the public process able
#     to run code it should never be able to run;
#   * an edge without psycopg — fine until the first Slack command on the
#     deployment whose whole point is the Postgres store;
#   * a runner WITHOUT pytest — it starts, enrols, claims a job, and only then
#     fails to run it, which reads as a queue bug;
#   * an edge whose CMD is `uv run`, which re-syncs at container start and
#     reinstalls the dev group, silently undoing the first point above.
#
# Every check below prints what it examined. A smoke script that says only
# "ok" is the same green-over-an-empty-set this repo keeps writing about.
#
# Written for bash 3.2 — that is what macOS ships as /bin/bash.

set -euo pipefail

cd "$(dirname "$0")/.."

EDGE_IMAGE="${EDGE_IMAGE:-slack-runtests-edge:smoke}"
RUNNER_IMAGE="${RUNNER_IMAGE:-slack-runtests-runner:smoke}"
CONTAINER="slack-runtests-smoke-$$"

failures=0
checks=0

check() {
  # check <description> <expected> <actual>
  checks=$((checks + 1))
  if [ "$2" = "$3" ]; then
    printf '  ok    %s\n' "$1"
  else
    printf '  FAIL  %s\n        expected: %s\n        actual:   %s\n' "$1" "$2" "$3"
    failures=$((failures + 1))
  fi
}

contains() {
  # contains <description> <needle> <haystack>
  checks=$((checks + 1))
  case "$3" in
    *"$2"*) printf '  ok    %s\n' "$1" ;;
    *) printf '  FAIL  %s\n        expected to contain: %s\n        got: %s\n' \
         "$1" "$2" "$(printf '%s' "$3" | tail -c 400)"
       failures=$((failures + 1)) ;;
  esac
}

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

printf '== building ==\n'
docker build --quiet --target edge   -t "$EDGE_IMAGE"   -f docker/Dockerfile . >/dev/null
docker build --quiet --target runner -t "$RUNNER_IMAGE" -f docker/Dockerfile . >/dev/null
printf '  built %s and %s\n' "$EDGE_IMAGE" "$RUNNER_IMAGE"

# ── what is in each image ────────────────────────────────────────────────────
#
# The inventory is the deliverable. `psycopg` is present in BOTH and that is not
# an oversight: the conformance suite needs it, so it lives in the dev group
# that the runner image installs. Asserting what is true beats asserting what
# would have been tidier.
inventory() {
  docker run --rm --entrypoint python "$1" -c '
import importlib.util as u
mods = ("edge_server", "test_server", "slack_runtests", "psycopg", "pytest")
out = []
for m in mods:
    out.append(m + "=" + ("Y" if u.find_spec(m) else "N"))
print(" ".join(out))
'
}

printf '== image contents (5 modules per image) ==\n'
edge_inv="$(inventory "$EDGE_IMAGE")"
runner_inv="$(inventory "$RUNNER_IMAGE")"
printf '  edge   : %s\n' "$edge_inv"
printf '  runner : %s\n' "$runner_inv"

contains 'the edge can serve'                    'edge_server=Y'   "$edge_inv"
contains 'the edge can reach Postgres'           'psycopg=Y'       "$edge_inv"
contains 'THE EDGE CANNOT RUN A SUITE'           'pytest=N'        "$edge_inv"
contains 'the edge does not carry the test server' 'test_server=N' "$edge_inv"
contains 'the test server can run a suite'       'pytest=Y'        "$runner_inv"
contains 'the test server can dial the edge'     'test_server=Y'   "$runner_inv"
contains 'the test server is not also the edge'  'edge_server=N'   "$runner_inv"

# ── the edge refuses an unconfigured start ───────────────────────────────────
printf '== an unconfigured edge must refuse to start ==\n'
set +e
unconfigured="$(docker run --rm --name "$CONTAINER" "$EDGE_IMAGE" 2>&1)"
unconfigured_code=$?
set -e
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true

check 'it exits non-zero' 'nonzero' "$([ "$unconfigured_code" -ne 0 ] && echo nonzero || echo "zero($unconfigured_code)")"
contains 'it says why'                'REFUSING TO START'  "$unconfigured"
contains 'it names the signing secret' 'SLACK_SIGNING_SECRET' "$unconfigured"

# ── a configured edge serves, and says what store it opened ──────────────────
printf '== a configured edge must serve ==\n'
docker run -d --rm --name "$CONTAINER" \
  -e SLACK_SIGNING_SECRET=smoke-secret \
  -e SLACK_TEAM_ID=T_SMOKE \
  -e RUNTESTS_CHANNELS=C_SMOKE \
  -e HOST=0.0.0.0 \
  -e EDGE_DB_PATH=/app/data/edge.db \
  -e EDGE_KEY_PATH=/app/keys/edge.pem \
  "$EDGE_IMAGE" >/dev/null

health=""
for _ in $(seq 1 40); do
  health="$(docker exec "$CONTAINER" python -c '
import urllib.request
try:
    print(urllib.request.urlopen("http://127.0.0.1:8500/healthz", timeout=2).read().decode())
except Exception as exc:
    print("not-yet:", exc)
' 2>&1 || true)"
  case "$health" in *'"ok"'*) break ;; esac
  sleep 0.5
done
started="$(docker logs "$CONTAINER" 2>&1 || true)"

# AFTER it has started, not only after it was built. The inventory above runs
# `--entrypoint python`, which never executes the image's CMD — so an edge whose
# CMD is `uv run` would pass every check so far and then reinstall the dev group
# at container start, putting pytest back into the running process. This is the
# only check that can see that, and it is the reason the CMD is the console
# script directly.
running_pytest="$(docker exec "$CONTAINER" python -c '
import importlib.util as u
print("present" if u.find_spec("pytest") else "absent")
' 2>&1 || true)"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true

check 'pytest is still absent from the RUNNING edge' 'absent' "$running_pytest"

contains 'it answers /healthz'          '"ok"'          "$health"
contains 'it names the store backend'   'store: sqlite' "$started"
contains 'it names its concurrency caps' 'caps:'        "$started"
# The point of the previous check is that success and a silent fallback to the
# wrong store are otherwise identical in every log line that follows.
case "$started" in
  *'NOT PROTECTING ANYTHING'*)
    printf '  FAIL  a configured edge must not announce insecure mode\n'
    failures=$((failures + 1)) ;;
  *) printf '  ok    a configured edge does not announce insecure mode\n' ;;
esac
checks=$((checks + 1))

printf '\n== %d check(s), %d failure(s) ==\n' "$checks" "$failures"
[ "$failures" -eq 0 ] || exit 1
