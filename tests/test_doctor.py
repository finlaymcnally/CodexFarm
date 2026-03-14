import subprocess

import pytest

from codex_farm.doctor import (
    check_codex_login_status,
    check_codex_non_interactive_status,
    run_codex_execution_checks,
    run_doctor_checks,
)


def _completed(
    cmd: list[str],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


def test_check_codex_login_status_passes_when_logged_in(monkeypatch) -> None:
    monkeypatch.setattr("codex_farm.doctor.shutil.which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(
        "codex_farm.doctor._run_command",
        lambda cmd, timeout_seconds=20: _completed(
            cmd,
            returncode=0,
            stdout="Logged in using ChatGPT\n",
        ),
    )

    check = check_codex_login_status()
    assert check.ok is True
    assert check.name == "codex login status"
    assert "Logged in using ChatGPT" in check.detail


def test_run_doctor_checks_skips_smoke_when_login_status_fails(monkeypatch) -> None:
    monkeypatch.setattr("codex_farm.doctor.shutil.which", lambda name: "/usr/bin/codex")
    calls: list[list[str]] = []

    def fake_run_command(cmd: list[str], timeout_seconds: int = 20):
        calls.append(list(cmd))
        if cmd[:2] == ["codex", "--version"]:
            return _completed(cmd, returncode=0, stdout="codex 0.0.0-test\n")
        if cmd[:3] == ["codex", "login", "status"]:
            return _completed(
                cmd,
                returncode=1,
                stderr="Not logged in",
            )
        pytest.fail(f"Unexpected command during login-precheck failure path: {cmd}")

    monkeypatch.setattr("codex_farm.doctor._run_command", fake_run_command)

    checks, all_ok = run_doctor_checks()
    assert all_ok is False
    assert any(check.name == "codex login status" and not check.ok for check in checks)
    smoke = next(check for check in checks if check.name == "codex non-interactive check")
    assert smoke.ok is False
    assert smoke.detail == "Skipped because login status check failed"
    assert not any("exec" in cmd for cmd in calls)


def test_check_codex_non_interactive_status_reports_websocket_auth_failure(monkeypatch) -> None:
    monkeypatch.setattr("codex_farm.doctor.shutil.which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(
        "codex_farm.doctor._run_command",
        lambda cmd, timeout_seconds=60: _completed(
            cmd,
            returncode=1,
            stderr=(
                "WebSocket error: HTTP 403 Forbidden "
                "wss://chatgpt.com/backend-api/codex/responses"
            ),
        ),
    )

    check = check_codex_non_interactive_status()
    assert check.ok is False
    assert "403 Forbidden" in check.detail
    assert "Run `codex` once and sign in with ChatGPT." in check.detail


def test_check_codex_non_interactive_status_uses_requested_model_and_effort(monkeypatch) -> None:
    monkeypatch.setattr("codex_farm.doctor.shutil.which", lambda name: "/usr/bin/codex")
    captured: list[list[str]] = []

    def fake_run_command(cmd: list[str], timeout_seconds: int = 60):
        captured.append(list(cmd))
        return _completed(cmd, returncode=0, stdout="OK\n")

    monkeypatch.setattr("codex_farm.doctor._run_command", fake_run_command)

    check = check_codex_non_interactive_status(
        model="gpt-5.1-codex-mini",
        reasoning_effort="medium",
    )

    assert check.ok is True
    assert captured == [
        [
            "codex",
            "--ask-for-approval",
            "never",
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--model",
            "gpt-5.1-codex-mini",
            "--config",
            'model_reasoning_effort="medium"',
            "Reply with exactly: OK",
        ]
    ]


def test_check_codex_non_interactive_status_passes_env_overrides(monkeypatch) -> None:
    monkeypatch.setattr("codex_farm.doctor.shutil.which", lambda name: "/usr/bin/codex")
    captured_envs: list[dict[str, str] | None] = []

    def fake_run_command(
        cmd: list[str],
        timeout_seconds: int = 60,
        *,
        env_overrides=None,
    ):
        captured_envs.append(env_overrides)
        return _completed(cmd, returncode=0, stdout="OK\n")

    monkeypatch.setattr("codex_farm.doctor._run_command", fake_run_command)

    check = check_codex_non_interactive_status(
        env_overrides={"CODEX_HOME": "/tmp/codex-home"},
    )

    assert check.ok is True
    assert captured_envs == [{"CODEX_HOME": "/tmp/codex-home"}]


def test_run_codex_execution_checks_require_smoke_even_when_login_status_passes(monkeypatch) -> None:
    monkeypatch.setattr("codex_farm.doctor.shutil.which", lambda name: "/usr/bin/codex")

    def fake_run_command(cmd: list[str], timeout_seconds: int = 20):
        if cmd[:3] == ["codex", "login", "status"]:
            return _completed(cmd, returncode=0, stdout="Logged in using ChatGPT\n")
        if "exec" in cmd:
            return _completed(
                cmd,
                returncode=1,
                stderr=(
                    "WARNING: no last agent message. "
                    "WebSocket error: HTTP 403 Forbidden "
                    "wss://chatgpt.com/backend-api/codex/responses"
                ),
            )
        pytest.fail(f"Unexpected command: {cmd}")

    monkeypatch.setattr("codex_farm.doctor._run_command", fake_run_command)

    checks, all_ok = run_codex_execution_checks()
    assert all_ok is False
    assert len(checks) == 2
    assert checks[0].name == "codex login status"
    assert checks[0].ok is True
    assert checks[1].name == "codex non-interactive check"
    assert checks[1].ok is False
    assert "403 Forbidden" in checks[1].detail
