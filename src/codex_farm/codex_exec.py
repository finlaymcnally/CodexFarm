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
import shutil
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
    stdout_tail: str = ""


class CodexExecTimeoutError(TimeoutError):
    """Raised when codex exec exceeds timeout."""

    def __init__(
        self,
        message: str,
        *,
        stdout_tail: str = "",
        stderr_tail: str = "",
    ) -> None:
        super().__init__(message)
        self.stdout_tail = stdout_tail
        self.stderr_tail = stderr_tail


class CodexExecRateLimitError(RuntimeError):
    """Raised when codex exec reports API rate limiting (HTTP 429)."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: int | None = None,
        stderr_tail: str = "",
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        self.stderr_tail = stderr_tail


_RATE_LIMIT_PATTERN = re.compile(
    r"\b429\b|too many requests|rate[ -]?limit(?:ed|ing)?",
    re.IGNORECASE,
)
_AUTH_FAILURE_PATTERN = re.compile(
    r"\b(?:401|403)\b[^\n]{0,100}\b(?:unauthoriz(?:ed|ation)|forbidden)\b"
    r"|backend-api/codex/responses"
    r"|not\s+(?:logged|signed)\s+in"
    r"|sign\s+in\s+with\s+chatgpt"
    r"|auth(?:entication|orization)\s+(?:failed|required)"
    r"|login\s+required",
    re.IGNORECASE,
)
_RETRY_AFTER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:retry(?:ing)?\s+after|retry-?after[:=\s]+)\s*(\d+)\s*(seconds?|secs?|s|minutes?|mins?|m)?",
        re.IGNORECASE,
    ),
    re.compile(
        r"try again in\s+(\d+)\s*(seconds?|secs?|s|minutes?|mins?|m)?",
        re.IGNORECASE,
    ),
)


def is_rate_limit_message(text: str) -> bool:
    """Return True when stderr/stdout text indicates rate limiting."""
    if not text:
        return False
    return _RATE_LIMIT_PATTERN.search(text) is not None


def is_auth_failure_message(text: str) -> bool:
    """Return True when stderr/stdout text indicates auth/login failure."""
    if not text:
        return False
    return _AUTH_FAILURE_PATTERN.search(text) is not None


def extract_retry_after_seconds(text: str) -> int | None:
    """Best-effort parser for explicit retry-after hints in provider messages."""
    if not text:
        return None
    for pattern in _RETRY_AFTER_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        try:
            value = int(match.group(1))
        except ValueError:
            continue
        if value <= 0:
            continue
        raw_unit = (match.group(2) or "s").strip().lower()
        if raw_unit.startswith("m"):
            value *= 60
        return value
    return None


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
    "reasoning_effort",
    "cd_dir",
    "output_schema_path",
    "output_path",
    "output_payload_present",
    "output_bytes",
    "output_sha256",
    "output_preview",
    "output_preview_chars",
    "output_preview_truncated",
    "tokens_input",
    "tokens_cached_input",
    "tokens_output",
    "tokens_reasoning",
    "tokens_total",
    "usage_json",
    "thread_id",
    "codex_event_count",
    "codex_event_types_json",
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
    "heads_up_applied",
    "heads_up_tip_count",
    "heads_up_input_signature",
    "heads_up_tip_ids_json",
    "heads_up_tip_texts_json",
    "heads_up_tip_scores_json",
    "attempt_index",
    "lease_claim_index",
    "execution_attempt_index",
    "retry_context_applied",
    "retry_previous_error",
    "retry_previous_error_chars",
    "retry_previous_error_sha256",
    "failure_category",
    "rate_limit_suspected",
)
_LEGACY_USAGE_LOG_FIELDS = (
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
_LEGACY_USAGE_LOG_FIELDS_WITH_REASONING = (
    _LEGACY_USAGE_LOG_FIELDS[:12]
    + ("reasoning_effort",)
    + _LEGACY_USAGE_LOG_FIELDS[12:]
)
_LEGACY_USAGE_LOG_FIELDS_WITH_HEADS_UP = (
    _LEGACY_USAGE_LOG_FIELDS_WITH_REASONING
    + (
        "heads_up_applied",
        "heads_up_tip_count",
        "heads_up_input_signature",
    )
)
_USAGE_LOG_FIELDS_WITHOUT_THREAD_ID = tuple(
    field for field in _USAGE_LOG_FIELDS if field != "thread_id"
)
_USAGE_LOG_FIELDS_WITHOUT_THREAD_ID_OR_FAILURE = tuple(
    field
    for field in _USAGE_LOG_FIELDS_WITHOUT_THREAD_ID
    if field not in ("failure_category", "rate_limit_suspected")
)
_KNOWN_USAGE_LOG_ROW_SCHEMAS = (
    _USAGE_LOG_FIELDS,
    _USAGE_LOG_FIELDS_WITHOUT_THREAD_ID,
    _USAGE_LOG_FIELDS_WITHOUT_THREAD_ID_OR_FAILURE,
    _LEGACY_USAGE_LOG_FIELDS_WITH_HEADS_UP,
    _LEGACY_USAGE_LOG_FIELDS_WITH_REASONING,
    _LEGACY_USAGE_LOG_FIELDS,
)
_USAGE_LOG_LOCK = threading.Lock()
_OUTPUT_PREVIEW_BYTES = 2400


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


def _event_types(events: list[dict[str, object]]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for event in events:
        raw_type = event.get("type")
        if not isinstance(raw_type, str):
            continue
        event_type = raw_type.strip()
        if not event_type or event_type in seen:
            continue
        seen.add(event_type)
        ordered.append(event_type)
    return ordered


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


def _extract_reasoning_tokens(usage: Mapping[str, object]) -> int | None:
    direct = _as_int(usage.get("reasoning_tokens"))
    if direct is not None and direct >= 0:
        return direct

    for detail_key in ("output_tokens_details", "completion_tokens_details"):
        raw_details = usage.get(detail_key)
        if not isinstance(raw_details, dict):
            continue
        parsed = _as_int(raw_details.get("reasoning_tokens"))
        if parsed is not None and parsed >= 0:
            return parsed
    return None


def _estimate_tokens_from_chars(char_count: int) -> int:
    if char_count <= 0:
        return 0
    # Lightweight fallback: common rough rule-of-thumb for English text.
    return (char_count + 3) // 4


def _csv_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _output_payload_snapshot(
    path: Path,
    *,
    preview_bytes: int = _OUTPUT_PREVIEW_BYTES,
) -> tuple[str, str, bool]:
    digest = hashlib.sha256()
    preview = bytearray()
    truncated = False
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                if len(preview) < preview_bytes:
                    remaining = preview_bytes - len(preview)
                    preview.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        truncated = True
                else:
                    truncated = True
    except OSError:
        return "", "", False
    return digest.hexdigest(), preview.decode("utf-8", errors="replace"), truncated


def _json_list_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value), sort_keys=True)
    return ""


def _failure_category(
    *,
    status: str,
    exit_code: int | None,
    output_payload_present: bool,
    accepted_nonzero_exit: bool,
) -> str:
    if status == "ok" and accepted_nonzero_exit:
        return "accepted_nonzero_exit"
    if status == "timeout":
        return "timeout"
    if status != "failed":
        return ""
    if exit_code is None:
        return "failed_unknown"
    if exit_code != 0 and not output_payload_present:
        return "nonzero_exit_no_payload"
    if exit_code == 0 and not output_payload_present:
        return "zero_exit_no_payload"
    return "failed"


def _recover_reasoning_tokens(usage_json: str) -> int | None:
    if not usage_json:
        return None
    try:
        parsed = json.loads(usage_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return _extract_reasoning_tokens(parsed)


def _row_schema_for_migration(
    *,
    existing_header: list[str],
    row: list[str],
) -> tuple[str, ...]:
    row_len = len(row)
    for schema in _KNOWN_USAGE_LOG_ROW_SCHEMAS:
        if len(schema) == row_len:
            return schema

    cleaned_header = tuple(
        cell.strip()
        for cell in existing_header
        if isinstance(cell, str) and cell.strip()
    )
    if cleaned_header and row_len <= len(cleaned_header):
        return cleaned_header[:row_len]
    if row_len <= len(existing_header):
        return tuple(existing_header[:row_len])

    # Last-resort fallback for unknown expanded schemas.
    return _USAGE_LOG_FIELDS[:row_len]


def _migrate_usage_row(
    *,
    existing_header: list[str],
    row: list[str],
) -> dict[str, str]:
    mapped: dict[str, str] = {}
    row_schema = _row_schema_for_migration(existing_header=existing_header, row=row)
    for index, value in enumerate(row):
        if index >= len(row_schema):
            break
        field_name = row_schema[index].strip()
        if field_name:
            mapped[field_name] = value

    normalized = {field: mapped.get(field, "") for field in _USAGE_LOG_FIELDS}
    if not normalized["tokens_reasoning"]:
        recovered = _recover_reasoning_tokens(normalized.get("usage_json", ""))
        if recovered is not None:
            normalized["tokens_reasoning"] = str(recovered)
    if not normalized["tokens_total"]:
        input_tokens = _as_int(normalized["tokens_input"])
        output_tokens = _as_int(normalized["tokens_output"])
        if input_tokens is not None and output_tokens is not None:
            normalized["tokens_total"] = str(input_tokens + output_tokens)
    return normalized


def _legacy_usage_backup_path(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d_%H.%M.%S")
    return path.with_name(f"{path.stem}.legacy-{stamp}{path.suffix}")


def _migrate_usage_log_schema_if_needed(handle, *, path: Path) -> None:
    handle.seek(0)
    reader = csv.reader(handle)
    try:
        existing_header = next(reader)
    except StopIteration:
        return
    if tuple(existing_header) == _USAGE_LOG_FIELDS:
        return

    migrated_rows = [
        _migrate_usage_row(existing_header=existing_header, row=row)
        for row in reader
        if row
    ]

    try:
        shutil.copy2(path, _legacy_usage_backup_path(path))
    except OSError:
        # A failed backup should not block telemetry writes.
        pass

    handle.seek(0)
    handle.truncate(0)
    writer = csv.DictWriter(handle, fieldnames=_USAGE_LOG_FIELDS)
    writer.writeheader()
    for migrated in migrated_rows:
        writer.writerow(migrated)


def _append_usage_row(path: Path, row: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _USAGE_LOG_LOCK:
        with path.open("a+", encoding="utf-8", newline="") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                _migrate_usage_log_schema_if_needed(handle, path=path)
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
    reasoning_effort: str | None,
    cd_dir: Path,
    output_schema: Path,
    output_schema_logical_path: Path | None,
    output_path: Path,
    output_payload_present: bool,
    output_bytes: int,
    output_sha256: str,
    output_preview: str,
    output_preview_truncated: bool,
    prompt: str,
    stdout: str,
    stderr: str,
) -> None:
    if usage_log_csv is None:
        return

    events, passthrough_lines = _parse_jsonl_events(stdout)
    event_types = _event_types(events)
    usage, thread_id = _extract_usage(events)
    tokens_input = _as_int(usage.get("input_tokens"))
    tokens_cached_input = _as_int(usage.get("cached_input_tokens"))
    tokens_output = _as_int(usage.get("output_tokens"))
    tokens_reasoning = _extract_reasoning_tokens(usage)
    tokens_total = _as_int(usage.get("total_tokens"))
    if tokens_total is None and tokens_input is not None and tokens_output is not None:
        tokens_total = tokens_input + tokens_output
    usage_json = json.dumps(usage, sort_keys=True) if usage else ""

    if (
        tokens_input is None
        and tokens_cached_input is None
        and tokens_output is None
        and tokens_total is None
    ):
        estimated_input_tokens = _estimate_tokens_from_chars(len(prompt))
        estimated_output_tokens = _estimate_tokens_from_chars(output_bytes)
        tokens_input = estimated_input_tokens
        tokens_cached_input = 0
        tokens_output = estimated_output_tokens
        tokens_total = estimated_input_tokens + estimated_output_tokens
        usage_json = json.dumps(
            {
                "estimated": True,
                "method": "chars_div_4",
                "input_chars": len(prompt),
                "output_bytes": output_bytes,
                "input_tokens": estimated_input_tokens,
                "cached_input_tokens": 0,
                "output_tokens": estimated_output_tokens,
                "total_tokens": tokens_total,
            },
            sort_keys=True,
        )

    stderr_tail = _tail_lines(stderr)
    stdout_tail = _tail_lines("\n".join(passthrough_lines))
    combined_tails = "\n".join(part for part in (stderr_tail, stdout_tail) if part).strip()

    context = usage_context or {}
    retry_previous_error = context.get("retry_previous_error")
    retry_error_text = ""
    if isinstance(retry_previous_error, str):
        retry_error_text = retry_previous_error.strip()
    retry_error_sha = (
        hashlib.sha256(retry_error_text.encode("utf-8")).hexdigest() if retry_error_text else ""
    )
    failure_category = _failure_category(
        status=status,
        exit_code=exit_code,
        output_payload_present=output_payload_present,
        accepted_nonzero_exit=accepted_nonzero_exit,
    )
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
        "reasoning_effort": reasoning_effort,
        "cd_dir": str(cd_dir.resolve()),
        "output_schema_path": str(
            (output_schema_logical_path or output_schema).expanduser().resolve()
        ),
        "output_path": str(output_path.resolve()),
        "output_payload_present": output_payload_present,
        "output_bytes": output_bytes,
        "output_sha256": output_sha256,
        "output_preview": output_preview,
        "output_preview_chars": len(output_preview),
        "output_preview_truncated": output_preview_truncated,
        "tokens_input": tokens_input,
        "tokens_cached_input": tokens_cached_input,
        "tokens_output": tokens_output,
        "tokens_reasoning": tokens_reasoning,
        "tokens_total": tokens_total,
        "usage_json": usage_json,
        "thread_id": thread_id,
        "codex_event_count": len(events),
        "codex_event_types_json": json.dumps(event_types, sort_keys=True) if event_types else "",
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
        "heads_up_applied": context.get("heads_up_applied"),
        "heads_up_tip_count": context.get("heads_up_tip_count"),
        "heads_up_input_signature": context.get("heads_up_input_signature"),
        "heads_up_tip_ids_json": _json_list_cell(context.get("heads_up_tip_ids_json")),
        "heads_up_tip_texts_json": _json_list_cell(context.get("heads_up_tip_texts_json")),
        "heads_up_tip_scores_json": _json_list_cell(context.get("heads_up_tip_scores_json")),
        "attempt_index": _as_int(context.get("attempt_index")),
        "lease_claim_index": _as_int(context.get("lease_claim_index")),
        "execution_attempt_index": _as_int(context.get("execution_attempt_index")),
        "retry_context_applied": context.get("retry_context_applied"),
        "retry_previous_error": retry_error_text,
        "retry_previous_error_chars": len(retry_error_text),
        "retry_previous_error_sha256": retry_error_sha,
        "failure_category": failure_category,
        "rate_limit_suspected": is_rate_limit_message(combined_tails),
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
    output_schema_logical_path: Path | None = None,
    reasoning_effort: str | None = None,
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
    ]
    if reasoning_effort is not None:
        cmd.extend(
            [
                "--config",
                f'model_reasoning_effort="{reasoning_effort}"',
            ]
        )
    cmd.extend(
        [
            "--output-schema",
            str(output_schema.resolve()),
            "--output-last-message",
            str(temp_output_path),
            "--json",
            prompt,
        ]
    )

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
        timeout_stdout_tail = _tail_lines(timeout_stdout)
        timeout_stderr_tail = _tail_lines(timeout_stderr)
        temp_has_payload = temp_output_path.exists() and temp_output_path.stat().st_size > 0
        output_bytes = temp_output_path.stat().st_size if temp_has_payload else 0
        output_sha256 = ""
        output_preview = ""
        output_preview_truncated = False
        if temp_has_payload:
            output_sha256, output_preview, output_preview_truncated = _output_payload_snapshot(
                temp_output_path
            )
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
            reasoning_effort=reasoning_effort,
            cd_dir=cd_dir,
            output_schema=output_schema,
            output_schema_logical_path=output_schema_logical_path,
            output_path=output_path,
            output_payload_present=temp_has_payload,
            output_bytes=output_bytes,
            output_sha256=output_sha256,
            output_preview=output_preview,
            output_preview_truncated=output_preview_truncated,
            prompt=prompt,
            stdout=timeout_stdout,
            stderr=timeout_stderr,
        )
        raise CodexExecTimeoutError(
            f"codex exec timed out after {timeout_seconds}s",
            stdout_tail=timeout_stdout_tail,
            stderr_tail=timeout_stderr_tail,
        ) from exc

    passthrough_lines = _parse_jsonl_events(proc.stdout)[1]
    stderr_tail = _tail_lines(proc.stderr)
    stdout_tail = _tail_lines("\n".join(passthrough_lines))
    if not stderr_tail and stdout_tail:
        stderr_tail = stdout_tail
    temp_has_payload = temp_output_path.exists() and temp_output_path.stat().st_size > 0
    output_bytes = temp_output_path.stat().st_size if temp_has_payload else 0
    output_sha256 = ""
    output_preview = ""
    output_preview_truncated = False
    if temp_has_payload:
        output_sha256, output_preview, output_preview_truncated = _output_payload_snapshot(
            temp_output_path
        )
    accepted_nonzero_exit = proc.returncode != 0 and temp_has_payload
    status = "ok"

    if proc.returncode != 0 and not temp_has_payload:
        temp_output_path.unlink(missing_ok=True)
        status = "failed"
        result = CodexExecResult(
            ok=False,
            exit_code=proc.returncode,
            stderr_tail=stderr_tail,
            stdout_tail=stdout_tail,
        )
    elif not temp_has_payload:
        temp_output_path.unlink(missing_ok=True)
        status = "failed"
        result = CodexExecResult(
            ok=False,
            exit_code=proc.returncode,
            stderr_tail="codex exec exited 0 but produced no output file",
            stdout_tail=stdout_tail,
        )
    else:
        os.replace(temp_output_path, output_path)
        result = CodexExecResult(
            ok=True,
            exit_code=proc.returncode,
            stderr_tail=stderr_tail,
            stdout_tail=stdout_tail,
        )

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
        reasoning_effort=reasoning_effort,
        cd_dir=cd_dir,
        output_schema=output_schema,
        output_schema_logical_path=output_schema_logical_path,
        output_path=output_path,
        output_payload_present=temp_has_payload,
        output_bytes=output_bytes,
        output_sha256=output_sha256,
        output_preview=output_preview,
        output_preview_truncated=output_preview_truncated,
        prompt=prompt,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )
    return result
