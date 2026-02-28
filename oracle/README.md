# Oracle Runbook (WSL / CodexFarm)

This is the single source of truth for Oracle browser runs in this repo.

For incident detail and exact logs from 2026-02-28, see:
- [RECOVERY-2026-02-28.md](RECOVERY-2026-02-28.md)

As of 2026-02-28, `/home/mcnal/.local/bin/oracle-browser-headless` runs Chrome in an off-screen Xvfb display.
Expected behavior:
- `./oracle/wsl-login.sh` opens a visible Chromium window for manual sign-in.
- `./oracle/smoke-fast.sh` and normal Oracle runs do not open/focus a desktop window.

## Non-Negotiable Profile Rule

- This repo defaults Oracle runtime state to:
  - `ORACLE_HOME_DIR=<repo>/var/oracle`
  - `ORACLE_BROWSER_PROFILE_DIR=$ORACLE_HOME_DIR/browser-profile`
- `var/` is git-ignored in this repo, so Oracle state here is not tracked by git.
- If you point `ORACLE_HOME_DIR` at another path (`/tmp/oracle-home-*` or `$HOME/.local/share/oracle`), you are often using a different profile.
- If ChatGPT opens logged out and Oracle stalls, this mismatch is the first thing to check.

## Canonical WSL Workflow

1. Preflight first (never skip):
   ```bash
   cd ~/projects/shared/CodexFarm
   ./oracle/preflight.sh
   ```

2. If this is your first repo-local setup, seed profile from global once:
   ```bash
   cd ~/projects/shared/CodexFarm
   ./oracle/seed-profile-from-global.sh
   ```

3. If preflight shows no session cookie, log in to the same profile Oracle will use:
   ```bash
   cd ~/projects/shared/CodexFarm
   ./oracle/wsl-login.sh
   ```

4. Fast smoke (model fixed to `gpt-5-instant`):
   ```bash
   cd ~/projects/shared/CodexFarm
   ./oracle/smoke-fast.sh
   ```

5. Upload smoke (forces attachments, confirms file upload path):
   ```bash
   cd ~/projects/shared/CodexFarm
   ./oracle/smoke-upload.sh
   ```

6. Real run template:
   ```bash
   cd ~/projects/shared/CodexFarm
   ORACLE_BROWSER_REMOTE_DEBUG_HOST=127.0.0.1 \
   /home/mcnal/.local/bin/oracle-browser-headless \
     --model gpt-5-instant \
     --slug codexfarm-real-run-file \
     --force \
     --browser-attachments always \
     --browser-bundle-files \
     -p "You are reviewing CodexFarm, a solo-maintainer Python CLI/orchestration project. First explain how it works end-to-end from the attached files. Then propose a prioritized list of practical improvements for a solo developer. For each: why it matters, expected impact, implementation effort (S/M/L), and one concrete first step. Focus on reliability, usability, debuggability, performance, and documentation quality." \
     --file docs/AI_Context.md \
     --file docs/2026-02-28_10.36.12_CodexFarm-docs-summary.md
   ```

## Windows PowerShell Fallback (Verified 2026-02-28)

Use this only when Linux Chromium does not show up in WSLg.

1. Open dedicated Oracle profile in Windows Chrome and sign in:
   ```powershell
   $profile = Join-Path $env:TEMP 'oracle-home-mcnal\browser-profile'
   New-Item -ItemType Directory -Force -Path $profile | Out-Null
   $chrome = Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'
   if (!(Test-Path $chrome)) { $chrome = Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe' }
   Start-Process -FilePath $chrome -ArgumentList @("--user-data-dir=$profile","https://chatgpt.com/")
   ```

2. Complete login fully, then close that dedicated window.

3. Run Oracle from PowerShell with absolute UNC file paths:
   ```powershell
   $file_ai = "\\wsl$\Ubuntu\home\mcnal\projects\shared\CodexFarm\docs\AI_Context.md"
   $file_summary = "\\wsl$\Ubuntu\home\mcnal\projects\shared\CodexFarm\docs\2026-02-28_10.36.12_CodexFarm-docs-summary.md"
   $env:ORACLE_HOME_DIR = Join-Path $env:TEMP 'oracle-home-mcnal'
   $env:ORACLE_BROWSER_PROFILE_DIR = Join-Path $env:ORACLE_HOME_DIR 'browser-profile'
   Set-Location '\\wsl$\Ubuntu\home\mcnal\projects\shared\CodexFarm'
   npx -y @steipete/oracle --engine browser --browser-manual-login --browser-input-timeout 300s --browser-timeout 60m --browser-attachments always --browser-bundle-files --model gpt-5-instant --slug win-oracle-upload-file --force --verbose --prompt "Reply with exactly OK." --file "$file_ai"
   ```

