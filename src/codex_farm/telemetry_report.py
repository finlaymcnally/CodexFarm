"""Caller-facing telemetry report builder for prompt/data/schema refinement loops."""

from __future__ import annotations

import csv
from datetime import UTC, datetime
import json
from pathlib import Path
import re


TELEMETRY_REPORT_SCHEMA_VERSION = 2
_SCHEMA_PATH_PATTERN = re.compile(r"Schema validation failed at ([^:]+):")
_MAX_RECENT_ROWS = 2000
_MAX_BREAKDOWN_ROWS = 30


def read_telemetry_rows(csv_path: Path) -> tuple[list[dict[str, str]], list[str]]:
    """Read telemetry rows from CSV, returning rows plus non-fatal warnings."""
    resolved = csv_path.expanduser().resolve()
    warnings: list[str] = []
    if not resolved.exists():
        warnings.append(f"Telemetry CSV does not exist: {resolved}")
        return [], warnings
    if not resolved.is_file():
        warnings.append(f"Telemetry CSV path is not a file: {resolved}")
        return [], warnings

    try:
        with resolved.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = [dict(row) for row in reader if row]
    except OSError as exc:
        warnings.append(f"Could not read telemetry CSV ({resolved}): {exc}")
        return [], warnings

    return rows, warnings


