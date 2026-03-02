#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_ORACLE_HOME_DIR="$REPO_ROOT/var/oracle"

ORACLE_HOME_DIR="${ORACLE_HOME_DIR:-$DEFAULT_ORACLE_HOME_DIR}"
ORACLE_BROWSER_PROFILE_DIR="${ORACLE_BROWSER_PROFILE_DIR:-$ORACLE_HOME_DIR/browser-profile}"
ORACLE_BROWSER_REMOTE_DEBUG_HOST="${ORACLE_BROWSER_REMOTE_DEBUG_HOST:-127.0.0.1}"
ORACLE_BROWSER_MANUAL_LOGIN="${ORACLE_BROWSER_MANUAL_LOGIN:-1}"
ORACLE_NOTIFY="${ORACLE_NOTIFY:-0}"

export ORACLE_HOME_DIR
export ORACLE_BROWSER_PROFILE_DIR
export ORACLE_BROWSER_REMOTE_DEBUG_HOST
export ORACLE_BROWSER_MANUAL_LOGIN
export ORACLE_NOTIFY

cd "$REPO_ROOT"
./oracle/cleanup-stale.sh
./oracle/preflight.sh

SMOKE_LOG="$(mktemp -t oracle-smoke-fast.XXXXXX.log)"
cleanup_log() {
  rm -f "$SMOKE_LOG" 2>/dev/null || true
}
trap cleanup_log EXIT
log_has_expected_answer() {
  grep -Eq '^Answer:[[:space:]]*$' "$SMOKE_LOG" && grep -Eq '^OK[[:space:]]*$' "$SMOKE_LOG"
}

set +e
/home/mcnal/.local/bin/oracle-browser-headless \
  --model gpt-5-instant \
  --slug oracle-fast-smoke \
  --force \
  --chatgpt-url "https://chatgpt.com/?temporary-chat=true" \
  --browser-attachments never \
  --browser-inline-files \
  -p "Reply with exactly OK." >"$SMOKE_LOG" 2>&1 &
oracle_pid=$!
tail --pid="$oracle_pid" -n +1 -f "$SMOKE_LOG" &
tail_pid=$!

deadline=$((SECONDS + 120))
success_seen=0
success_seen_at=0
success_grace_secs=5
timed_out=0

while kill -0 "$oracle_pid" 2>/dev/null; do
  if log_has_expected_answer; then
    if [[ "$success_seen" -eq 0 ]]; then
      success_seen=1
      success_seen_at=$SECONDS
    elif (( SECONDS - success_seen_at >= success_grace_secs )); then
      kill -TERM "$oracle_pid" 2>/dev/null || true
      break
    fi
  fi
  if (( SECONDS >= deadline )); then
    timed_out=1
    kill -TERM "$oracle_pid" 2>/dev/null || true
    break
  fi
  sleep 1
done

wait "$oracle_pid"
oracle_status=$?
wait "$tail_pid" 2>/dev/null || true
set -e
./oracle/cleanup-stale.sh >/dev/null || true

status=$oracle_status
if log_has_expected_answer; then
  if [[ "$success_seen" -eq 1 ]]; then
    echo "PASS: smoke-fast produced expected answer; exiting without waiting for lingering process."
  elif [[ "$timed_out" -eq 1 ]]; then
    echo "PASS: smoke-fast produced expected answer before timeout; treating as success."
  else
    echo "PASS: smoke-fast produced expected answer."
  fi
  status=0
elif [[ "$timed_out" -eq 1 ]]; then
    echo "FAIL: smoke-fast timed out (>120s). Treating as broken run."
fi
if [[ "$status" -ne 0 ]]; then
  echo "HINT: if login/session errors appear, run ./oracle/wsl-login.sh and retry."
fi
exit "$status"
