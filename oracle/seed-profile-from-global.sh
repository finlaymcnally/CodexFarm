#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PROJECT_ORACLE_HOME_DIR="${ORACLE_HOME_DIR:-$REPO_ROOT/var/oracle}"
PROJECT_PROFILE_DIR="${ORACLE_BROWSER_PROFILE_DIR:-$PROJECT_ORACLE_HOME_DIR/browser-profile}"
GLOBAL_PROFILE_DIR="${GLOBAL_ORACLE_PROFILE_DIR:-${HOME:-/home/mcnal}/.local/share/oracle/browser-profile}"

echo "Seeding project Oracle profile"
echo "  from: $GLOBAL_PROFILE_DIR"
echo "  to:   $PROJECT_PROFILE_DIR"

if [[ ! -d "$GLOBAL_PROFILE_DIR" ]]; then
  echo "FAIL: source profile missing: $GLOBAL_PROFILE_DIR"
  exit 1
fi

cd "$REPO_ROOT"
ORACLE_HOME_DIR="$PROJECT_ORACLE_HOME_DIR" ORACLE_BROWSER_PROFILE_DIR="$PROJECT_PROFILE_DIR" ./oracle/cleanup-stale.sh >/dev/null || true
ORACLE_HOME_DIR="${HOME:-/home/mcnal}/.local/share/oracle" ORACLE_BROWSER_PROFILE_DIR="$GLOBAL_PROFILE_DIR" ./oracle/cleanup-stale.sh >/dev/null || true

mkdir -p "$PROJECT_ORACLE_HOME_DIR"
rsync -a --delete "$GLOBAL_PROFILE_DIR/" "$PROJECT_PROFILE_DIR/"

echo "Done. Run ./oracle/preflight.sh to verify session cookies."