def build_telemetry_report(
    rows: list[dict[str, str]],
    *,
    run_id: str | None = None,
    pipeline_id: str | None = None,
    source: str | None = None,
    status: str | None = None,
    limit: int = 500,
    recommendations_limit: int = 10,
    terminal_errors: list[str] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, object]:
    """Build machine-usable telemetry report and recommendation payload."""
    safe_limit = max(1, min(_MAX_RECENT_ROWS, int(limit)))
    safe_rec_limit = max(1, min(30, int(recommendations_limit)))
    warning_rows = list(warnings or [])
    terminal_error_rows = [
        item.strip()
        for item in (terminal_errors or [])
        if isinstance(item, str) and item.strip()
    ]

    filtered = _filter_rows(
        rows=rows,
        run_id=run_id,
        pipeline_id=pipeline_id,
        source=source,
        status=status,
    )
    ordered = _sort_rows_by_logged_at_desc(filtered)
    limited = ordered[:safe_limit]

    status_counts = {"ok": 0, "failed": 0, "timeout": 0, "other": 0}
    failure_category_counts: dict[str, int] = {}
    duration_values: list[int] = []
    token_total = 0
    reasoning_token_total = 0
    retry_context_rows = 0
    heads_up_applied_rows = 0
    heads_up_tip_rows = 0
    rate_limit_rows = 0
    accepted_nonzero_rows = 0
    output_missing_rows = 0
    output_preview_truncated_rows = 0
    attempt_counts: dict[str, int] = {}
    schema_path_counts: dict[str, int] = {}
    schema_issue_type_counts: dict[str, int] = {}
    retry_error_patterns: dict[str, dict[str, object]] = {}
    tip_effectiveness: dict[str, dict[str, object]] = {}
    model_reasoning_stats: dict[str, dict[str, object]] = {}
    prompt_fingerprint_stats: dict[str, dict[str, object]] = {}
    input_failure_hotspots: dict[str, dict[str, object]] = {}
    codex_event_type_counts: dict[str, int] = {}
    codex_rows_with_turn_completed = 0
    codex_rows_with_no_events = 0
    codex_event_count_total = 0
    retry_applied_effect = _new_effectiveness_counter()
    retry_not_applied_effect = _new_effectiveness_counter()
    heads_up_applied_effect = _new_effectiveness_counter()
    heads_up_not_applied_effect = _new_effectiveness_counter()
    retry_and_heads_up_effect = _new_effectiveness_counter()
    not_retry_and_heads_up_effect = _new_effectiveness_counter()
    schema_messages: list[str] = []

    for row in limited:
        row_status = _normalized_status(row.get("status", ""))
        retry_applied = _as_bool(row.get("retry_context_applied"))
        heads_up_applied = _as_bool(row.get("heads_up_applied"))
        output_payload_present = _row_output_payload_present(row)
        status_counts[row_status] = status_counts.get(row_status, 0) + 1

        failure_category = _clean_text(row.get("failure_category"))
        if failure_category:
            failure_category_counts[failure_category] = failure_category_counts.get(failure_category, 0) + 1

        duration_values.append(_as_int(row.get("duration_ms")) or 0)
        token_total += _as_int(row.get("tokens_total")) or 0
        reasoning_token_total += _as_int(row.get("tokens_reasoning")) or 0

        if retry_applied:
            retry_context_rows += 1
            _bump_effectiveness_counter(retry_applied_effect, row_status)
        else:
            _bump_effectiveness_counter(retry_not_applied_effect, row_status)
        if heads_up_applied:
            heads_up_applied_rows += 1
            _bump_effectiveness_counter(heads_up_applied_effect, row_status)
        else:
            _bump_effectiveness_counter(heads_up_not_applied_effect, row_status)
        if retry_applied and heads_up_applied:
            _bump_effectiveness_counter(retry_and_heads_up_effect, row_status)
        else:
            _bump_effectiveness_counter(not_retry_and_heads_up_effect, row_status)
        if (_as_int(row.get("heads_up_tip_count")) or 0) > 0:
            heads_up_tip_rows += 1
        if _as_bool(row.get("rate_limit_suspected")):
            rate_limit_rows += 1
        if _as_bool(row.get("accepted_nonzero_exit")):
            accepted_nonzero_rows += 1
        if not output_payload_present:
            output_missing_rows += 1
        if _as_bool(row.get("output_preview_truncated")):
            output_preview_truncated_rows += 1

        model = _clean_text(row.get("model")) or "<unknown>"
        reasoning_effort = _clean_text(row.get("reasoning_effort")) or "<default>"
        model_key = f"{model}\x1f{reasoning_effort}"
        model_slot = model_reasoning_stats.setdefault(
            model_key,
            {
                "model": model,
                "reasoning_effort": reasoning_effort,
                "calls": 0,
                "ok": 0,
                "failed": 0,
                "timeout": 0,
                "other": 0,
                "_duration_total_ms": 0,
                "_tokens_total": 0,
                "_tokens_reasoning_total": 0,
                "output_missing_rows": 0,
                "retry_context_rows": 0,
                "heads_up_rows": 0,
            },
        )
        model_slot["calls"] = int(model_slot["calls"]) + 1
        model_slot[row_status] = int(model_slot.get(row_status, 0)) + 1
        model_slot["_duration_total_ms"] = int(model_slot["_duration_total_ms"]) + (_as_int(row.get("duration_ms")) or 0)
        model_slot["_tokens_total"] = int(model_slot["_tokens_total"]) + (_as_int(row.get("tokens_total")) or 0)
        model_slot["_tokens_reasoning_total"] = int(model_slot["_tokens_reasoning_total"]) + (
            _as_int(row.get("tokens_reasoning")) or 0
        )
        if not output_payload_present:
            model_slot["output_missing_rows"] = int(model_slot["output_missing_rows"]) + 1
        if retry_applied:
            model_slot["retry_context_rows"] = int(model_slot["retry_context_rows"]) + 1
        if heads_up_applied:
            model_slot["heads_up_rows"] = int(model_slot["heads_up_rows"]) + 1

        prompt_sha = _clean_text(row.get("prompt_sha256"))
        if prompt_sha:
            prompt_slot = prompt_fingerprint_stats.setdefault(
                prompt_sha,
                {
                    "prompt_sha256": prompt_sha,
                    "sample_prompt": _trim_text(_clean_text(row.get("prompt_text")), 260),
                    "calls": 0,
                    "ok": 0,
                    "failed": 0,
                    "timeout": 0,
                    "other": 0,
                    "output_missing_rows": 0,
                },
            )
            prompt_slot["calls"] = int(prompt_slot["calls"]) + 1
            prompt_slot[row_status] = int(prompt_slot.get(row_status, 0)) + 1
            if not output_payload_present:
                prompt_slot["output_missing_rows"] = int(prompt_slot["output_missing_rows"]) + 1

        input_path = _clean_text(row.get("input_path"))
        if input_path and row_status != "ok":
            input_slot = input_failure_hotspots.setdefault(
                input_path,
                {
                    "input_path": input_path,
                    "non_ok_rows": 0,
                    "failed": 0,
                    "timeout": 0,
                    "other": 0,
                    "_failure_categories": {},
                    "sample_error": "",
                },
            )
            input_slot["non_ok_rows"] = int(input_slot["non_ok_rows"]) + 1
            input_slot[row_status] = int(input_slot.get(row_status, 0)) + 1
            if failure_category:
                category_counts = dict(input_slot.get("_failure_categories", {}))
                category_counts[failure_category] = int(category_counts.get(failure_category, 0)) + 1
                input_slot["_failure_categories"] = category_counts
            if not input_slot["sample_error"]:
                sample_error = _clean_text(row.get("retry_previous_error")) or _clean_text(row.get("stderr_tail"))
                input_slot["sample_error"] = _trim_text(sample_error, 220) if sample_error else ""

        event_types = _json_list(row.get("codex_event_types_json"))
        event_count = _as_int(row.get("codex_event_count"))
        effective_event_count = event_count if event_count is not None else len(event_types)
        codex_event_count_total += max(0, effective_event_count)
        if effective_event_count == 0 and not event_types:
            codex_rows_with_no_events += 1
        if "turn.completed" in event_types:
            codex_rows_with_turn_completed += 1
        for event_type in event_types:
            if not event_type:
                continue
            codex_event_type_counts[event_type] = codex_event_type_counts.get(event_type, 0) + 1

        attempt_index = _as_int(row.get("attempt_index"))
        if attempt_index is not None and attempt_index >= 1:
            key = str(attempt_index)
            attempt_counts[key] = attempt_counts.get(key, 0) + 1

        retry_error_text = _clean_text(row.get("retry_previous_error"))
        if retry_error_text:
            error_hash = _clean_text(row.get("retry_previous_error_sha256")) or "<missing-hash>"
            pattern = retry_error_patterns.setdefault(
                error_hash,
                {
                    "error_sha256": error_hash,
                    "sample_error": _trim_text(retry_error_text, 220),
                    "count": 0,
                    "ok": 0,
                    "failed": 0,
                    "timeout": 0,
                    "other": 0,
                },
            )
            pattern["count"] = int(pattern["count"]) + 1
            pattern[row_status] = int(pattern.get(row_status, 0)) + 1
            _collect_schema_signals(
                retry_error_text,
                schema_path_counts=schema_path_counts,
                schema_issue_type_counts=schema_issue_type_counts,
                schema_messages=schema_messages,
            )

        for message_field in ("stderr_tail",):
            message = _clean_text(row.get(message_field))
            if message:
                _collect_schema_signals(
                    message,
                    schema_path_counts=schema_path_counts,
                    schema_issue_type_counts=schema_issue_type_counts,
                    schema_messages=schema_messages,
                )

        row_tip_ids = _json_list(row.get("heads_up_tip_ids_json"))
        row_tip_texts = _json_list(row.get("heads_up_tip_texts_json"))
        row_tip_scores = _json_float_list(row.get("heads_up_tip_scores_json"))

        max_tips = max(len(row_tip_ids), len(row_tip_texts), len(row_tip_scores))
        for idx in range(max_tips):
            tip_id = row_tip_ids[idx] if idx < len(row_tip_ids) else ""
            tip_text = row_tip_texts[idx] if idx < len(row_tip_texts) else ""
            tip_score = row_tip_scores[idx] if idx < len(row_tip_scores) else None
            tip_key = tip_id or tip_text
            if not tip_key:
                continue
            slot = tip_effectiveness.setdefault(
                tip_key,
                {
                    "tip_id": tip_id or None,
                    "tip_text": tip_text or None,
                    "uses": 0,
                    "ok": 0,
                    "failed": 0,
                    "timeout": 0,
                    "other": 0,
                    "score_avg": 0.0,
                    "_score_count": 0,
                },
            )
            slot["uses"] = int(slot["uses"]) + 1
            slot[row_status] = int(slot.get(row_status, 0)) + 1
            if tip_score is not None:
                slot["score_avg"] = float(slot["score_avg"]) + tip_score
                slot["_score_count"] = int(slot["_score_count"]) + 1

    for message in terminal_error_rows:
        _collect_schema_signals(
            message,
            schema_path_counts=schema_path_counts,
            schema_issue_type_counts=schema_issue_type_counts,
            schema_messages=schema_messages,
        )

    total_rows = len(limited)
    ok_rows = status_counts.get("ok", 0)
    failed_rows = status_counts.get("failed", 0)
    timeout_rows = status_counts.get("timeout", 0)
    non_ok_rows = failed_rows + timeout_rows + status_counts.get("other", 0)
    rows_without_turn_completed = max(0, total_rows - codex_rows_with_turn_completed)
    retry_effectiveness_summary = _effectiveness_summary(
        applied=retry_applied_effect,
        control=retry_not_applied_effect,
    )
    heads_up_effectiveness_summary = _effectiveness_summary(
        applied=heads_up_applied_effect,
        control=heads_up_not_applied_effect,
    )
    retry_and_heads_up_effectiveness_summary = _effectiveness_summary(
        applied=retry_and_heads_up_effect,
        control=not_retry_and_heads_up_effect,
    )
    model_reasoning_breakdown = _sorted_model_reasoning_breakdown(model_reasoning_stats)
    prompt_fingerprint_breakdown = _sorted_prompt_fingerprint_breakdown(prompt_fingerprint_stats)
    input_hotspot_breakdown = _sorted_input_failure_hotspots(input_failure_hotspots)
    reasoning_signal_summary = {
        "rows_with_turn_completed": codex_rows_with_turn_completed,
        "rows_without_turn_completed": rows_without_turn_completed,
        "rows_with_no_events": codex_rows_with_no_events,
        "event_count_total": codex_event_count_total,
        "event_count_avg_per_call": (
            round(codex_event_count_total / total_rows, 2) if total_rows else 0.0
        ),
        "event_types": _sorted_count_rows(
            codex_event_type_counts,
            key_name="event_type",
            total=max(1, total_rows),
            limit=20,
        ),
    }

    recommendations = _build_recommendations(
        total_rows=total_rows,
        status_counts=status_counts,
        failure_category_counts=failure_category_counts,
        schema_path_counts=schema_path_counts,
        schema_issue_type_counts=schema_issue_type_counts,
        retry_error_patterns=retry_error_patterns,
        tip_effectiveness=tip_effectiveness,
        rate_limit_rows=rate_limit_rows,
        output_missing_rows=output_missing_rows,
        output_preview_truncated_rows=output_preview_truncated_rows,
        terminal_error_count=len(terminal_error_rows),
        model_reasoning_breakdown=model_reasoning_breakdown,
        retry_effectiveness_summary=retry_effectiveness_summary,
        heads_up_effectiveness_summary=heads_up_effectiveness_summary,
        rows_without_turn_completed=rows_without_turn_completed,
        rows_with_no_events=codex_rows_with_no_events,
        recommendations_limit=safe_rec_limit,
    )
    tuning_playbook = _build_tuning_playbook(
        total_rows=total_rows,
        failure_category_counts=failure_category_counts,
        schema_path_counts=schema_path_counts,
        schema_issue_type_counts=schema_issue_type_counts,
        rate_limit_rows=rate_limit_rows,
        timeout_rows=timeout_rows,
        output_preview_truncated_rows=output_preview_truncated_rows,
        retry_effectiveness_summary=retry_effectiveness_summary,
        heads_up_effectiveness_summary=heads_up_effectiveness_summary,
        model_reasoning_breakdown=model_reasoning_breakdown,
    )

    report = {
        "schema_version": TELEMETRY_REPORT_SCHEMA_VERSION,
        "generated_at_utc": _utc_ts(datetime.now(UTC)),
        "filters": {
            "run_id": run_id,
            "pipeline_id": pipeline_id,
            "source": source,
            "status": status,
            "limit": safe_limit,
        },
        "warnings": warning_rows,
        "row_count_input": len(rows),
        "matched_rows": total_rows,
        "summary": {
            "status_counts": status_counts,
            "success_rate_pct": _ratio_pct(ok_rows, total_rows),
            "failure_rate_pct": _ratio_pct(non_ok_rows, total_rows),
            "retry_context_rows": retry_context_rows,
            "heads_up_applied_rows": heads_up_applied_rows,
            "heads_up_tip_rows": heads_up_tip_rows,
            "rate_limit_suspected_rows": rate_limit_rows,
            "accepted_nonzero_exit_rows": accepted_nonzero_rows,
            "output_missing_rows": output_missing_rows,
            "output_preview_truncated_rows": output_preview_truncated_rows,
            "attempt_index_counts": dict(sorted(attempt_counts.items(), key=lambda item: int(item[0]))),
            "duration_avg_ms": _avg_int(duration_values),
            "duration_p95_ms": _percentile(duration_values, 95),
            "tokens_total": token_total,
            "tokens_avg_per_call": int(round(token_total / total_rows)) if total_rows else 0,
            "tokens_reasoning_total": reasoning_token_total,
            "tokens_reasoning_avg_per_call": (
                int(round(reasoning_token_total / total_rows)) if total_rows else 0
            ),
        },
        "failure_patterns": {
            "failure_categories": _sorted_count_rows(
                failure_category_counts,
                key_name="category",
                total=total_rows,
            ),
            "schema_paths": _sorted_count_rows(
                schema_path_counts,
                key_name="path",
                total=max(1, len(schema_messages)),
            ),
            "schema_issue_types": _sorted_count_rows(
                schema_issue_type_counts,
                key_name="issue_type",
                total=max(1, len(schema_messages)),
            ),
            "retry_error_patterns": _sorted_retry_patterns(retry_error_patterns),
        },
        "heads_up_patterns": {
            "tip_effectiveness": _sorted_tip_effectiveness(tip_effectiveness),
        },
        "insights": {
            "model_reasoning_breakdown": model_reasoning_breakdown,
            "prompt_fingerprint_breakdown": prompt_fingerprint_breakdown,
            "input_failure_hotspots": input_hotspot_breakdown,
            "reasoning_signals": reasoning_signal_summary,
            "pass_forward_effectiveness": {
                "retry_context": retry_effectiveness_summary,
                "heads_up": heads_up_effectiveness_summary,
                "retry_and_heads_up": retry_and_heads_up_effectiveness_summary,
            },
        },
        "terminal_errors": {
            "count": len(terminal_error_rows),
            "samples": [_trim_text(item, 220) for item in terminal_error_rows[:8]],
        },
        "recommendations": recommendations,
        "tuning_playbook": tuning_playbook,
        "recent_rows": _recent_rows(limited, max_rows=min(50, safe_limit)),
    }
    return report


