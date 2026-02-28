#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_ORACLE_HOME_DIR="$REPO_ROOT/var/oracle"

ORACLE_HOME_DIR="${ORACLE_HOME_DIR:-$DEFAULT_ORACLE_HOME_DIR}"
ORACLE_BROWSER_PROFILE_DIR="${ORACLE_BROWSER_PROFILE_DIR:-$ORACLE_HOME_DIR/browser-profile}"

export ORACLE_HOME_DIR
export ORACLE_BROWSER_PROFILE_DIR

mkdir -p "$ORACLE_HOME_DIR" "$ORACLE_BROWSER_PROFILE_DIR"

echo "Launching login browser for Oracle profile:"
echo "  ORACLE_HOME_DIR=$ORACLE_HOME_DIR"
echo "  ORACLE_BROWSER_PROFILE_DIR=$ORACLE_BROWSER_PROFILE_DIR"
echo
echo "After finishing ChatGPT login, run:"
echo "  cd $REPO_ROOT && ./oracle/preflight.sh"

exec /home/mcnal/.local/bin/chromium-chatgpt-login
