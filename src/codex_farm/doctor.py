"""Prerequisite checks for local codex-farm usage."""

from __future__ import annotations

from dataclasses import dataclass
import shutil
import subprocess
import sys


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
        smoke_detail = (
            (smoke_stderr.strip() or smoke_stdout.strip() or "codex exec failed")
            + "; run `codex` once and sign in with ChatGPT"
        )

    checks.append(
        CheckResult(
            name="codex non-interactive check",
            ok=smoke_ok,
            detail=smoke_detail,
        )
    )

    return checks, all(check.ok for check in checks)
