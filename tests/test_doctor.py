import subprocess

import pytest

from codex_farm.doctor import check_codex_login_status, run_doctor_checks


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
