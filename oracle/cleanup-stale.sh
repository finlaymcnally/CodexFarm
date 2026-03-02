#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEFAULT_ORACLE_HOME_DIR="$REPO_ROOT/var/oracle"
ORACLE_HOME_DIR="${ORACLE_HOME_DIR:-$DEFAULT_ORACLE_HOME_DIR}"
ORACLE_BROWSER_PROFILE_DIR="${ORACLE_BROWSER_PROFILE_DIR:-$ORACLE_HOME_DIR/browser-profile}"
ORACLE_BROWSER_PORT="${ORACLE_BROWSER_PORT:-9222}"

killed=0

kill_matches() {
  local awk_program="$1"
  while IFS= read -r pid; do
    if [[ -n "$pid" ]]; then
      kill "$pid" 2>/dev/null || true
      killed=1
    fi
  done < <(ps -eo pid=,cmd= | awk "$awk_program")
}

# Kill orphaned Oracle browser runners.
kill_matches 'index($0, "oracle --engine browser") { print $1 }'

# Kill Chrome processes for the exact profile path.
kill_matches "index(\$0, \"chrome-linux/chrome\") && index(\$0, \"--user-data-dir=$ORACLE_BROWSER_PROFILE_DIR\") { print \$1 }"

# Kill stale shared-debug sessions that hijack new runs, regardless of profile path.
kill_matches "index(\$0, \"chrome-linux/chrome\") && index(\$0, \"--remote-debugging-port=$ORACLE_BROWSER_PORT\") { print \$1 }"
kill_matches "index(\$0, \"xvfb-run\") && index(\$0, \"chrome-linux/chrome\") && index(\$0, \"--remote-debugging-port=$ORACLE_BROWSER_PORT\") { print \$1 }"

rm -f \
  "$ORACLE_BROWSER_PROFILE_DIR/SingletonLock" \
  "$ORACLE_BROWSER_PROFILE_DIR/SingletonSocket" \
  "$ORACLE_BROWSER_PROFILE_DIR/SingletonCookie" \
  "$ORACLE_BROWSER_PROFILE_DIR/DevToolsActivePort" \
  "$ORACLE_BROWSER_PROFILE_DIR/Default/DevToolsActivePort" \
  2>/dev/null || true

if [[ "$killed" -eq 1 ]]; then
  echo "Cleaned stale Oracle/Chromium processes for $ORACLE_BROWSER_PROFILE_DIR"
else
  echo "No stale Oracle/Chromium processes found for $ORACLE_BROWSER_PROFILE_DIR"
fi
