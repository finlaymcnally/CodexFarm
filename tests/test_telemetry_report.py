import json

from codex_farm.telemetry_report import build_telemetry_report, read_telemetry_rows


def _row(**overrides: str) -> dict[str, str]:
    row = {
        "logged_at_utc": "2026-02-28T15:10:00.000Z",
        "status": "ok",
        "run_id": "run-1",
        "pipeline_id": "demo.pipeline.v1",
        "source": "worker",
        "duration_ms": "1000",
        "tokens_total": "100",
        "tokens_reasoning": "25",
        "failure_category": "",
        "retry_context_applied": "false",
        "retry_previous_error": "",
        "retry_previous_error_sha256": "",
        "heads_up_applied": "false",
        "heads_up_tip_count": "0",
        "heads_up_tip_ids_json": "[]",
        "heads_up_tip_texts_json": "[]",
        "heads_up_tip_scores_json": "[]",
        "rate_limit_suspected": "false",
        "accepted_nonzero_exit": "false",
        "output_payload_present": "true",
        "output_preview_truncated": "false",
        "attempt_index": "1",
        "stderr_tail": "",
        "prompt_sha256": "prompt-a",
        "prompt_text": "Return JSON only.",
        "output_sha256": "output-a",
        "model": "gpt-5.3-codex-spark",
        "reasoning_effort": "medium",
        "input_path": "/tmp/in/a.json",
        "codex_event_count": "2",
        "codex_event_types_json": json.dumps(["thread.started", "turn.completed"]),
    }
    row.update(overrides)
    return row


def test_read_telemetry_rows_missing_path_returns_warning(tmp_path) -> None:
    rows, warnings = read_telemetry_rows(tmp_path / "missing.csv")
    assert rows == []
    assert warnings
    assert "does not exist" in warnings[0]


def test_build_telemetry_report_summarizes_patterns_and_recommendations() -> None:
    repeated_retry_error = (
        "Schema validation failed at recipeInstructions[0].text: "
        "'text' is a required property"
    )
    rows = [
        _row(
            status="failed",
            failure_category="nonzero_exit_no_payload",
            retry_context_applied="true",
            retry_previous_error=repeated_retry_error,
            retry_previous_error_sha256="retry-hash-1",
            rate_limit_suspected="true",
            output_payload_present="false",
            logged_at_utc="2026-02-28T15:12:00.000Z",
        ),
        _row(
            status="failed",
            failure_category="nonzero_exit_no_payload",
            retry_context_applied="true",
            retry_previous_error=repeated_retry_error,
            retry_previous_error_sha256="retry-hash-1",
            output_payload_present="false",
            logged_at_utc="2026-02-28T15:11:00.000Z",
        ),
        _row(
            status="ok",
            heads_up_applied="true",
            heads_up_tip_count="1",
            heads_up_tip_ids_json=json.dumps(["tip-1"]),
            heads_up_tip_texts_json=json.dumps(["Return raw JSON only."]),
            heads_up_tip_scores_json=json.dumps([0.9]),
            logged_at_utc="2026-02-28T15:10:00.000Z",
        ),
        _row(
            status="timeout",
            logged_at_utc="2026-02-28T15:09:00.000Z",
        ),
    ]

    report = build_telemetry_report(
        rows,
        run_id="run-1",
        limit=100,
        terminal_errors=[
            "Schema validation failed at <root>: Additional properties are not allowed",
        ],
        warnings=["synthetic warning"],
    )

    assert report["warnings"] == ["synthetic warning"]
    assert report["matched_rows"] == 4
    assert report["summary"]["status_counts"]["failed"] == 2
    assert report["summary"]["status_counts"]["timeout"] == 1
    assert report["summary"]["status_counts"]["ok"] == 1
    assert report["summary"]["rate_limit_suspected_rows"] == 1
    assert report["summary"]["retry_context_rows"] == 2
    assert report["summary"]["output_missing_rows"] == 2
    assert report["summary"]["tokens_reasoning_total"] == 100
    assert report["failure_patterns"]["schema_paths"][0]["path"] == "recipeInstructions[0].text"
    assert report["terminal_errors"]["count"] == 1

    recommendation_codes = {
        row["code"]
        for category in report["recommendations"].values()
        for row in category
    }
    assert "prompt.raw_json_only_guardrail" in recommendation_codes
    assert "runtime.rate_limit_backoff" in recommendation_codes
    assert "output_schema.required_vs_optional_review" in recommendation_codes
    assert "prompt.promote_retry_context" in recommendation_codes


