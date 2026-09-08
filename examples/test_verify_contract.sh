#!/usr/bin/env bash
# Does examples/verify.sh keep its own contract?
#
#     bash examples/test_verify_contract.sh
#
# WHY THIS EXISTS (NEH-1210)
#
# verify.sh is the example tier's verifier and a CI step. Its contract is two
# sentences long:
#
#   1. a run that reports failures must EXIT NON-ZERO
#   2. a run whose PRECONDITION failed must say so, by name, rather than
#      letting the missing precondition surface three checks later as
#      something else entirely
#
# Neither sentence was asserted anywhere, so (2) was false for months: the seed
# ended `>/dev/null 2>&1 || true`, a required field was added to JobDef, and
# the resulting TypeError read as "a correctly signed command is ACCEPTED
# through the proxy" -- signing, proxy, gate, none of which was wrong.
#
# HOW IT TESTS THE REAL SCRIPT WITHOUT DOCKER
#
# It runs the real examples/verify.sh with stub `docker` and `curl` first on
# PATH. The stubs are not a model of Docker; they are just enough for the real
# script to walk its real control flow, so what is under test here is the
# script's own arithmetic, reporting and exit status -- which is precisely what
# NEH-1210 is about, and what a real `docker compose up` tells you nothing
# extra about.
#
# BOTH DIRECTIONS, because a verifier that always fails is as broken as one
# that never does:
#
#   green      -> exit 0, 14 checks, 0 failures
#   seed broken-> exit non-zero, and the SEED is the check that is named
#   sig broken -> exit non-zero
#
# The middle case is the planted failure NEH-1210 describes. On the unfixed
# script it exits 0 with 12/12 green.

set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$(pwd)"

EXPECTED_CHECKS=14

failures=0
cases=0

STATE="$(mktemp -d "${TMPDIR:-/tmp}/verify-contract.XXXXXX")"
cleanup() { rm -rf "$STATE"; }
trap cleanup EXIT

mkdir -p "$STATE/bin"

# ── the stubs ───────────────────────────────────────────────────────────────

cat > "$STATE/bin/docker" <<'STUB'
#!/usr/bin/env bash
# Just enough docker for verify.sh to walk its own control flow.
marker="$STUB_STATE/last_example"
case "${1:-}" in
  build) exit 0 ;;
  inspect)
    # Called from the repo root, so it cannot see which example it is about.
    # `compose <dir> ps -q edge` runs immediately before it and records that.
    case "$(cat "$marker" 2>/dev/null || echo standalone)" in
      standalone) printf '{"8500/tcp":[{"HostIp":"127.0.0.1","HostPort":"8500"}]}\n' ;;
      *)          printf '{}\n' ;;
    esac
    exit 0 ;;
  compose) shift ;;
  *) exit 0 ;;
esac

example="$(basename "$PWD")"
printf '%s' "$example" > "$marker"

sub="${1:-}"; shift || true
case "$sub" in
  up)
    case " $* " in
      *" --wait "*) exit 0 ;;
      # The first `up -d` is the UNEDITED .env, which must be refused by name.
      *) printf 'edge-1 | SLACK_SIGNING_SECRET is not set; refusing to start\n' >&2
         printf 'dependency failed to start: container edge-1 exited (2)\n'
         exit 1 ;;
    esac ;;
  down) exit 0 ;;
  logs)
    if [ "$example" = standalone ]; then
      printf 'edge-1 | store ready: sqlite /data/testrunner.db\n'
    else
      printf 'edge-1 | store ready: postgres\n'
    fi
    exit 0 ;;
  ps) printf 'stub-container-id\n'; exit 0 ;;
  exec)
    if [ "${STUB_BREAK:-}" = seed ]; then
      # Exactly the NEH-1210 shape: JobDef gains a required field.
      printf 'Traceback (most recent call last):\n' >&2
      printf "TypeError: JobDef.__init__() missing 1 required positional argument: 'language'\n" >&2
      exit 1
    fi
    printf 'seeded 1 job definition(s)\n'
    exit 0 ;;
  *) exit 0 ;;
esac
STUB

cat > "$STATE/bin/curl" <<'STUB'
#!/usr/bin/env bash
url=""; tampered=0
for a in "$@"; do
  case "$a" in http://*) url="$a" ;; esac
  case "$a" in *tampered=1*) tampered=1 ;; esac
done
case "$url" in
  *:8500/healthz|*api/testrunner/healthz) printf '{"ok":true}\n'; exit 0 ;;
  *slack/commands)
    if [ "$tampered" -eq 1 ] || [ "${STUB_BREAK:-}" = signature ]; then
      printf '{"error":"bad signature"}\n'; exit 0
    fi
    printf '{"response_type":"ephemeral","text":"Queued job example-1"}\n'; exit 0 ;;
esac
printf 'unreachable\n'; exit 7
STUB

chmod +x "$STATE/bin/docker" "$STATE/bin/curl"

# ── the harness ─────────────────────────────────────────────────────────────