## Rules (Do Not Skip)

- Use `gpt-5-instant` for test loops.
- Prefer `/home/mcnal/.local/bin/oracle-browser-headless` from WSL.
- Keep `ORACLE_BROWSER_REMOTE_DEBUG_HOST=127.0.0.1` in WSL runs.
- If ChatGPT shows "duplicate document", clear it and rerun with `--force`.
- `about:blank` after run is usually a harmless control tab.
- The smoke scripts use `https://chatgpt.com/?temporary-chat=true` to reduce stale-draft/duplicate-popup issues.
- The wrapper defaults `ORACLE_BROWSER_STRICT_ATTACHMENT_ACK=0` to avoid false attachment-ack failures.
- The wrapper defaults `ORACLE_BROWSER_MANUAL_LOGIN=1` (manual-login profile reuse).
- Use `./oracle/cleanup-stale.sh` before manual runs if a previous run got stuck.
- Use `./oracle/seed-profile-from-global.sh` to initialize repo-local profile from global profile.
- Smoke scripts hard-timeout at 120s; slower than that is treated as a broken run.

## Known Failures and Exact Fixes

### Browser opens logged-out ChatGPT and nothing proceeds

Cause:
- Oracle is using a different profile path than the one you authenticated.
- Most common: stale env variables pointing to `/tmp/oracle-home-*`.

Fix:
- Run `./oracle/preflight.sh`.
- If hints show session cookies in another profile, point Oracle there or unset stale env:
  ```bash
  unset ORACLE_HOME_DIR ORACLE_BROWSER_PROFILE_DIR
  ```
- Then rerun login with `./oracle/wsl-login.sh`.

### Manual-login timeout even when preflight shows session cookie

Cause:
- Wrapper forced `--browser-manual-login`, and Oracle waited for interactive sign-in.

Fix:
- Run `./oracle/cleanup-stale.sh` and retry smoke (stale Chrome/oracle processes can trigger false waits).
- Keep preflight + repo-local profile in sync.
- Optional advanced mode (not recommended here unless you have keytar/libsecret deps):
  ```bash
  ORACLE_BROWSER_MANUAL_LOGIN=0
  ```

### Oracle run steals mouse/keyboard focus

Cause:
- Not using `/home/mcnal/.local/bin/oracle-browser-headless`, or wrapper changed unexpectedly.

Fix:
- Run through repo scripts (`./oracle/smoke-fast.sh`, `./oracle/smoke-upload.sh`) or call the wrapper directly.
- Verify wrapper path:
  ```bash
  command -v oracle-browser-headless
  ```

### `EACCES: permission denied, mkdtemp '/mnt/c/Windows/Temp/oracle-browser-*'`

Cause: Oracle chose a Windows temp path blocked from this environment.

Fix:
- Use `/home/mcnal/.local/bin/oracle-browser-headless` (wrapper handles safer defaults).
- Do not run ad-hoc `npx ... --engine browser` from WSL unless you need raw behavior.

### `connect ECONNREFUSED 10.255.255.254:<port>`

Cause: wrong DevTools host chosen in WSL.

Fix:
- Set `ORACLE_BROWSER_REMOTE_DEBUG_HOST=127.0.0.1`.

### `No ChatGPT cookies were applied from your Chrome profile`

Cause:
- ChatGPT not logged in for the active Oracle profile, or
- keyring/cookie extraction unavailable.

Fix:
- Prefer manual-login profile flow.
- Run `./oracle/preflight.sh` to verify active profile cookies.

### `CMD.EXE ... UNC paths are not supported` and `Missing file or directory: docs/...`

Cause: Windows run from UNC path falls back cwd to `C:\Windows`.

Fix:
- Use absolute UNC paths for every `--file`.

### `connect ECONNREFUSED 127.0.0.1:<port>` after manual-login

Cause: profile already open in a normal Chrome session.

Fix:
- Close dedicated manual-login Chrome window first.
- Remove stale `SingletonLock`, `SingletonSocket`, `SingletonCookie`, `DevToolsActivePort`.

## Session Recovery

List recent sessions:

```bash
ORACLE_HOME_DIR="$PWD/var/oracle" npx -y @steipete/oracle status --hours 48
```

Reattach a slug:

```bash
ORACLE_HOME_DIR="$PWD/var/oracle" npx -y @steipete/oracle session <slug> --render
```
