"""Read Codex rollout artifacts and normalize reasoning metadata."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Mapping

_RECORDER_ERROR_PATTERN = re.compile(
    r"failed to record rollout items|failed to queue rollout items|channel closed",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RolloutReasoningResult:
    status: str
    thread_id: str | None
    codex_home_path: Path | None
    rollout_path: Path | None
    reasoning_item_count: int
    summary_count: int
    summary_texts: list[str]
    encrypted_reasoning_present: bool
    reasoning_output_tokens: int | None
    recorder_error_detected: bool


def resolve_codex_home_path(
    *,
    env_overrides: Mapping[str, str] | None = None,
    usage_context: Mapping[str, object] | None = None,
    codex_home_path: Path | str | None = None,
) -> Path:
    if codex_home_path is not None:
        raw_value = codex_home_path
    else:
        raw_value = (env_overrides or {}).get("CODEX_HOME") or (usage_context or {}).get(
            "codex_home_path"
        )
    if isinstance(raw_value, Path):
        candidate = raw_value
    elif isinstance(raw_value, str) and raw_value.strip():
        candidate = Path(raw_value.strip())
    else:
        candidate = Path.home() / ".codex"
    return candidate.expanduser().resolve(strict=False)


def harvest_rollout_reasoning(
    *,
    thread_id: str | None,
    codex_home_path: Path | None,
    stderr_text: str,
) -> RolloutReasoningResult:
    effective_home = resolve_codex_home_path(codex_home_path=codex_home_path)
    recorder_error_detected = _RECORDER_ERROR_PATTERN.search(stderr_text or "") is not None
    normalized_thread_id = (thread_id or "").strip() or None
    if normalized_thread_id is None:
        return RolloutReasoningResult(
            status="thread_missing",
            thread_id=None,
            codex_home_path=effective_home,
            rollout_path=None,
            reasoning_item_count=0,
            summary_count=0,
            summary_texts=[],
            encrypted_reasoning_present=False,
            reasoning_output_tokens=None,
            recorder_error_detected=recorder_error_detected,
        )

    rollout_path = _find_rollout_path(
        sessions_dir=effective_home / "sessions",
        thread_id=normalized_thread_id,
    )
    if rollout_path is None:
        return RolloutReasoningResult(
            status="rollout_missing",
            thread_id=normalized_thread_id,
            codex_home_path=effective_home,
            rollout_path=None,
            reasoning_item_count=0,
            summary_count=0,
            summary_texts=[],
            encrypted_reasoning_present=False,
            reasoning_output_tokens=None,
            recorder_error_detected=recorder_error_detected,
        )

    reasoning_item_count = 0
    encrypted_reasoning_present = False
    reasoning_output_tokens: int | None = None
    summary_texts: list[str] = []
    seen_summary_texts: set[str] = set()

    try:
        lines = rollout_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue

        line_type = payload.get("type")
        line_payload = payload.get("payload")
        if line_type == "response_item" and isinstance(line_payload, dict):
            if line_payload.get("type") != "reasoning":
                continue
            reasoning_item_count += 1
            if line_payload.get("encrypted_content"):
                encrypted_reasoning_present = True
            for text in _extract_summary_texts(line_payload):
                if text in seen_summary_texts:
                    continue
                seen_summary_texts.add(text)
                summary_texts.append(text)
            continue

        if line_type == "event_msg" and isinstance(line_payload, dict):
            parsed_tokens = _extract_reasoning_output_tokens(line_payload)
            if parsed_tokens is not None and parsed_tokens >= 0:
                if reasoning_output_tokens is None:
                    reasoning_output_tokens = parsed_tokens
                else:
                    reasoning_output_tokens = max(reasoning_output_tokens, parsed_tokens)

    if reasoning_item_count == 0:
        status = "reasoning_missing"
    elif summary_texts:
        status = "summary_present"
    elif encrypted_reasoning_present:
        status = "summary_empty_encrypted_present"
    else:
        status = "summary_empty"

    return RolloutReasoningResult(
        status=status,
        thread_id=normalized_thread_id,
        codex_home_path=effective_home,
        rollout_path=rollout_path.resolve(strict=False),
        reasoning_item_count=reasoning_item_count,
        summary_count=len(summary_texts),
        summary_texts=summary_texts,
        encrypted_reasoning_present=encrypted_reasoning_present,
        reasoning_output_tokens=reasoning_output_tokens,
        recorder_error_detected=recorder_error_detected,
    )


def _find_rollout_path(*, sessions_dir: Path, thread_id: str) -> Path | None:
    if not sessions_dir.exists():
        return None

    direct_matches = sorted(
        sessions_dir.rglob(f"rollout-*{thread_id}*.jsonl"),
        key=_rollout_sort_key,
    )
    if direct_matches:
        return direct_matches[-1]

    content_matches: list[Path] = []
    for candidate in sessions_dir.rglob("rollout-*.jsonl"):
        try:
            if thread_id in candidate.read_text(encoding="utf-8", errors="replace"):
                content_matches.append(candidate)
        except OSError:
            continue
    if not content_matches:
        return None
    content_matches.sort(key=_rollout_sort_key)
    return content_matches[-1]


def _rollout_sort_key(path: Path) -> tuple[float, str]:
    try:
        stat = path.stat()
        mtime = stat.st_mtime
    except OSError:
        mtime = 0.0
    return (mtime, str(path))


def _extract_summary_texts(reasoning_payload: Mapping[str, object]) -> list[str]:
    results: list[str] = []
    seen: set[str] = set()
    for key in ("summary", "summary_text"):
        for text in _walk_summary_text(reasoning_payload.get(key)):
            if text in seen:
                continue
            seen.add(text)
            results.append(text)
    return results


def _walk_summary_text(value: object) -> list[str]:
    if isinstance(value, str):
        normalized = value.strip()
        return [normalized] if normalized else []
    if isinstance(value, list):
        results: list[str] = []
        for item in value:
            results.extend(_walk_summary_text(item))
        return results
    if isinstance(value, dict):
        results: list[str] = []
        for key in ("summary_text", "text"):
            raw_text = value.get(key)
            if isinstance(raw_text, str):
                normalized = raw_text.strip()
                if normalized:
                    results.append(normalized)
        if results:
            return results
        for key in ("summary", "content"):
            if key in value:
                results.extend(_walk_summary_text(value.get(key)))
        return results
    return []


def _extract_reasoning_output_tokens(payload: Mapping[str, object]) -> int | None:
    if payload.get("type") != "token_count":
        return None
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    for usage_key in ("total_token_usage", "last_token_usage"):
        usage = info.get(usage_key)
        if not isinstance(usage, dict):
            continue
        raw_tokens = usage.get("reasoning_output_tokens")
        if isinstance(raw_tokens, bool):
            continue
        if isinstance(raw_tokens, int):
            return raw_tokens
        if isinstance(raw_tokens, str):
            try:
                return int(raw_tokens)
            except ValueError:
                continue
    return None