def test_build_telemetry_report_treats_legacy_ok_rows_without_payload_flag_as_present() -> None:
    legacy_ok = _row(status="ok")
    legacy_ok.pop("output_payload_present", None)
    legacy_ok["output_bytes"] = ""

    report = build_telemetry_report([legacy_ok], run_id="run-1", limit=10)

    assert report["summary"]["output_missing_rows"] == 0
    runtime_codes = {row["code"] for row in report["recommendations"]["runtime"]}
    assert "runtime.detect_output_write_failures" not in runtime_codes


def test_build_telemetry_report_emits_insights_and_tuning_playbook() -> None:
    rows = [
        _row(
            status="failed",
            model="gpt-5.3-codex-spark",
            reasoning_effort="high",
            retry_context_applied="true",
            heads_up_applied="true",
            input_path="/tmp/in/fail-a.json",
            failure_category="nonzero_exit_no_payload",
            output_payload_present="false",
            codex_event_count="1",
            codex_event_types_json=json.dumps(["thread.started"]),
            logged_at_utc="2026-02-28T15:12:00.000Z",
        ),
        _row(
            status="failed",
            model="gpt-5.3-codex-spark",
            reasoning_effort="high",
            retry_context_applied="true",
            heads_up_applied="true",
            input_path="/tmp/in/fail-a.json",
            failure_category="nonzero_exit_no_payload",
            output_payload_present="false",
            codex_event_count="0",
            codex_event_types_json="[]",
            logged_at_utc="2026-02-28T15:11:00.000Z",
        ),
        _row(
            status="ok",
            model="gpt-5.3-codex-mini",
            reasoning_effort="low",
            retry_context_applied="false",
            heads_up_applied="false",
            input_path="/tmp/in/ok-a.json",
            logged_at_utc="2026-02-28T15:10:00.000Z",
        ),
        _row(
            status="ok",
            model="gpt-5.3-codex-mini",
            reasoning_effort="low",
            retry_context_applied="false",
            heads_up_applied="false",
            input_path="/tmp/in/ok-b.json",
            logged_at_utc="2026-02-28T15:09:00.000Z",
        ),
    ]

    report = build_telemetry_report(rows, run_id="run-1", limit=20)
    assert report["schema_version"] == 2

    insights = report["insights"]
    assert insights["reasoning_signals"]["rows_without_turn_completed"] >= 2
    assert insights["reasoning_signals"]["rows_with_no_events"] >= 1
    assert insights["input_failure_hotspots"][0]["input_path"] == "/tmp/in/fail-a.json"
    assert any(
        row["model"] == "gpt-5.3-codex-mini" and row["reasoning_effort"] == "low"
        for row in insights["model_reasoning_breakdown"]
    )
    mini_row = next(
        row
        for row in insights["model_reasoning_breakdown"]
        if row["model"] == "gpt-5.3-codex-mini" and row["reasoning_effort"] == "low"
    )
    assert mini_row["tokens_reasoning_avg_per_call"] == 25

    recommendation_codes = {
        row["code"]
        for category in report["recommendations"].values()
        for row in category
    }
    assert "runtime.model_effort_shift" in recommendation_codes
    assert "prompt.retry_context_quality_review" in recommendation_codes
    assert "runtime.codex_turn_completion_gap" in recommendation_codes

    playbook_ids = {
        row["id"]
        for section in report["tuning_playbook"].values()
        for row in section
    }
    assert "model.prefer_high_success_config" in playbook_ids
    assert "prompt.retry_context_compact" in playbook_ids
