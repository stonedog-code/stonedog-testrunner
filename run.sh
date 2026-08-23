#!/usr/bin/env bash
# run.sh — start the API (or this project's own tests) on either machine.
#
#     bash run.sh                    # serve on :8500 (V1, local mode)
#     bash run.sh serve              # same
#     bash run.sh edge               # V3: the edge server on :8500
#     bash run.sh runner             # V3: a test server, dialling out to the edge
#     bash run.sh test               # this project's own unit suite
#     bash run.sh test:integration   # the integration tier (starts an edge itself)
#     bash run.sh test:conformance   # the store suite, against every backend
#     bash run.sh docker             # edge + three test servers, in containers
#
# Environment passes through, so the V2 mode still works the documented way:
#
#     RUNTESTS_MODE=github bash run.sh
#
# Then, in a second terminal, poke it with the signed request:
#
#     bash test.sh
#
# Why this exists rather than a bare `uv run slack-runtests`: the workspace is a
# Samba share, so the Mac and the Linux box see the SAME `.venv`, which is a
# Linux one — and using it from macOS fails with a misleading
# "Failed to spawn: No such file or directory". See scripts/uv-env.sh for the
# mechanism. This picks the right per-platform environment and syncs it first.
#
# Written for bash 3.2 — that is what macOS ships as /bin/bash.

set -euo pipefail

cd "$(dirname "$0")"
# shellcheck source=scripts/uv-env.sh
. ./scripts/uv-env.sh

uv sync --quiet

# Both servers now REFUSE to start with no signing secret and no allowlist,
# because an unset allowlist used to allow everyone and nothing said so. That
# refusal must not make `bash run.sh` stop working on a laptop, so this sets the
# opt-out — and PRINTS that it did. The variable is never set in an image, and
# an existing value is left alone so `SLACK_SIGNING_SECRET=... bash run.sh edge`
# still exercises the configured path.
# NOTHING configured, not "something missing". This distinction is the whole
# safety of the function and the first version got it wrong: it set the opt-out
# whenever ANY protection was absent, so a deployment with a signing secret, a
# team id and a TYPO in RUNTESTS_CHANNELS was silently opted into insecure mode
# by its own launcher — restoring the exact fail-open this change exists to
# close, and doing it to the operator who was visibly trying to configure it.
#
# A fresh checkout has none of them set. Somebody who has set even one is
# configuring this deliberately, and deserves the gate's refusal naming what is
# still missing, not a launcher that decides for them.
dev_mode_if_unconfigured() {
  if [ -n "${RUNTESTS_INSECURE_DEV:-}" ]; then
    return
  fi
  if [ -n "${SLACK_SIGNING_SECRET:-}" ] || [ -n "${SLACK_TEAM_ID:-}" ] \
     || [ -n "${RUNTESTS_CHANNELS:-}" ] || [ -n "${RUNTESTS_USERS:-}" ]; then
    return
  fi
  printf 'no Slack protection configured at all — setting RUNTESTS_INSECURE_DEV=1 for local dev\n' >&2
  printf 'set any one of SLACK_SIGNING_SECRET / SLACK_TEAM_ID / RUNTESTS_CHANNELS / RUNTESTS_USERS\n' >&2
  printf 'and this stops, so a half-configured server refuses to start instead.\n' >&2
  export RUNTESTS_INSECURE_DEV=1
}

cmd="${1:-serve}"
[ $# -gt 0 ] && shift

case "$cmd" in
  serve)
    dev_mode_if_unconfigured
    printf 'serving from %s — mode=%s\n' "$UV_PROJECT_ENVIRONMENT" "${RUNTESTS_MODE:-local}"
    exec uv run slack-runtests "$@"
    ;;
  edge)
    dev_mode_if_unconfigured
    printf 'edge server — slack=%s\n' "${SLACK_SIGNING_SECRET:+configured}${SLACK_SIGNING_SECRET:-UNVERIFIED}"
    exec uv run slack-runtests-edge "$@"
    ;;
  runner)
    printf 'test server %s -> %s\n' "${RUNNER_ID:-$(hostname)}" "${EDGE_URL:-http://127.0.0.1:8500}"
    exec uv run slack-runtests-runner "$@"
    ;;
  test)
    exec uv run pytest "$@"
    ;;
  test:conformance)
    # The store suite alone, so it can be pointed at a database and re-run
    # without waiting for everything else. It runs in `test` as well, against
    # SQLite; TESTRUNNER_TEST_POSTGRES_DSN is what adds the Postgres parameter,
    # and TESTRUNNER_REQUIRED_BACKENDS is what makes a missing one a FAILURE
    # rather than a suite that silently covers half of what it claims to.
    exec uv run pytest tests/conformance "$@"
    ;;
  test:integration)
    # A separate path because these spawn a real uvicorn process and talk to it
    # over a real socket. They are excluded from `testpaths` so the fast unit
    # gate stays fast and stays honest about what it covers.
    exec uv run pytest tests/integration "$@"
    ;;
  docker)
    exec docker compose -f docker/compose.yml "${@:-up}" 
    ;;
  *)
    printf 'usage: %s [serve|edge|runner|test|test:conformance|test:integration|docker] [args...]\n' "$0" >&2
    exit 2
    ;;
esac
