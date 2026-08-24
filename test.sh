#!/usr/bin/env bash
# test.sh — POST to the locally running server to start a test run.
#
#     bash test.sh                              # /runtests -p webapp -s staging --test_scope smoke
#     bash test.sh -- -p webapp -k smoke        # pass your own flags
#     bash test.sh --results                    # ask for the last run
#     bash test.sh --bad                        # a rejected command, to see the error
#     bash test.sh --unsigned                   # omit the signature (expect 401 when configured)
#
# Start the server first, in another terminal:
#     bash run.sh
#
# IT SENDS A REAL, SIGNED SLACK REQUEST. The signature is computed with the same
# function the server verifies with (`signature.sign` / `signature.is_valid`),
# so this exercises the real code path. There is deliberately no "skip auth in
# dev" flag in the server — that flag is the one that eventually ships. If
# SLACK_SIGNING_SECRET is unset the server accepts unverified requests and says
# so loudly on every one; set it here and there to test the verified path.
#
# Invoke with `bash`, not ./test.sh. The old reason was exFAT, which had no
# executable bit; the stick is gone and this is now ext4 over SMB, but the Mac
# may still not see the bit through the share, so `bash` stays the safe form.

set -euo pipefail

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8500}"
URL="http://${HOST}:${PORT}/slack/commands"
SECRET="${SLACK_SIGNING_SECRET:-}"

# Values that mimic a real Slack slash-command payload.
TEAM_ID="${SLACK_TEAM_ID:-T0123456789}"
CHANNEL_ID="${SLACK_CHANNEL_ID:-C0123456789}"
CHANNEL_NAME="${SLACK_CHANNEL_NAME:-#testing}"
USER_ID="${SLACK_USER_ID:-U0123456789}"

COMMAND_TEXT="-p webapp -s staging --test_scope smoke"
SIGN=1

while [ $# -gt 0 ]; do
  case "$1" in
    --results)  COMMAND_TEXT="results -p webapp"; shift ;;
    --bad)      COMMAND_TEXT="-p ../../etc -s prod"; shift ;;
    --unsigned) SIGN=0; shift ;;
    --)         shift; COMMAND_TEXT="$*"; break ;;
    *)          COMMAND_TEXT="$*"; break ;;
  esac
done

# A fresh trigger_id per run, because the server keys idempotency on it — reuse
# it and the second call is correctly refused as a duplicate. Two invocations of
# this script should start two runs; that is what makes it useful for poking at
# the server, and it is why this is not a fixed string.
TRIGGER_ID="$(date +%s%N)-$$"
TIMESTAMP="$(date +%s)"

# urlencode with python: the command text contains spaces and quotes, and
# hand-rolling percent-encoding in bash is how you get a signature that does not
# match the body you actually sent.
BODY="$(python3 - "$COMMAND_TEXT" "$TEAM_ID" "$CHANNEL_ID" "$CHANNEL_NAME" "$USER_ID" "$TRIGGER_ID" <<'PY'
import sys, urllib.parse
text, team, chan_id, chan_name, user, trigger = sys.argv[1:7]
print(urllib.parse.urlencode({
    "token": "gIkuvaNzQIHg97ATvDxqgjtO",
    "team_id": team,
    "team_domain": "example",
    "channel_id": chan_id,
    "channel_name": chan_name.lstrip("#"),
    "user_id": user,
    "user_name": "qa.bot",
    "command": "/runtests",
    "text": text,
    "api_app_id": "A0123456789",
    "response_url": "https://hooks.slack.com/commands/1234/5678",
    "trigger_id": trigger,
}), end="")
PY
)"

HEADERS=(-H "Content-Type: application/x-www-form-urlencoded")
if [ "$SIGN" -eq 1 ] && [ -n "$SECRET" ]; then
  SIG="$(python3 - "$BODY" "$TIMESTAMP" "$SECRET" <<'PY'
import sys, hashlib, hmac
body, ts, secret = sys.argv[1], sys.argv[2], sys.argv[3]
base = b"v0:" + ts.encode() + b":" + body.encode()
print("v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest(), end="")
PY
)"
  HEADERS+=(-H "X-Slack-Request-Timestamp: $TIMESTAMP" -H "X-Slack-Signature: $SIG")
  printf '  signed with SLACK_SIGNING_SECRET\n'
elif [ "$SIGN" -eq 1 ]; then
  printf '  no SLACK_SIGNING_SECRET set — sending unsigned (server will accept and warn)\n'
else
  printf '  --unsigned: deliberately omitting the signature\n'
fi

printf '  POST %s\n  text: %s\n\n' "$URL" "$COMMAND_TEXT"

HTTP_CODE="$(curl -sS -o /tmp/slack-runtests-response.json -w '%{http_code}' \
  -X POST "$URL" "${HEADERS[@]}" --data "$BODY")" || {
    printf '\nCould not reach %s — is the server running?\n  bash run.sh\n' "$URL" >&2
    exit 1
  }

printf 'HTTP %s\n' "$HTTP_CODE"
python3 -m json.tool /tmp/slack-runtests-response.json 2>/dev/null || cat /tmp/slack-runtests-response.json
printf '\n'

# The 200 only means the command was ACCEPTED. The run happens in a background
# task and reports to Slack (or, with no token, to the server's console) — so
# watch the server's output for the result.
if [ "$HTTP_CODE" = "200" ]; then
  printf '  Accepted. Watch the SERVER console for the run and its Slack output.\n'
fi
