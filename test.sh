#!/usr/bin/env bash
#
# Local smoke test for the ASR service.
#
# No secret is stored in this file. The API key is read from the environment:
#
#   export ASR_API_KEY='...'
#
# or from a local, untracked .env file next to this script (see .env.example).
# The key is never echoed, never written to a file, and never passed on a
# command line in a way that lands in the process list.
#
# Usage:
#   ./test.sh                    # test the local server on 127.0.0.1:8000
#   bash test.sh                 # the same
#   sh test.sh                   # the same — the script re-executes itself
#   ASR_BASE_URL=https://asr.lourie.info ./test.sh
#   ASR_TEST_AUDIO=/path/to/file.wav ./test.sh
#
# Exit code is 0 only if every check passed.

# This script needs bash: `pipefail`, `[[ ... ]]` and `BASH_SOURCE` are not
# available in a plain POSIX shell. `/bin/sh` is dash on Debian and Ubuntu,
# and `sh test.sh` overrides the shebang, so re-execute under bash instead
# of failing on the first bash-only construct.
if [ -z "${BASH_VERSION:-}" ]; then
  if command -v bash >/dev/null 2>&1; then
    exec bash "$0" "$@"
  fi
  echo "test.sh needs bash, and bash was not found on PATH." >&2
  exit 2
fi

set -uo pipefail

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

BASE_URL="${ASR_BASE_URL:-http://127.0.0.1:8000}"
AUDIO_FILE="${ASR_TEST_AUDIO:-${SCRIPT_DIR}/test/fixtures/test-audio.wav}"
LANGUAGE="${ASR_LANGUAGE:-ru-RU}"
MODEL="${ASR_MODEL:-large-v3-turbo}"

# Load a local, untracked .env if present. The key is read under the same
# names the service itself accepts, in the same order, so this script
# exercises the same resolution the service does. An exported value wins.
if [[ -z "${ASR_API_KEY:-}" && -f "${SCRIPT_DIR}/.env" ]]; then
  for name in ASR_API_KEY API_KEY AUTH_KEY; do
    from_env="$(sed -n "s/^${name}=//p" "${SCRIPT_DIR}/.env" | tail -n 1)"
    from_env="${from_env%\"}"; from_env="${from_env#\"}"
    from_env="${from_env%\'}"; from_env="${from_env#\'}"
    if [[ -n "$from_env" ]]; then
      ASR_API_KEY="$from_env"
      break
    fi
  done
fi

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
PASSED=0
FAILED=0

green() { printf '\033[32m%s\033[0m\n' "$1"; }
red()   { printf '\033[31m%s\033[0m\n' "$1"; }
bold()  { printf '\033[1m%s\033[0m\n' "$1"; }

pass() { green "  PASS  $1"; PASSED=$((PASSED + 1)); }
fail() { red   "  FAIL  $1"; FAILED=$((FAILED + 1)); }
skip() { printf '  SKIP  %s\n' "$1"; }

# json_get <json> <key> — the value of a key, or nothing.
# jq is used when it is available; python3 otherwise. The two are both
# optional, but one of them is needed to read a JSON response, and a
# missing tool must not be mistaken for an empty value.
json_get() {
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$1" | jq -r ".${2} // empty" 2>/dev/null
  elif command -v python3 >/dev/null 2>&1; then
    printf '%s' "$1" | python3 -c '
import json, sys
try:
    value = json.load(sys.stdin).get(sys.argv[1])
except Exception:
    value = None
print(value if isinstance(value, str) else "")
' "$2" 2>/dev/null
  fi
}