run_verify() { # $1 = STUB_BREAK value ("" for a clean run); writes $STATE/out
  STUB_STATE="$STATE" STUB_BREAK="$1" PATH="$STATE/bin:$PATH" \
    timeout 120 bash "$REPO/examples/verify.sh" > "$STATE/out" 2>&1
  printf '%s' $? > "$STATE/code"
}

expect() { # $1 = name, $2 = expected, $3 = actual
  cases=$((cases + 1))
  if [ "$2" = "$3" ]; then
    printf '  ok    %s\n' "$1"
  else
    printf '  FAIL  %s\n        expected: %s\n        actual:   %s\n' "$1" "$2" "$3"
    failures=$((failures + 1))
  fi
}

expect_contains() { # $1 = name, $2 = needle, $3 = haystack
  cases=$((cases + 1))
  case "$3" in
    *"$2"*) printf '  ok    %s\n' "$1" ;;
    *) printf '  FAIL  %s\n        expected to contain: %s\n        got:\n%s\n' \
         "$1" "$2" "$(printf '%s' "$3" | tail -c 800)"
       failures=$((failures + 1)) ;;
  esac
}

summary_line() { grep -E '^== [0-9]+ check\(s\)' "$STATE/out" | tail -1; }
checks_ran()   { summary_line | sed -E 's/^== ([0-9]+) check.*/\1/'; }
fails_ran()    { summary_line | sed -E 's/.* ([0-9]+) failure\(s\).*/\1/'; }

# ── direction 1: a clean run still PASSES ───────────────────────────────────
#
# Without this, "make it exit non-zero" is satisfied by a script that always
# exits non-zero, which is not a verifier.

printf '== a clean run passes, and says how many checks it made ==\n'
set +e; run_verify ""; set -e
code="$(cat "$STATE/code")"
printf '  input set: %s check(s) reported\n' "$(checks_ran)"
expect 'a clean run exits 0'            '0'                "$code"
expect 'it ran every check'             "$EXPECTED_CHECKS" "$(checks_ran)"
expect 'it reported no failures'        '0'                "$(fails_ran)"
expect_contains 'the seed is a check of its own' \
  'the example job definition seeds' "$(cat "$STATE/out")"

# ── direction 2: a broken PRECONDITION fails, by name ───────────────────────
#
# THE NEH-1210 PLANT. On the unfixed script the seed is silenced, every later
# check still passes against the stubs, and this run exits 0 -- so all four
# assertions below fail on unfixed code and pass on fixed code.

printf '\n== a seed that raises fails the run, and is named ==\n'
set +e; run_verify "seed"; set -e
code="$(cat "$STATE/code")"
out="$(cat "$STATE/out")"
printf '  input set: %s check(s) reported\n' "$(checks_ran)"
expect 'a failed seed exits non-zero' 'nonzero' \
  "$([ "$code" -ne 0 ] && echo nonzero || echo zero)"
expect 'the run still made every check'  "$EXPECTED_CHECKS" "$(checks_ran)"
expect_contains 'the SEED is the check that fails' \
  'FAIL  the example job definition seeds' "$out"
expect_contains 'it prints why, instead of discarding it' 'TypeError' "$out"
expect_contains 'it says the rest of the run is unsound' 'UNSOUND' "$out"

# ── direction 3: a real check failure fails the run ─────────────────────────

printf '\n== a failing check exits non-zero ==\n'
set +e; run_verify "signature"; set -e
code="$(cat "$STATE/code")"
out="$(cat "$STATE/out")"
printf '  input set: %s check(s) reported\n' "$(checks_ran)"
expect 'a reported failure exits non-zero' 'nonzero' \
  "$([ "$code" -ne 0 ] && echo nonzero || echo zero)"
expect 'the summary counts it' 'counted' \
  "$([ "$(fails_ran)" -ge 1 ] 2>/dev/null && echo counted || echo uncounted)"
expect_contains 'and names the check that failed' \
  'a correctly signed command is ACCEPTED through the proxy' "$out"

# ── the shape sweep ─────────────────────────────────────────────────────────
#
# The defect was a precondition ending `|| true`. Assert the seed no longer
# does, so a future edit cannot silently reintroduce it.

printf '\n== the seed is not silenced ==\n'
seed_block="$(sed -n '/^seed_out="\$(compose embedded exec/,/^seed_code=\$?$/p' "$REPO/examples/verify.sh")"
cases=$((cases + 1))
cases=$((cases + 1))
case "$seed_block" in
  '') printf '  FAIL  the seed block was not found -- this check is vacuous\n'
      failures=$((failures + 1)) ;;
  *) printf '  ok    found the seed block (%d line(s))\n' "$(printf '%s\n' "$seed_block" | wc -l)" ;;
esac
case "$seed_block" in
  *'>/dev/null'*|*'|| true'*)
    printf '  FAIL  the seed discards its own output or exit status\n'
    failures=$((failures + 1)) ;;
  *) printf '  ok    the seed keeps its output and its exit status\n' ;;
esac

printf '\n== %d case(s), %d failure(s) ==\n' "$cases" "$failures"
[ "$cases" -gt 0 ] || { printf 'UNSOUND: no cases ran\n' >&2; exit 1; }
[ "$failures" -eq 0 ] || exit 1
