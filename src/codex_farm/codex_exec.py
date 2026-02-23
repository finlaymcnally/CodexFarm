"""Wrapper for running codex exec in non-interactive mode."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from typing import Mapping

try:  # pragma: no cover - available on Linux/macOS, optional on Windows.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


@dataclass(frozen=True)
class CodexExecResult:
    ok: bool
    exit_code: int
    stderr_tail: str


class CodexExecTimeoutError(TimeoutError):
    """Raised when codex exec exceeds timeout."""


class CodexExecRateLimitError(RuntimeError):
    """Raised when codex exec reports API rate limiting (HTTP 429)."""


_RATE_LIMIT_PATTERN = re.compile(
    r"\b429\b|too many requests|rate[ -]?limit(?:ed|ing)?",
    re.IGNORECASE,
)


def is_rate_limit_message(text: str) -> bool:
    """Return True when stderr/stdout text indicates rate limiting."""
    if not text:
        return False
    return _RATE_LIMIT_PATTERN.search(text) is not None


_USAGE_LOG_FIELDS = (
    "logged_at_utc",
    "started_at_utc",
    "finished_at_utc",
    "duration_ms",
    "status",
    "exit_code",
    "accepted_nonzero_exit",
    "timeout_seconds",
    "model",
    "sandbox",
    "ask_for_approval",
    "web_search",
    "cd_dir",
    "output_schema_path",
    "output_path",
    "output_payload_present",
    "output_bytes",
    "tokens_input",
    "tokens_cached_input",
    "tokens_output",
    "tokens_total",
    "usage_json",
    "thread_id",
    "prompt_chars",
    "prompt_sha256",
    "prompt_text",
    "stderr_tail",
    "stdout_tail",
    "source",
    "pipeline_id",
    "run_id",
    "task_id",
    "worker_id",
    "input_path",
)
_USAGE_LOG_LOCK = threading.Lock()


def _tail_lines(text: str, max_lines: int = 20) -> str:
    lines = text.strip().splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join(lines[-max_lines:])


def _coerce_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _utc_ts(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_jsonl_events(stdout: str) -> tuple[list[dict[str, object]], list[str]]:
    events: list[dict[str, object]] = []
    passthrough_lines: list[str] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("{") and line.endswith("}"):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                passthrough_lines.append(raw)
                continue
            if isinstance(parsed, dict):
                events.append(parsed)
                continue
        passthrough_lines.append(raw)
    return events, passthrough_lines


def _extract_usage(
    events: list[dict[str, object]],
) -> tuple[dict[str, object], str]:
    usage: dict[str, object] = {}
    thread_id = ""
    for event in events:
        event_type = event.get("type")
        if event_type == "thread.started":
            raw_thread_id = event.get("thread_id")
            if isinstance(raw_thread_id, str):
                thread_id = raw_thread_id
            continue
        if event_type == "turn.completed":
            raw_usage = event.get("usage")
            if isinstance(raw_usage, dict):
                usage = raw_usage
    return usage, thread_id


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _csv_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _append_usage_row(path: Path, row: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _USAGE_LOG_LOCK:
        with path.open("a+", encoding="utf-8", newline="") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0, os.SEEK_END)
                write_header = handle.tell() == 0
                writer = csv.DictWriter(handle, fieldnames=_USAGE_LOG_FIELDS)
                if write_header:
                    writer.writeheader()
                writer.writerow({field: _csv_cell(row.get(field)) for field in _USAGE_LOG_FIELDS})
                handle.flush()
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _log_codex_activity(
    *,
    usage_log_csv: Path | None,
    usage_context: Mapping[str, object] | None,
    started_at: datetime,
    finished_at: datetime,
    duration_ms: int,
    status: str,
    exit_code: int | None,
    accepted_nonzero_exit: bool,
    timeout_seconds: int,
    model: str,
    sandbox: str,
    ask_for_approval: str,
    web_search: str,
    cd_dir: Path,
    output_schema: Path,
    output_path: Path,
    output_payload_present: bool,
    output_bytes: int,
    prompt: str,
    stdout: str,
    stderr: str,
) -> None:
    if usage_log_csv is None:
        return

    events, passthrough_lines = _parse_jsonl_events(stdout)
    usage, thread_id = _extract_usage(events)
    tokens_input = _as_int(usage.get("input_tokens"))
    tokens_cached_input = _as_int(usage.get("cached_input_tokens"))
    tokens_output = _as_int(usage.get("output_tokens"))
    tokens_total = _as_int(usage.get("total_tokens"))
    if tokens_total is None and tokens_input is not None and tokens_output is not None:
        tokens_total = tokens_input + tokens_output

    stderr_tail = _tail_lines(stderr)
    stdout_tail = _tail_lines("\n".join(passthrough_lines))

    context = usage_context or {}
    row = {
        "logged_at_utc": _utc_ts(datetime.now(UTC)),
        "started_at_utc": _utc_ts(started_at),
        "finished_at_utc": _utc_ts(finished_at),
        "duration_ms": duration_ms,
        "status": status,
        "exit_code": exit_code,
        "accepted_nonzero_exit": accepted_nonzero_exit,
        "timeout_seconds": timeout_seconds,
        "model": model,
        "sandbox": sandbox,
        "ask_for_approval": ask_for_approval,
        "web_search": web_search,
        "cd_dir": str(cd_dir.resolve()),
        "output_schema_path": str(output_schema.resolve()),
        "output_path": str(output_path.resolve()),
        "output_payload_present": output_payload_present,
        "output_bytes": output_bytes,
        "tokens_input": tokens_input,
        "tokens_cached_input": tokens_cached_input,
        "tokens_output": tokens_output,
        "tokens_total": tokens_total,
        "usage_json": json.dumps(usage, sort_keys=True) if usage else "",
        "thread_id": thread_id,
        "prompt_chars": len(prompt),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "prompt_text": prompt,
        "stderr_tail": stderr_tail,
        "stdout_tail": stdout_tail,
        "source": context.get("source"),
        "pipeline_id": context.get("pipeline_id"),
        "run_id": context.get("run_id"),
        "task_id": context.get("task_id"),
        "worker_id": context.get("worker_id"),
        "input_path": context.get("input_path"),
    }

    try:
        _append_usage_row(usage_log_csv, row)
    except OSError:
        # Usage logging must not break task execution.
        return


def run_codex_exec(
    *,
    cd_dir: Path,
    prompt: str,
    model: str,
    sandbox: str,
    ask_for_approval: str,
    web_search: str,
    output_schema: Path,
    output_path: Path,
    timeout_seconds: int,
    usage_log_csv: Path | None = None,
    usage_context: Mapping[str, object] | None = None,
) -> CodexExecResult:
    """Run codex exec and atomically move output into place on success."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(UTC)
    started_clock = time.perf_counter()

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
        str(cd_dir.resolve()),
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
        "--json",
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
        timeout_stdout = _coerce_text(exc.stdout)
        timeout_stderr = _coerce_text(exc.stderr)
        temp_has_payload = temp_output_path.exists() and temp_output_path.stat().st_size > 0
        output_bytes = temp_output_path.stat().st_size if temp_has_payload else 0
        if temp_output_path.exists():
            temp_output_path.unlink(missing_ok=True)
        finished_at = datetime.now(UTC)
        duration_ms = int((time.perf_counter() - started_clock) * 1000)
        _log_codex_activity(
            usage_log_csv=usage_log_csv,
            usage_context=usage_context,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            status="timeout",
            exit_code=None,
            accepted_nonzero_exit=False,
            timeout_seconds=timeout_seconds,
            model=model,
            sandbox=sandbox,
            ask_for_approval=ask_for_approval,
            web_search=web_search,
            cd_dir=cd_dir,
            output_schema=output_schema,
            output_path=output_path,
            output_payload_present=temp_has_payload,
            output_bytes=output_bytes,
            prompt=prompt,
            stdout=timeout_stdout,
            stderr=timeout_stderr,
        )
        raise CodexExecTimeoutError(
            f"codex exec timed out after {timeout_seconds}s"
        ) from exc

    passthrough_lines = _parse_jsonl_events(proc.stdout)[1]
    stderr_tail = _tail_lines(proc.stderr)
    stdout_tail = _tail_lines("\n".join(passthrough_lines))
    if not stderr_tail and stdout_tail:
        stderr_tail = stdout_tail
    temp_has_payload = temp_output_path.exists() and temp_output_path.stat().st_size > 0
    output_bytes = temp_output_path.stat().st_size if temp_has_payload else 0
    accepted_nonzero_exit = proc.returncode != 0 and temp_has_payload
    status = "ok"

    if proc.returncode != 0 and not temp_has_payload:
        temp_output_path.unlink(missing_ok=True)
        status = "failed"
        result = CodexExecResult(ok=False, exit_code=proc.returncode, stderr_tail=stderr_tail)
    elif not temp_has_payload:
        temp_output_path.unlink(missing_ok=True)
        status = "failed"
        result = CodexExecResult(
            ok=False,
            exit_code=proc.returncode,
            stderr_tail="codex exec exited 0 but produced no output file",
        )
    else:
        os.replace(temp_output_path, output_path)
        result = CodexExecResult(ok=True, exit_code=proc.returncode, stderr_tail=stderr_tail)

    finished_at = datetime.now(UTC)
    duration_ms = int((time.perf_counter() - started_clock) * 1000)
    _log_codex_activity(
        usage_log_csv=usage_log_csv,
        usage_context=usage_context,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=duration_ms,
        status=status,
        exit_code=proc.returncode,
        accepted_nonzero_exit=accepted_nonzero_exit,
        timeout_seconds=timeout_seconds,
        model=model,
        sandbox=sandbox,
        ask_for_approval=ask_for_approval,
        web_search=web_search,
        cd_dir=cd_dir,
        output_schema=output_schema,
        output_path=output_path,
        output_payload_present=temp_has_payload,
        output_bytes=output_bytes,
        prompt=prompt,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )
    return result