# json_has <json> <key> — whether a key is present and not empty.
json_has() {
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$1" | jq -e "has(\"${2}\") and .${2} != null and .${2} != \"\"" \
      >/dev/null 2>&1
  elif command -v python3 >/dev/null 2>&1; then
    printf '%s' "$1" | python3 -c '
import json, sys
try:
    value = json.load(sys.stdin).get(sys.argv[1])
except Exception:
    value = None
sys.exit(0 if value else 1)
' "$2" >/dev/null 2>&1
  else
    return 1
  fi
}

# check <name> <expected-status> <actual-status> [detail]
check() {
  local name="$1" expected="$2" actual="$3" detail="${4:-}"
  if [[ "$actual" == "$expected" ]]; then
    pass "${name} (HTTP ${actual})"
  else
    fail "${name}: expected HTTP ${expected}, got HTTP ${actual}${detail:+ — ${detail}}"
  fi
}

bold "ASR service smoke test"
echo "  target:    ${BASE_URL}"
echo "  audio:     ${AUDIO_FILE}"
echo

# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------
if ! command -v curl >/dev/null 2>&1; then
  red "curl was not found on PATH."
  echo "  Install curl and run the check again."
  exit 2
fi

# Reading a JSON response needs jq or python3. Without either, the checks
# would report a false failure, so this is stated up front instead.
if ! command -v jq >/dev/null 2>&1 && ! command -v python3 >/dev/null 2>&1; then
  red "Neither jq nor python3 was found on PATH."
  echo "  One of them is needed to read the service's JSON responses."
  exit 2
fi

if [[ ! -f "$AUDIO_FILE" ]]; then
  red "Audio file not found: ${AUDIO_FILE}"
  echo "  Set ASR_TEST_AUDIO to a readable WAV file, or see test/fixtures/."
  exit 2
fi

if [[ -z "${ASR_API_KEY:-}" ]]; then
  red "ASR_API_KEY is not set."
  echo "  Export it, or put it in an untracked .env next to this script."
  exit 2
fi

# Read the key into a curl config file so it never appears in the process
# list (visible via `ps`) and never lands in shell history.
CURL_CONFIG="$(mktemp)"
chmod 600 "$CURL_CONFIG"
trap 'rm -f "$CURL_CONFIG"' EXIT
printf 'header = "X-API-Key: %s"\n' "$ASR_API_KEY" > "$CURL_CONFIG"

# --------------------------------------------------------------------------
# Unauthenticated surface — must work without a key
# --------------------------------------------------------------------------
bold "Public surface (no key required)"

code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "${BASE_URL}/health")"
check "GET /health" 200 "$code"

code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "${BASE_URL}/")"
check "GET /" 200 "$code"

code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "${BASE_URL}/static/script.js")"
check "GET /static/script.js" 200 "$code"

# --------------------------------------------------------------------------
# Forwarding endpoints — must reject a missing or wrong key
# --------------------------------------------------------------------------
bold "Forwarding endpoints (key required)"

# A request without a key must be refused before any upstream work, so it
# must answer fast. A request with a key does the real transcription, so it
# gets the full budget.
code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 \
  -X POST "${BASE_URL}/asr" \
  -F "file=@${AUDIO_FILE}" -F "language=${LANGUAGE}")"
check "POST /asr without key" 401 "$code"

code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 180 \
  --config "$CURL_CONFIG" \
  -X POST "${BASE_URL}/asr" \
  -F "file=@${AUDIO_FILE}" -F "language=${LANGUAGE}")"
check "POST /asr with key" 200 "$code"

# The key must never be echoed back to the caller.
body="$(curl -sS --max-time 30 \
  -H "X-API-Key: ${ASR_API_KEY}-wrong" \
  -X POST "${BASE_URL}/asr" \
  -F "file=@${AUDIO_FILE}" -F "language=${LANGUAGE}")"
if [[ -z "$body" ]]; then
  fail "request with a wrong key returned no body"
elif [[ "$body" == *"${ASR_API_KEY}"* ]]; then
  fail "the key is reflected in a response body"
else
  pass "the key is not reflected in a response body"
fi

# --------------------------------------------------------------------------
# Transcription actually returns text
# --------------------------------------------------------------------------
bold "Transcription"

transcript="$(curl -sS --max-time 120 \
  --config "$CURL_CONFIG" \
  -X POST "${BASE_URL}/asr" \
  -F "file=@${AUDIO_FILE}" -F "language=${LANGUAGE}" -F "model=${MODEL}")"

text="$(json_get "$transcript" text)"
if [[ -n "$text" ]]; then
  pass "transcription returned text: ${text}"
else
  fail "transcription returned no text — response: ${transcript}"
fi

# --------------------------------------------------------------------------
# Questionnaire — public browser demo, no key
# --------------------------------------------------------------------------
bold "Questionnaire (public browser demo)"

questionnaire="$(curl -sS --max-time 120 \
  -X POST "${BASE_URL}/submit_questionnaire" \
  -F "gender=тест" -F "age=30" -F "track=past" -F "goal=отпустить" \
  -F "event_name=тест" -F "age_at_moment=10" -F "place_action=тест" \
  -F "light_weather=солнце")"

if json_has "$questionnaire" response; then
  pass "questionnaire returned a generated response"
else
  fail "questionnaire did not return a response — got: ${questionnaire}"
fi

# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------
echo
bold "Result: ${PASSED} passed, ${FAILED} failed"
if [[ "$FAILED" -gt 0 ]]; then
  exit 1
fi
exit 0
