"""Wrapper for running codex exec in non-interactive mode."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import tempfile


@dataclass(frozen=True)
class CodexExecResult:
    ok: bool
    exit_code: int
    stderr_tail: str


class CodexExecTimeoutError(TimeoutError):
    """Raised when codex exec exceeds timeout."""


def _stderr_tail(stderr: str, max_lines: int = 20) -> str:
    lines = stderr.strip().splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join(lines[-max_lines:])


def run_codex_exec(
    *,
    workdir: Path,
    prompt: str,
    model: str,
    sandbox: str,
    ask_for_approval: str,
    web_search: str,
    output_schema: Path,
    output_path: Path,
    timeout_seconds: int,
) -> CodexExecResult:
    """Run codex exec and atomically move output into place on success."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        suffix=".tmp",
    ) as tmp:
        temp_output_path = Path(tmp.name)

    cmd = [
        "codex",
        "--ask-for-approval",
        ask_for_approval,
        "exec",
        "--cd",
        str(workdir.resolve()),
        "--skip-git-repo-check",
        "--model",
        model,
        "--sandbox",
        sandbox,
        "--config",
        f"web_search={web_search}",
        "--output-schema",
        str(output_schema.resolve()),
        "--output-last-message",
        str(temp_output_path),
        prompt,
    ]

    try:
        proc = subprocess.run(
            cmd,
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        if temp_output_path.exists():
            temp_output_path.unlink(missing_ok=True)
        raise CodexExecTimeoutError(
            f"codex exec timed out after {timeout_seconds}s"
        ) from exc

    stderr_tail = _stderr_tail(proc.stderr)
    temp_has_payload = temp_output_path.exists() and temp_output_path.stat().st_size > 0

    if proc.returncode != 0 and not temp_has_payload:
        temp_output_path.unlink(missing_ok=True)
        return CodexExecResult(ok=False, exit_code=proc.returncode, stderr_tail=stderr_tail)

    if not temp_has_payload:
        return CodexExecResult(
            ok=False,
            exit_code=proc.returncode,
            stderr_tail="codex exec exited 0 but produced no output file",
        )

    os.replace(temp_output_path, output_path)
    return CodexExecResult(ok=True, exit_code=proc.returncode, stderr_tail=stderr_tail)
