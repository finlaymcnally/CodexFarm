"""Prerequisite checks for local codex-farm usage."""

from __future__ import annotations

from dataclasses import dataclass
import shutil
import subprocess
import sys

from .codex_exec import is_auth_failure_message


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def _run_command(cmd: list[str], timeout_seconds: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=False,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
    )


def _contains_exact_ok_line(text: str) -> bool:
    return any(line.strip() == "OK" for line in text.splitlines())


def check_codex_login_status(timeout_seconds: int = 20) -> CheckResult:
    codex_path = shutil.which("codex")
    if codex_path is None:
        return CheckResult(
            name="codex login status",
            ok=False,
            detail="Skipped because codex is not installed",
        )

    login_proc = _run_command(["codex", "login", "status"], timeout_seconds=timeout_seconds)
    raw_detail = login_proc.stdout.strip() or login_proc.stderr.strip() or "codex login status failed"
    normalized = raw_detail.lower()
    logged_in = "logged in" in normalized and "not logged in" not in normalized
    ok = login_proc.returncode == 0 and logged_in
    if ok:
        detail = raw_detail
    else:
        detail = f"{raw_detail}; run `codex login` (or `codex`) and sign in with ChatGPT"

    return CheckResult(
        name="codex login status",
        ok=ok,
        detail=detail,
    )


def run_doctor_checks() -> tuple[list[CheckResult], bool]:
    checks: list[CheckResult] = []

    py_ok = sys.version_info >= (3, 11)
    checks.append(
        CheckResult(
            name="Python 3.11+",
            ok=py_ok,
            detail=sys.version.split()[0] if py_ok else f"Found {sys.version.split()[0]}",
        )
    )

    codex_path = shutil.which("codex")
    if codex_path is None:
        checks.append(
            CheckResult(
                name="codex on PATH",
                ok=False,
                detail="codex executable not found on PATH",
            )
        )
        checks.append(
            CheckResult(
                name="codex non-interactive check",
                ok=False,
                detail="Skipped because codex is not installed",
            )
        )
        return checks, False

    version_proc = _run_command(["codex", "--version"], timeout_seconds=20)
    checks.append(
        CheckResult(
            name="codex on PATH",
            ok=version_proc.returncode == 0,
            detail=(
                (version_proc.stdout.strip() or "codex --version returned 0")
                if version_proc.returncode == 0
                else (version_proc.stderr.strip() or "codex --version failed")
            ),
        )
    )
    login_check = check_codex_login_status(timeout_seconds=20)
    checks.append(login_check)
    if not login_check.ok:
        checks.append(
            CheckResult(
                name="codex non-interactive check",
                ok=False,
                detail="Skipped because login status check failed",
            )
        )
        return checks, False

    smoke_cmd = [
        "codex",
        "--ask-for-approval",
        "never",
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--model",
        "gpt-5.3-codex-spark",
        "Reply with exactly: OK",
    ]
    smoke_proc = _run_command(smoke_cmd, timeout_seconds=60)
    smoke_stdout = smoke_proc.stdout or ""
    smoke_stderr = smoke_proc.stderr or ""
    smoke_ok = smoke_proc.returncode == 0 or _contains_exact_ok_line(smoke_stdout)

    if smoke_ok:
        smoke_detail = (
            "OK"
            if smoke_proc.returncode == 0
            else "Received expected OK response (ignoring non-zero exit caused by local Codex warnings)"
        )
    else:
        failure_text = smoke_stderr.strip() or smoke_stdout.strip() or "codex exec failed"
        if is_auth_failure_message(failure_text):
            smoke_detail = (
                f"{failure_text}; authentication appears invalid for this machine. "
                "Run `codex` once and sign in with ChatGPT."
            )
        else:
            smoke_detail = f"{failure_text}; run `codex` once and sign in with ChatGPT"

    checks.append(
        CheckResult(
            name="codex non-interactive check",
            ok=smoke_ok,
            detail=smoke_detail,
        )
    )

    return checks, all(check.ok for check in checks)