def _filter_rows(
    *,
    rows: list[dict[str, str]],
    run_id: str | None,
    pipeline_id: str | None,
    source: str | None,
    status: str | None,
) -> list[dict[str, str]]:
    filtered: list[dict[str, str]] = []
    target_status = _normalized_status(status or "") if status else None
    for row in rows:
        if run_id is not None and _clean_text(row.get("run_id")) != run_id:
            continue
        if pipeline_id is not None and _clean_text(row.get("pipeline_id")) != pipeline_id:
            continue
        if source is not None and _clean_text(row.get("source")) != source:
            continue
        if target_status is not None and _normalized_status(row.get("status", "")) != target_status:
            continue
        filtered.append(row)
    return filtered


def _sort_rows_by_logged_at_desc(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    decorated: list[tuple[float, int, dict[str, str]]] = []
    for idx, row in enumerate(rows):
        logged_at = _parse_ts(_clean_text(row.get("logged_at_utc")))
        sort_key = logged_at.timestamp() if logged_at is not None else float("-inf")
        decorated.append((sort_key, idx, row))
    decorated.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [row for _sort_key, _idx, row in decorated]


def _collect_schema_signals(
    message: str,
    *,
    schema_path_counts: dict[str, int],
    schema_issue_type_counts: dict[str, int],
    schema_messages: list[str],
) -> None:
    matched = False
    for match in _SCHEMA_PATH_PATTERN.finditer(message):
        path = match.group(1).strip()
        if not path:
            continue
        schema_path_counts[path] = schema_path_counts.get(path, 0) + 1
        matched = True

    lowered = message.lower()
    issue_type = ""
    if "schema validation failed" in lowered:
        matched = True
        schema_messages.append(message)
    if "required property" in lowered:
        issue_type = "required_property"
    elif "additional properties are not allowed" in lowered:
        issue_type = "additional_properties"
    elif "is not of type" in lowered:
        issue_type = "type_mismatch"
    elif "is not one of" in lowered:
        issue_type = "enum_mismatch"
    elif "is too short" in lowered or "is too long" in lowered:
        issue_type = "size_constraint"
    elif "does not match" in lowered and "pattern" in lowered:
        issue_type = "pattern_mismatch"
    elif matched:
        issue_type = "other"

    if issue_type:
        schema_issue_type_counts[issue_type] = schema_issue_type_counts.get(issue_type, 0) + 1


def _build_recommendations(
    *,
    total_rows: int,
    status_counts: dict[str, int],
    failure_category_counts: dict[str, int],
    schema_path_counts: dict[str, int],
    schema_issue_type_counts: dict[str, int],
    retry_error_patterns: dict[str, dict[str, object]],
    tip_effectiveness: dict[str, dict[str, object]],
    rate_limit_rows: int,
    output_missing_rows: int,
    output_preview_truncated_rows: int,
    terminal_error_count: int,
    model_reasoning_breakdown: list[dict[str, object]],
    retry_effectiveness_summary: dict[str, object],
    heads_up_effectiveness_summary: dict[str, object],
    rows_without_turn_completed: int,
    rows_with_no_events: int,
    recommendations_limit: int,
) -> dict[str, list[dict[str, object]]]:
    recommendations: dict[str, list[dict[str, object]]] = {
        "prompt": [],
        "input_data": [],
        "output_schema": [],
        "runtime": [],
    }
    seen_codes: set[str] = set()

    def add(
        category: str,
        *,
        code: str,
        priority: str,
        action: str,
        reason: str,
        evidence: dict[str, object],
    ) -> None:
        if category not in recommendations:
            return
        if code in seen_codes:
            return
        if len(recommendations[category]) >= recommendations_limit:
            return
        seen_codes.add(code)
        recommendations[category].append(
            {
                "code": code,
                "priority": priority,
                "action": action,
                "reason": reason,
                "evidence": evidence,
            }
        )

    failed_rows = status_counts.get("failed", 0)
    timeout_rows = status_counts.get("timeout", 0)
    non_ok_rows = failed_rows + timeout_rows + status_counts.get("other", 0)

    nonzero_no_payload = failure_category_counts.get("nonzero_exit_no_payload", 0)
    zero_no_payload = failure_category_counts.get("zero_exit_no_payload", 0)
    if nonzero_no_payload + zero_no_payload >= 1:
        missing_count = nonzero_no_payload + zero_no_payload
        add(
            "prompt",
            code="prompt.raw_json_only_guardrail",
            priority="high",
            action=(
                "Strengthen prompt ending with explicit output rules: "
                "'Return only JSON that matches the schema, no markdown/prose.'"
            ),
            reason="Rows are failing with missing output payload despite Codex execution.",
            evidence={
                "missing_payload_failures": missing_count,
                "share_pct": _ratio_pct(missing_count, max(1, total_rows)),
            },
        )

    if output_missing_rows >= max(2, int(total_rows * 0.2)):
        add(
            "runtime",
            code="runtime.detect_output_write_failures",
            priority="medium",
            action=(
                "Inspect model/provider reliability for empty output writes; "
                "consider reducing concurrency or splitting long prompts."
            ),
            reason="A high share of calls produced no output payload.",
            evidence={
                "output_missing_rows": output_missing_rows,
                "share_pct": _ratio_pct(output_missing_rows, max(1, total_rows)),
            },
        )

    if timeout_rows >= max(1, int(total_rows * 0.2)):
        add(
            "runtime",
            code="runtime.timeout_pressure",
            priority="medium",
            action="Increase timeout and/or shorten prompts and expected output breadth.",
            reason="Timeouts are a significant share of calls.",
            evidence={
                "timeout_rows": timeout_rows,
                "share_pct": _ratio_pct(timeout_rows, max(1, total_rows)),
            },
        )

    if rate_limit_rows > 0:
        add(
            "runtime",
            code="runtime.rate_limit_backoff",
            priority="high",
            action="Lower worker concurrency and apply backoff before rerunning this pipeline.",
            reason="Rate-limit signals were detected in telemetry.",
            evidence={"rate_limit_rows": rate_limit_rows},
        )

    top_schema_paths = sorted(
        schema_path_counts.items(),
        key=lambda item: (-item[1], item[0]),
    )
    if top_schema_paths:
        top_path, top_path_count = top_schema_paths[0]
        add(
            "prompt",
            code="prompt.schema_path_guardrail",
            priority="medium",
            action=f"Add an explicit instruction for `{top_path}` shape and constraints in the prompt template.",
            reason="Schema validation signals cluster at one output path.",
            evidence={
                "path": top_path,
                "count": top_path_count,
                "share_pct": _ratio_pct(top_path_count, max(1, sum(schema_path_counts.values()))),
            },
        )

    if schema_issue_type_counts.get("required_property", 0) > 0:
        add(
            "output_schema",
            code="output_schema.required_vs_optional_review",
            priority="high",
            action=(
                "Review required properties: keep truly-required keys in `required`, "
                "and model optional fields as nullable required or relax schema intentionally."
            ),
            reason="Repeated missing-required-property validation failures detected.",
            evidence={"count": schema_issue_type_counts.get("required_property", 0)},
        )
        add(
            "input_data",
            code="input_data.prevalidate_required_inputs",
            priority="medium",
            action="Add pre-validation/normalization for source inputs that drive required output fields.",
            reason="Required-property schema failures often reflect missing upstream source facts.",
            evidence={"count": schema_issue_type_counts.get("required_property", 0)},
        )

    if schema_issue_type_counts.get("type_mismatch", 0) > 0:
        add(
            "output_schema",
            code="output_schema.type_mismatch_review",
            priority="medium",
            action="Consider nullable/union types where variation is expected, or tighten prompt typing instructions.",
            reason="Model output frequently violates expected JSON types.",
            evidence={"count": schema_issue_type_counts.get("type_mismatch", 0)},
        )
        add(
            "input_data",
            code="input_data.normalize_types",
            priority="medium",
            action="Normalize upstream numeric/string/null input variants before prompt rendering.",
            reason="Type mismatches suggest unstable source-value typing.",
            evidence={"count": schema_issue_type_counts.get("type_mismatch", 0)},
        )

    if schema_issue_type_counts.get("additional_properties", 0) > 0:
        add(
            "prompt",
            code="prompt.no_extra_keys_guardrail",
            priority="medium",
            action="Add explicit instruction: do not emit keys outside schema properties.",
            reason="Additional-properties validation failures detected.",
            evidence={"count": schema_issue_type_counts.get("additional_properties", 0)},
        )

    sorted_retry_patterns = sorted(
        retry_error_patterns.values(),
        key=lambda item: (-int(item.get("count", 0)), str(item.get("error_sha256", ""))),
    )
    if sorted_retry_patterns:
        top_retry = sorted_retry_patterns[0]
        retry_count = int(top_retry.get("count", 0))
        failed_retry = int(top_retry.get("failed", 0)) + int(top_retry.get("timeout", 0))
        if retry_count >= 2 and failed_retry >= 1:
            add(
                "prompt",
                code="prompt.promote_retry_context",
                priority="high",
                action=(
                    "Promote the top retry failure mode into baseline prompt guardrails "
                    "instead of only passing it via retry context."
                ),
                reason="The same failure pattern recurs across retries.",
                evidence={
                    "error_sha256": top_retry.get("error_sha256"),
                    "count": retry_count,
                    "failed_or_timeout": failed_retry,
                    "sample_error": top_retry.get("sample_error"),
                },
            )

    tip_rows = sorted(
        tip_effectiveness.values(),
        key=lambda item: (-int(item.get("uses", 0)), str(item.get("tip_id") or item.get("tip_text") or "")),
    )
    strong_tips = [
        row for row in tip_rows
        if int(row.get("uses", 0)) >= 2 and _ratio_pct(int(row.get("ok", 0)), int(row.get("uses", 0))) >= 80.0
    ]
    weak_tips = [
        row for row in tip_rows
        if int(row.get("uses", 0)) >= 2 and _ratio_pct(int(row.get("ok", 0)), int(row.get("uses", 0))) <= 40.0
    ]
    if strong_tips:
        add(
            "prompt",
            code="prompt.promote_high_win_heads_up_tips",
            priority="low",
            action="Promote high-success Heads Up tips into base prompt template for this pipeline.",
            reason="Some adaptive tips consistently correlate with successful outcomes.",
            evidence={
                "tip_count": len(strong_tips),
                "top_tip": strong_tips[0].get("tip_text") or strong_tips[0].get("tip_id"),
                "top_tip_success_rate_pct": _ratio_pct(
                    int(strong_tips[0].get("ok", 0)),
                    int(strong_tips[0].get("uses", 0)),
                ),
            },
        )
    if weak_tips:
        add(
            "prompt",
            code="prompt.revise_low_win_heads_up_tips",
            priority="medium",
            action="Review or disable low-success Heads Up tips to avoid injecting misleading guidance.",
            reason="Some adaptive tips correlate with poor outcomes.",
            evidence={
                "tip_count": len(weak_tips),
                "top_tip": weak_tips[0].get("tip_text") or weak_tips[0].get("tip_id"),
                "top_tip_success_rate_pct": _ratio_pct(
                    int(weak_tips[0].get("ok", 0)),
                    int(weak_tips[0].get("uses", 0)),
                ),
            },
        )

    if output_preview_truncated_rows >= max(2, int(total_rows * 0.2)):
        add(
            "output_schema",
            code="output_schema.reduce_output_breadth",
            priority="low",
            action="Consider tighter schema bounds to prevent oversized outputs and improve reliability.",
            reason="Many telemetry previews are truncated, indicating very large payloads.",
            evidence={
                "truncated_preview_rows": output_preview_truncated_rows,
                "share_pct": _ratio_pct(output_preview_truncated_rows, max(1, total_rows)),
            },
        )

    if terminal_error_count > 0 and non_ok_rows == 0:
        add(
            "runtime",
            code="runtime.cross_check_task_errors",
            priority="medium",
            action=(
                "Cross-check run terminal errors with telemetry rows; "
                "some failures can happen after Codex returns payload (for example local schema gate)."
            ),
            reason="Run contains terminal task errors not visible as failed Codex subprocess rows.",
            evidence={"terminal_error_count": terminal_error_count},
        )

    retry_rows_applied = _as_int(retry_effectiveness_summary.get("rows_applied")) or 0
    retry_delta = _as_float(retry_effectiveness_summary.get("delta_success_rate_pct"))
    if retry_rows_applied >= 2 and retry_delta <= -10.0:
        add(
            "prompt",
            code="prompt.retry_context_quality_review",
            priority="medium",
            action=(
                "Compress retry pass-forward context into concise bullet points with only root-cause facts; "
                "remove verbose error text that may distract generation."
            ),
            reason="Retries with pass-forward context correlate with lower success than retries without it.",
            evidence={
                "rows_applied": retry_rows_applied,
                "delta_success_rate_pct": retry_delta,
            },
        )

    heads_up_rows_applied = _as_int(heads_up_effectiveness_summary.get("rows_applied")) or 0
    heads_up_delta = _as_float(heads_up_effectiveness_summary.get("delta_success_rate_pct"))
    if heads_up_rows_applied >= 2 and heads_up_delta <= -10.0:
        add(
            "prompt",
            code="prompt.heads_up_selection_review",
            priority="medium",
            action=(
                "Tighten Heads Up selection rules for this pipeline; "
                "prefer fewer high-confidence tips over broad tip injection."
            ),
            reason="Rows with Heads Up tips show materially worse success in this sample.",
            evidence={
                "rows_applied": heads_up_rows_applied,
                "delta_success_rate_pct": heads_up_delta,
            },
        )

    model_shift = _best_model_shift_candidate(model_reasoning_breakdown)
    if model_shift is not None:
        add(
            "runtime",
            code="runtime.model_effort_shift",
            priority="medium",
            action=(
                "Prefer "
                f"{model_shift['best_model']}@{model_shift['best_reasoning_effort']} "
                "for this pipeline until prompt/schema issues are reduced."
            ),
            reason="Observed success-rate gap between model/effort configurations is large.",
            evidence=model_shift,
        )

    if rows_without_turn_completed >= max(2, int(max(1, non_ok_rows) * 0.5)):
        add(
            "runtime",
            code="runtime.codex_turn_completion_gap",
            priority="medium",
            action=(
                "Investigate Codex transport/provider stability: many calls fail before `turn.completed` "
                "events are observed."
            ),
            reason="A large share of rows never reached turn completion events.",
            evidence={
                "rows_without_turn_completed": rows_without_turn_completed,
                "total_rows": total_rows,
                "share_pct": _ratio_pct(rows_without_turn_completed, max(1, total_rows)),
            },
        )

    if rows_with_no_events >= max(2, int(total_rows * 0.2)):
        add(
            "runtime",
            code="runtime.codex_event_stream_gap",
            priority="low",
            action=(
                "Capture full stderr/stdout for a sample run and verify Codex event streaming; "
                "empty event streams reduce telemetry quality and diagnostics."
            ),
            reason="Many rows have zero parsed Codex events.",
            evidence={
                "rows_with_no_events": rows_with_no_events,
                "share_pct": _ratio_pct(rows_with_no_events, max(1, total_rows)),
            },
        )

    return recommendations


def _build_tuning_playbook(
    *,
    total_rows: int,
    failure_category_counts: dict[str, int],
    schema_path_counts: dict[str, int],
    schema_issue_type_counts: dict[str, int],
    rate_limit_rows: int,
    timeout_rows: int,
    output_preview_truncated_rows: int,
    retry_effectiveness_summary: dict[str, object],
    heads_up_effectiveness_summary: dict[str, object],
    model_reasoning_breakdown: list[dict[str, object]],
) -> dict[str, list[dict[str, object]]]:
    playbook: dict[str, list[dict[str, object]]] = {
        "prompt_edits": [],
        "input_prechecks": [],
        "schema_edits": [],
        "runtime_tuning": [],
        "model_tuning": [],
    }
    seen_ids: set[str] = set()

    def add(
        section: str,
        *,
        item_id: str,
        priority: str,
        target: str,
        change: str,
        trigger: str,
        evidence: dict[str, object],
    ) -> None:
        if section not in playbook:
            return
        if item_id in seen_ids:
            return
        if len(playbook[section]) >= 20:
            return
        seen_ids.add(item_id)
        playbook[section].append(
            {
                "id": item_id,
                "priority": priority,
                "target": target,
                "change": change,
                "trigger": trigger,
                "evidence": evidence,
            }
        )

    missing_payload_failures = (
        failure_category_counts.get("nonzero_exit_no_payload", 0)
        + failure_category_counts.get("zero_exit_no_payload", 0)
    )
    if missing_payload_failures > 0:
        add(
            "prompt_edits",
            item_id="prompt.raw_json_only_footer",
            priority="high",
            target="prompt_template_tail",
            change=(
                "Append: 'Return only JSON matching the schema. "
                "No markdown, prose, or code fences.'"
            ),
            trigger="missing_payload_failures > 0",
            evidence={
                "missing_payload_failures": missing_payload_failures,
                "share_pct": _ratio_pct(missing_payload_failures, max(1, total_rows)),
            },
        )

    top_schema_paths = sorted(schema_path_counts.items(), key=lambda item: (-item[1], item[0]))
    if top_schema_paths:
        top_path = top_schema_paths[0][0]
        add(
            "prompt_edits",
            item_id="prompt.schema_path_contract",
            priority="medium",
            target="prompt_template_body",
            change=f"Add an explicit shape rule for `{top_path}` with allowed keys and value types.",
            trigger="schema_path_hotspot_detected",
            evidence={
                "top_schema_paths": [
                    {"path": path, "count": count}
                    for path, count in top_schema_paths[:3]
                ]
            },
        )

    if schema_issue_type_counts.get("additional_properties", 0) > 0:
        add(
            "prompt_edits",
            item_id="prompt.no_extra_keys",
            priority="medium",
            target="prompt_template_tail",
            change="Append: 'Do not emit keys that are not defined in the schema.'",
            trigger="additional_properties_failures > 0",
            evidence={"count": schema_issue_type_counts.get("additional_properties", 0)},
        )

    if schema_issue_type_counts.get("required_property", 0) > 0:
        add(
            "input_prechecks",
            item_id="input.required_facts_precheck",
            priority="high",
            target="caller_input_validation",
            change=(
                "Pre-validate source payloads for required source facts before prompt rendering; "
                "skip/flag records that cannot satisfy required output keys."
            ),
            trigger="required_property_failures > 0",
            evidence={
                "required_property_failures": schema_issue_type_counts.get("required_property", 0),
                "top_schema_paths": [path for path, _count in top_schema_paths[:3]],
            },
        )
        add(
            "schema_edits",
            item_id="schema.required_optional_review",
            priority="high",
            target="output_schema.required",
            change=(
                "Review strict required keys; convert conditionally-present fields to nullable required fields "
                "or optional fields where appropriate."
            ),
            trigger="required_property_failures > 0",
            evidence={"required_property_failures": schema_issue_type_counts.get("required_property", 0)},
        )

    if schema_issue_type_counts.get("type_mismatch", 0) > 0:
        add(
            "input_prechecks",
            item_id="input.normalize_types",
            priority="medium",
            target="caller_input_normalization",
            change="Normalize numeric/string/null variants before prompt rendering.",
            trigger="type_mismatch_failures > 0",
            evidence={"type_mismatch_failures": schema_issue_type_counts.get("type_mismatch", 0)},
        )
        add(
            "schema_edits",
            item_id="schema.type_union_review",
            priority="medium",
            target="output_schema.types",
            change="Use explicit nullable/union types where value-shape variance is expected.",
            trigger="type_mismatch_failures > 0",
            evidence={"type_mismatch_failures": schema_issue_type_counts.get("type_mismatch", 0)},
        )

    if timeout_rows >= max(1, int(total_rows * 0.2)):
        add(
            "runtime_tuning",
            item_id="runtime.timeout_increase",
            priority="medium",
            target="codex_timeout_seconds",
            change="Increase timeout by ~1.5x and reduce expected response breadth.",
            trigger="timeout_share >= 20%",
            evidence={
                "timeout_rows": timeout_rows,
                "share_pct": _ratio_pct(timeout_rows, max(1, total_rows)),
            },
        )

    if rate_limit_rows > 0:
        add(
            "runtime_tuning",
            item_id="runtime.rate_limit_backoff",
            priority="high",
            target="worker_concurrency/backoff",
            change="Reduce worker count (for example, 50%) and introduce exponential backoff between retries.",
            trigger="rate_limit_rows > 0",
            evidence={"rate_limit_rows": rate_limit_rows},
        )

    if output_preview_truncated_rows >= max(2, int(total_rows * 0.2)):
        add(
            "schema_edits",
            item_id="schema.bound_large_outputs",
            priority="low",
            target="output_schema.maxItems/maxLength",
            change="Add tighter bounds to large arrays/strings to reduce oversized payload generation.",
            trigger="truncated_preview_share >= 20%",
            evidence={
                "truncated_preview_rows": output_preview_truncated_rows,
                "share_pct": _ratio_pct(output_preview_truncated_rows, max(1, total_rows)),
            },
        )

    retry_rows_applied = _as_int(retry_effectiveness_summary.get("rows_applied")) or 0
    retry_delta = _as_float(retry_effectiveness_summary.get("delta_success_rate_pct"))
    if retry_rows_applied >= 2 and retry_delta <= -10.0:
        add(
            "prompt_edits",
            item_id="prompt.retry_context_compact",
            priority="medium",
            target="retry_pass_forward_template",
            change=(
                "Shrink retry pass-forward text to a compact 'Error cause + exact fix hint' block, "
                "dropping long stack/context snippets."
            ),
            trigger="retry_context_success_delta <= -10%",
            evidence=retry_effectiveness_summary,
        )

    heads_up_rows_applied = _as_int(heads_up_effectiveness_summary.get("rows_applied")) or 0
    heads_up_delta = _as_float(heads_up_effectiveness_summary.get("delta_success_rate_pct"))
    if heads_up_rows_applied >= 2 and heads_up_delta <= -10.0:
        add(
            "prompt_edits",
            item_id="prompt.heads_up_stricter_selection",
            priority="medium",
            target="heads_up_tip_selector",
            change="Lower tip count and keep only highest-score Heads Up tips per input signature.",
            trigger="heads_up_success_delta <= -10%",
            evidence=heads_up_effectiveness_summary,
        )

    model_shift = _best_model_shift_candidate(model_reasoning_breakdown)
    if model_shift is not None:
        add(
            "model_tuning",
            item_id="model.prefer_high_success_config",
            priority="medium",
            target="codex_model/codex_reasoning_effort",
            change=(
                "Prefer "
                f"{model_shift['best_model']}@{model_shift['best_reasoning_effort']} "
                "for this pipeline."
            ),
            trigger="success_gap_between_model_configs >= 20%",
            evidence=model_shift,
        )

    return playbook


def _best_model_shift_candidate(
    model_reasoning_breakdown: list[dict[str, object]],
) -> dict[str, object] | None:
    candidates = [
        row for row in model_reasoning_breakdown
        if (_as_int(row.get("calls")) or 0) >= 2
    ]
    if len(candidates) < 2:
        return None

    best = max(
        candidates,
        key=lambda row: (
            _as_float(row.get("success_rate_pct")),
            _as_int(row.get("calls")) or 0,
            _clean_text(row.get("model")),
            _clean_text(row.get("reasoning_effort")),
        ),
    )
    worst = min(
        candidates,
        key=lambda row: (
            _as_float(row.get("success_rate_pct")),
            -(_as_int(row.get("calls")) or 0),
            _clean_text(row.get("model")),
            _clean_text(row.get("reasoning_effort")),
        ),
    )

    best_success = _as_float(best.get("success_rate_pct"))
    worst_success = _as_float(worst.get("success_rate_pct"))
    delta_success = round(best_success - worst_success, 1)
    if delta_success < 20.0:
        return None

    return {
        "best_model": _clean_text(best.get("model")),
        "best_reasoning_effort": _clean_text(best.get("reasoning_effort")),
        "best_success_rate_pct": best_success,
        "best_calls": _as_int(best.get("calls")) or 0,
        "worst_model": _clean_text(worst.get("model")),
        "worst_reasoning_effort": _clean_text(worst.get("reasoning_effort")),
        "worst_success_rate_pct": worst_success,
        "worst_calls": _as_int(worst.get("calls")) or 0,
        "delta_success_rate_pct": delta_success,
    }


def _new_effectiveness_counter() -> dict[str, int]:
    return {"rows": 0, "ok": 0, "non_ok": 0}


def _bump_effectiveness_counter(counter: dict[str, int], row_status: str) -> None:
    counter["rows"] = int(counter.get("rows", 0)) + 1
    if row_status == "ok":
        counter["ok"] = int(counter.get("ok", 0)) + 1
    else:
        counter["non_ok"] = int(counter.get("non_ok", 0)) + 1


def _effectiveness_summary(
    *,
    applied: dict[str, int],
    control: dict[str, int],
) -> dict[str, object]:
    rows_applied = int(applied.get("rows", 0))
    rows_control = int(control.get("rows", 0))
    success_applied_pct = _ratio_pct(int(applied.get("ok", 0)), rows_applied)
    success_control_pct = _ratio_pct(int(control.get("ok", 0)), rows_control)
    return {
        "rows_applied": rows_applied,
        "rows_control": rows_control,
        "success_rate_applied_pct": success_applied_pct,
        "success_rate_control_pct": success_control_pct,
        "delta_success_rate_pct": round(success_applied_pct - success_control_pct, 1),
    }


def _sorted_model_reasoning_breakdown(
    model_reasoning_stats: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for row in model_reasoning_stats.values():
        calls = int(row.get("calls", 0))
        ok = int(row.get("ok", 0))
        failed = int(row.get("failed", 0))
        timeout = int(row.get("timeout", 0))
        other = int(row.get("other", 0))
        duration_total_ms = int(row.get("_duration_total_ms", 0))
        tokens_total = int(row.get("_tokens_total", 0))
        tokens_reasoning_total = int(row.get("_tokens_reasoning_total", 0))
        rows.append(
            {
                "model": row.get("model"),
                "reasoning_effort": row.get("reasoning_effort"),
                "calls": calls,
                "ok": ok,
                "failed": failed,
                "timeout": timeout,
                "other": other,
                "success_rate_pct": _ratio_pct(ok, calls),
                "failure_rate_pct": _ratio_pct(failed + timeout + other, calls),
                "duration_avg_ms": int(round(duration_total_ms / calls)) if calls else 0,
                "tokens_avg_per_call": int(round(tokens_total / calls)) if calls else 0,
                "tokens_reasoning_avg_per_call": (
                    int(round(tokens_reasoning_total / calls)) if calls else 0
                ),
                "output_missing_rows": int(row.get("output_missing_rows", 0)),
                "retry_context_rows": int(row.get("retry_context_rows", 0)),
                "heads_up_rows": int(row.get("heads_up_rows", 0)),
            }
        )

    rows.sort(
        key=lambda row: (
            -int(row.get("calls", 0)),
            -_as_float(row.get("success_rate_pct")),
            _clean_text(row.get("model")),
            _clean_text(row.get("reasoning_effort")),
        )
    )
    return rows[:_MAX_BREAKDOWN_ROWS]


def _sorted_prompt_fingerprint_breakdown(
    prompt_fingerprint_stats: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for row in prompt_fingerprint_stats.values():
        calls = int(row.get("calls", 0))
        ok = int(row.get("ok", 0))
        failed = int(row.get("failed", 0))
        timeout = int(row.get("timeout", 0))
        other = int(row.get("other", 0))
        rows.append(
            {
                "prompt_sha256": row.get("prompt_sha256"),
                "sample_prompt": row.get("sample_prompt"),
                "calls": calls,
                "ok": ok,
                "failed": failed,
                "timeout": timeout,
                "other": other,
                "success_rate_pct": _ratio_pct(ok, calls),
                "output_missing_rows": int(row.get("output_missing_rows", 0)),
            }
        )
    rows.sort(
        key=lambda row: (
            -int(row.get("calls", 0)),
            -_as_float(row.get("success_rate_pct")),
            _clean_text(row.get("prompt_sha256")),
        )
    )
    return rows[:_MAX_BREAKDOWN_ROWS]


def _sorted_input_failure_hotspots(
    input_failure_hotspots: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for row in input_failure_hotspots.values():
        categories = row.get("_failure_categories")
        category_counts = categories if isinstance(categories, dict) else {}
        top_category = ""
        top_category_count = 0
        for category, count in category_counts.items():
            count_value = int(count)
            if count_value > top_category_count or (
                count_value == top_category_count and category < top_category
            ):
                top_category = str(category)
                top_category_count = count_value
        rows.append(
            {
                "input_path": row.get("input_path"),
                "non_ok_rows": int(row.get("non_ok_rows", 0)),
                "failed": int(row.get("failed", 0)),
                "timeout": int(row.get("timeout", 0)),
                "other": int(row.get("other", 0)),
                "top_failure_category": top_category,
                "top_failure_category_count": top_category_count,
                "sample_error": row.get("sample_error"),
            }
        )
    rows.sort(
        key=lambda row: (
            -int(row.get("non_ok_rows", 0)),
            _clean_text(row.get("input_path")),
        )
    )
    return rows[:_MAX_BREAKDOWN_ROWS]


def _sorted_count_rows(
    counts: dict[str, int],
    *,
    key_name: str,
    total: int,
    limit: int | None = None,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for label, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        rows.append(
            {
                key_name: label,
                "count": count,
                "share_pct": _ratio_pct(count, total),
            }
        )
    if limit is None:
        return rows
    return rows[: max(0, int(limit))]


def _sorted_retry_patterns(retry_error_patterns: dict[str, dict[str, object]]) -> list[dict[str, object]]:
    rows = sorted(
        retry_error_patterns.values(),
        key=lambda item: (-int(item.get("count", 0)), str(item.get("error_sha256", ""))),
    )
    return [
        {
            "error_sha256": row.get("error_sha256"),
            "sample_error": row.get("sample_error"),
            "count": int(row.get("count", 0)),
            "ok": int(row.get("ok", 0)),
            "failed": int(row.get("failed", 0)),
            "timeout": int(row.get("timeout", 0)),
            "other": int(row.get("other", 0)),
        }
        for row in rows
    ]


def _sorted_tip_effectiveness(tip_effectiveness: dict[str, dict[str, object]]) -> list[dict[str, object]]:
    rows = sorted(
        tip_effectiveness.values(),
        key=lambda item: (-int(item.get("uses", 0)), str(item.get("tip_id") or item.get("tip_text") or "")),
    )
    serialized: list[dict[str, object]] = []
    for row in rows:
        uses = int(row.get("uses", 0))
        score_count = int(row.get("_score_count", 0))
        score_avg_total = float(row.get("score_avg", 0.0))
        serialized.append(
            {
                "tip_id": row.get("tip_id"),
                "tip_text": row.get("tip_text"),
                "uses": uses,
                "ok": int(row.get("ok", 0)),
                "failed": int(row.get("failed", 0)),
                "timeout": int(row.get("timeout", 0)),
                "other": int(row.get("other", 0)),
                "success_rate_pct": _ratio_pct(int(row.get("ok", 0)), uses),
                "score_avg": round(score_avg_total / score_count, 4) if score_count else None,
            }
        )
    return serialized


def _recent_rows(rows: list[dict[str, str]], *, max_rows: int) -> list[dict[str, object]]:
    recent: list[dict[str, object]] = []
    for row in rows[:max(1, max_rows)]:
        recent.append(
            {
                "logged_at_utc": _clean_text(row.get("logged_at_utc")),
                "status": _normalized_status(row.get("status", "")),
                "failure_category": _clean_text(row.get("failure_category")),
                "source": _clean_text(row.get("source")),
                "pipeline_id": _clean_text(row.get("pipeline_id")),
                "run_id": _clean_text(row.get("run_id")),
                "task_id": _clean_text(row.get("task_id")),
                "model": _clean_text(row.get("model")),
                "reasoning_effort": _clean_text(row.get("reasoning_effort")),
                "duration_ms": _as_int(row.get("duration_ms")) or 0,
                "tokens_total": _as_int(row.get("tokens_total")) or 0,
                "tokens_reasoning": _as_int(row.get("tokens_reasoning")) or 0,
                "attempt_index": _as_int(row.get("attempt_index")),
                "retry_context_applied": _as_bool(row.get("retry_context_applied")),
                "heads_up_applied": _as_bool(row.get("heads_up_applied")),
                "heads_up_tip_count": _as_int(row.get("heads_up_tip_count")) or 0,
                "rate_limit_suspected": _as_bool(row.get("rate_limit_suspected")),
                "prompt_sha256": _clean_text(row.get("prompt_sha256")),
                "output_sha256": _clean_text(row.get("output_sha256")),
                "stderr_tail": _trim_text(_clean_text(row.get("stderr_tail")), 180),
            }
        )
    return recent


def _json_list(value: object) -> list[str]:
    if not isinstance(value, str):
        return []
    text = value.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    result: list[str] = []
    for item in parsed:
        if item is None:
            result.append("")
        else:
            result.append(str(item).strip())
    return result


def _json_float_list(value: object) -> list[float]:
    result: list[float] = []
    for item in _json_list(value):
        if not item:
            continue
        try:
            result.append(float(item))
        except ValueError:
            continue
    return result


def _normalized_status(value: object) -> str:
    lowered = _clean_text(value).lower()
    if lowered in {"ok", "failed", "timeout"}:
        return lowered
    return "other"


def _row_output_payload_present(row: dict[str, str]) -> bool:
    """Infer payload presence with backward compatibility for legacy telemetry rows."""
    raw_flag = _clean_text(row.get("output_payload_present"))
    if raw_flag:
        return _as_bool(raw_flag)

    output_bytes = _as_int(row.get("output_bytes")) or 0
    if output_bytes > 0:
        return True

    status = _normalized_status(row.get("status", ""))
    if status == "ok":
        failure_category = _clean_text(row.get("failure_category"))
        # Legacy rows may omit explicit payload flags; successful calls usually had payload.
        return failure_category not in {"nonzero_exit_no_payload", "zero_exit_no_payload", "timeout"}
    return False


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        return str(value).strip()
    return value.strip()


def _as_int(value: object) -> int | None:
    text = _clean_text(value)
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _as_float(value: object) -> float:
    text = _clean_text(value)
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _as_bool(value: object) -> bool:
    text = _clean_text(value).lower()
    return text in {"1", "true", "yes", "y"}


def _avg_int(values: list[int]) -> int:
    if not values:
        return 0
    return int(round(sum(values) / len(values)))


def _percentile(values: list[int], percentile: int) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, int(round((percentile / 100) * len(ordered) + 0.5)) - 1)
    return ordered[min(rank, len(ordered) - 1)]


def _ratio_pct(part: int, whole: int) -> float:
    if whole <= 0:
        return 0.0
    return round((part / whole) * 100, 1)


def _trim_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    normalized = value
    if value.endswith("Z"):
        normalized = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except ValueError:
        return None


def _utc_ts(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
