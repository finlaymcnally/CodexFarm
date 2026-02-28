#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEFAULT_ORACLE_HOME_DIR="$REPO_ROOT/var/oracle"
GLOBAL_ORACLE_HOME_DIR="${HOME:-/home/mcnal}/.local/share/oracle"
LEGACY_ORACLE_HOME_DIR="${HOME:-/home/mcnal}/.oracle"
TMP_ORACLE_HOME_DIR="/tmp/oracle-home-${USER:-mcnal}"

ORACLE_HOME_DIR="${ORACLE_HOME_DIR:-$DEFAULT_ORACLE_HOME_DIR}"
ORACLE_BROWSER_PROFILE_DIR="${ORACLE_BROWSER_PROFILE_DIR:-$ORACLE_HOME_DIR/browser-profile}"
COOKIE_DB="$ORACLE_BROWSER_PROFILE_DIR/Default/Cookies"
DEFAULT_COOKIE_DB="$DEFAULT_ORACLE_HOME_DIR/browser-profile/Default/Cookies"
GLOBAL_COOKIE_DB="$GLOBAL_ORACLE_HOME_DIR/browser-profile/Default/Cookies"
LEGACY_COOKIE_DB="$LEGACY_ORACLE_HOME_DIR/browser-profile/Default/Cookies"
TMP_COOKIE_DB="$TMP_ORACLE_HOME_DIR/browser-profile/Default/Cookies"

echo "Oracle preflight"
echo "  ORACLE_HOME_DIR=$ORACLE_HOME_DIR"
echo "  ORACLE_BROWSER_PROFILE_DIR=$ORACLE_BROWSER_PROFILE_DIR"
echo "  DEFAULT_ORACLE_HOME_DIR=$DEFAULT_ORACLE_HOME_DIR"
echo "  GLOBAL_ORACLE_HOME_DIR=$GLOBAL_ORACLE_HOME_DIR"

if [[ ! -f "$COOKIE_DB" ]]; then
  echo "FAIL: Missing cookie DB at $COOKIE_DB"
  echo "Run login for this same profile:"
  echo "  ORACLE_HOME_DIR=$ORACLE_HOME_DIR ORACLE_BROWSER_PROFILE_DIR=$ORACLE_BROWSER_PROFILE_DIR ./oracle/wsl-login.sh"
  exit 1
fi

python3 - "$COOKIE_DB" "$DEFAULT_COOKIE_DB" "$GLOBAL_COOKIE_DB" "$LEGACY_COOKIE_DB" "$TMP_COOKIE_DB" <<'PY'
import sqlite3
import sys
from pathlib import Path

active_db = Path(sys.argv[1])
default_db = Path(sys.argv[2])
global_db = Path(sys.argv[3])
legacy_db = Path(sys.argv[4])
tmp_db = Path(sys.argv[5])

def counts(path: Path) -> tuple[int, int]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    cur = conn.cursor()
    cur.execute(
        """
        select count(*)
        from cookies
        where host_key like '%chatgpt.com%' or host_key like '%openai.com%'
        """
    )
    total = cur.fetchone()[0]

    cur.execute(
        """
        select count(*)
        from cookies
        where (host_key like '%chatgpt.com%' or host_key like '%openai.com%')
          and (name like '%next-auth.session-token%' or name='__Secure-next-auth.session-token')
        """
    )
    session = cur.fetchone()[0]
    conn.close()
    return total, session

active_total, active_session = counts(active_db)
print(f"ChatGPT/OpenAI cookies: {active_total}")
print(f"Session cookies:        {active_session}")

if active_total <= 0:
    print("FAIL: no ChatGPT/OpenAI cookies found in Oracle profile")
    sys.exit(1)
if active_session <= 0:
    print("WARN: no next-auth session cookie found; login may be required")
    hint_found = False
    for label, path in [
        ("project default profile", default_db),
        ("global profile", global_db),
        ("legacy profile", legacy_db),
        ("tmp profile", tmp_db),
    ]:
        if path == active_db or not path.exists():
            continue
        try:
            total, session = counts(path)
        except sqlite3.Error:
            continue
        if session > 0:
            hint_found = True
            print(
                "HINT: session cookie found in "
                f"{label} ({path}) but active profile has none."
            )
    if hint_found:
        print("HINT: point ORACLE_HOME_DIR to the profile with session cookies.")
else:
    print("PASS: profile has session cookie")
PY

if command -v ldd >/dev/null 2>&1; then
  KEYTAR_NODE="/home/mcnal/.nvm/versions/node/v20.19.6/lib/node_modules/@steipete/oracle/node_modules/keytar/build/Release/keytar.node"
  if [[ -f "$KEYTAR_NODE" ]]; then
    if ldd "$KEYTAR_NODE" 2>/dev/null | grep -q "libsecret-1.so.0 => not found"; then
      echo "WARN: keytar missing libsecret-1.so.0 (cookie sync from Chrome profile may fail)"
    fi
  fi
fi

echo "Done."
