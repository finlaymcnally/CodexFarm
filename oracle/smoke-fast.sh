#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_ORACLE_HOME_DIR="$REPO_ROOT/var/oracle"

ORACLE_HOME_DIR="${ORACLE_HOME_DIR:-$DEFAULT_ORACLE_HOME_DIR}"
ORACLE_BROWSER_PROFILE_DIR="${ORACLE_BROWSER_PROFILE_DIR:-$ORACLE_HOME_DIR/browser-profile}"
ORACLE_BROWSER_REMOTE_DEBUG_HOST="${ORACLE_BROWSER_REMOTE_DEBUG_HOST:-127.0.0.1}"
ORACLE_BROWSER_MANUAL_LOGIN="${ORACLE_BROWSER_MANUAL_LOGIN:-1}"

export ORACLE_HOME_DIR
export ORACLE_BROWSER_PROFILE_DIR
export ORACLE_BROWSER_REMOTE_DEBUG_HOST
export ORACLE_BROWSER_MANUAL_LOGIN

cd "$REPO_ROOT"
./oracle/cleanup-stale.sh
./oracle/preflight.sh

set +e
timeout --signal=TERM 120s /home/mcnal/.local/bin/oracle-browser-headless \
  --model gpt-5-instant \
  --slug oracle-fast-smoke \
  --force \
  --chatgpt-url "https://chatgpt.com/?temporary-chat=true" \
  --browser-attachments never \
  --browser-inline-files \
  -p "Reply with exactly OK."
status=$?
set -e
./oracle/cleanup-stale.sh >/dev/null || true
if [[ "$status" -eq 124 ]]; then
  echo "FAIL: smoke-fast timed out (>120s). Treating as broken run."
fi
if [[ "$status" -ne 0 ]]; then
  echo "HINT: if login/session errors appear, run ./oracle/wsl-login.sh and retry."
fi
exit "$status"
