import csv
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from codex_farm.codex_exec import (
    CodexExecTimeoutError,
    _USAGE_LOG_FIELDS,
    _persist_trace_artifact,
    extract_retry_after_seconds,
    is_auth_failure_message,
    run_codex_exec,
)


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


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


def test_run_codex_exec_migrates_legacy_usage_log_schema(monkeypatch, tmp_path: Path) -> None:
    output_path = tmp_path / "out.json"
    schema_path = tmp_path / "schema.json"
    log_path = tmp_path / "codex_exec_activity.csv"
    schema_path.write_text("{}", encoding="utf-8")

    legacy_row = {field: "" for field in _LEGACY_USAGE_LOG_FIELDS}
    legacy_row.update(
        {
            "status": "ok",
            "tokens_input": "10",
            "tokens_output": "5",
            "tokens_total": "",
            "usage_json": json.dumps({"output_tokens_details": {"reasoning_tokens": 11}}),
            "prompt_text": "legacy row",
            "output_payload_present": "true",
            "output_bytes": "123",
        }
    )

    stale_header_row = {field: "" for field in _USAGE_LOG_FIELDS}
    stale_header_row.update(
        {
            "status": "ok",
            "model": "gpt-5.3-codex",
            "reasoning_effort": "high",
            "tokens_input": "20",
            "tokens_output": "9",
            "tokens_reasoning": "7",
            "tokens_total": "29",
            "usage_json": json.dumps({"output_tokens_details": {"reasoning_tokens": 7}}),
            "output_payload_present": "true",
            "output_bytes": "456",
            "prompt_text": "stale header row",
        }
    )
    legacy_with_reasoning_fields = (
        _LEGACY_USAGE_LOG_FIELDS[:12]
        + ("reasoning_effort",)
        + _LEGACY_USAGE_LOG_FIELDS[12:]
    )
    legacy_with_reasoning_row = {field: "" for field in legacy_with_reasoning_fields}
    legacy_with_reasoning_row.update(
        {
            "status": "ok",
            "reasoning_effort": "low",
            "cd_dir": "/tmp/v2",
            "tokens_input": "8",
            "tokens_output": "2",
            "tokens_total": "10",
            "prompt_sha256": "sha-v2",
            "prompt_text": "legacy with reasoning",
            "input_path": "/tmp/v2/input.json",
        }
    )
    no_thread_fields = tuple(field for field in _USAGE_LOG_FIELDS if field != "thread_id")
    no_thread_row = {field: "" for field in no_thread_fields}
    no_thread_row.update(
        {
            "status": "ok",
            "model": "gpt-5.3-codex-spark",
            "reasoning_effort": "medium",
            "tokens_input": "40",
            "tokens_output": "10",
            "tokens_reasoning": "13",
            "tokens_total": "50",
            "prompt_sha256": "sha-no-thread",
            "prompt_text": "no-thread row",
            "input_path": "/tmp/no-thread/input.json",
            "codex_event_count": "2",
            "codex_event_types_json": json.dumps(["thread.started", "turn.completed"]),
        }
    )

    with log_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(_LEGACY_USAGE_LOG_FIELDS)
        writer.writerow([legacy_row[field] for field in _LEGACY_USAGE_LOG_FIELDS])
        writer.writerow([legacy_with_reasoning_row[field] for field in legacy_with_reasoning_fields])
        writer.writerow([no_thread_row[field] for field in no_thread_fields])
        writer.writerow([stale_header_row[field] for field in _USAGE_LOG_FIELDS])

    def fake_run(cmd, **kwargs):
        temp_output = Path(cmd[cmd.index("--output-last-message") + 1])
        temp_output.write_text('{"ok":"OK"}', encoding="utf-8")
        stdout = (
            '{"type":"turn.completed","usage":{"input_tokens":120,"cached_input_tokens":10,'
            '"output_tokens":30,"output_tokens_details":{"reasoning_tokens":19}}}'
        )
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    run_codex_exec(
        cd_dir=tmp_path,
        prompt="Return JSON.",
        model="gpt-5.3-codex-spark",
        sandbox="read-only",
        ask_for_approval="never",
        web_search="disabled",
        output_schema=schema_path,
        output_path=output_path,
        timeout_seconds=30,
        usage_log_csv=log_path,
    )

    with log_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames is not None
        assert "tokens_reasoning" in reader.fieldnames
        assert "reasoning_effort" in reader.fieldnames
        rows = list(reader)

    assert len(rows) == 5
    assert rows[0]["tokens_reasoning"] == "11"
    assert rows[0]["tokens_total"] == "15"
    assert rows[1]["reasoning_effort"] == "low"
    assert rows[1]["prompt_sha256"] == "sha-v2"
    assert rows[1]["tokens_reasoning"] == ""
    assert rows[2]["reasoning_effort"] == "medium"
    assert rows[2]["prompt_sha256"] == "sha-no-thread"
    assert rows[2]["tokens_reasoning"] == "13"
    assert rows[3]["reasoning_effort"] == "high"
    assert rows[3]["tokens_reasoning"] == "7"
    assert rows[4]["tokens_reasoning"] == "19"

    backups = list(log_path.parent.glob("codex_exec_activity.legacy-*.csv"))
    assert backups


def test_run_codex_exec_logs_usage_from_jsonl_stdout(monkeypatch, tmp_path: Path) -> None:
    output_path = tmp_path / "out.json"
    schema_path = tmp_path / "schema.json"
    log_path = tmp_path / "codex_exec_activity.csv"
    schema_path.write_text("{}", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        assert "--json" in cmd
        temp_output = Path(cmd[cmd.index("--output-last-message") + 1])
        temp_output.write_text('{"ok":"OK"}', encoding="utf-8")
        stdout = "\n".join(
            [
                '{"type":"thread.started","thread_id":"thread-123"}',
                "non-json-warning line",
                (
                    '{"type":"turn.completed","usage":{"input_tokens":120,'
                    '"cached_input_tokens":10,"output_tokens":30,'
                    '"output_tokens_details":{"reasoning_tokens":19}}}'
                ),
            ]
        )
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_codex_exec(
        cd_dir=tmp_path,
        prompt="INPUT=/tmp/input.json\nReturn JSON.",
        model="gpt-5.3-codex-spark",
        sandbox="read-only",
        ask_for_approval="never",
        web_search="disabled",
        output_schema=schema_path,
        output_path=output_path,
        timeout_seconds=30,
        usage_log_csv=log_path,
        usage_context={
            "source": "worker",
            "pipeline_id": "demo.echo.v1",
            "run_id": "run-1",
            "task_id": "task-1",
            "worker_id": "worker-1",
            "input_path": "/tmp/input.json",
            "heads_up_applied": True,
            "heads_up_tip_count": 2,
            "heads_up_input_signature": "json_obj_keys:name,recipeIngredient",
            "heads_up_tip_ids_json": ["tip-1", "tip-2"],
            "heads_up_tip_texts_json": [
                "Return raw JSON only.",
                "Use HowToStep objects for recipeInstructions.",
            ],
            "heads_up_tip_scores_json": [0.8, 0.6],
            "attempt_index": 2,
            "lease_claim_index": 2,
            "execution_attempt_index": 2,
            "retry_context_applied": True,
            "retry_previous_error": "Schema validation failed at recipeInstructions[0].text",
        },
    )

    assert result.ok is True
    assert output_path.exists()

    rows = _read_rows(log_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "ok"
    assert row["exit_code"] == "0"
    assert row["tokens_input"] == "120"
    assert row["tokens_cached_input"] == "10"
    assert row["tokens_output"] == "30"
    assert row["tokens_reasoning"] == "19"
    assert row["tokens_total"] == "150"
    assert row["thread_id"] == "thread-123"
    assert row["source"] == "worker"
    assert row["pipeline_id"] == "demo.echo.v1"
    assert row["run_id"] == "run-1"
    assert row["task_id"] == "task-1"
    assert row["worker_id"] == "worker-1"
    assert row["input_path"] == "/tmp/input.json"
    assert row["prompt_text"] == "INPUT=/tmp/input.json\nReturn JSON."
    assert row["stdout_tail"] == "non-json-warning line"
    assert row["output_sha256"] == hashlib.sha256(b'{"ok":"OK"}').hexdigest()
    assert row["output_preview"] == '{"ok":"OK"}'
    assert row["output_preview_truncated"] == "false"
    assert row["codex_event_count"] == "2"
    assert json.loads(row["codex_event_types_json"]) == ["thread.started", "turn.completed"]
    assert row["heads_up_tip_count"] == "2"
    assert json.loads(row["heads_up_tip_ids_json"]) == ["tip-1", "tip-2"]
    assert json.loads(row["heads_up_tip_texts_json"]) == [
        "Return raw JSON only.",
        "Use HowToStep objects for recipeInstructions.",
    ]
    assert json.loads(row["heads_up_tip_scores_json"]) == [0.8, 0.6]
    assert row["attempt_index"] == "2"
    assert row["lease_claim_index"] == "2"
    assert row["execution_attempt_index"] == "2"
    assert row["retry_context_applied"] == "true"
    assert row["retry_previous_error"] == "Schema validation failed at recipeInstructions[0].text"
    assert row["retry_previous_error_chars"] == "54"
    assert row["retry_previous_error_sha256"]
    assert row["failure_category"] == ""
    assert row["rate_limit_suspected"] == "false"


def test_run_codex_exec_persists_trace_artifact_and_trace_metadata(
    monkeypatch,
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "out.json"
    schema_path = tmp_path / "schema.json"
    log_path = tmp_path / "codex_exec_activity.csv"
    trace_path = tmp_path / "traces" / "task-1.trace.json"
    schema_path.write_text("{}", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        temp_output = Path(cmd[cmd.index("--output-last-message") + 1])
        temp_output.write_text('{"ok":"OK"}', encoding="utf-8")
        stdout = "\n".join(
            [
                '{"type":"tool.exec","tool":"shell","command":"cat /tmp/input.json"}',
                '{"type":"reasoning.note","analysis":"plan before response"}',
                '{"type":"turn.completed","usage":{"input_tokens":20,"output_tokens":5}}',
            ]
        )
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_codex_exec(
        cd_dir=tmp_path,
        prompt="Return JSON.",
        model="gpt-5.3-codex-spark",
        sandbox="read-only",
        ask_for_approval="never",
        web_search="disabled",
        output_schema=schema_path,
        output_path=output_path,
        timeout_seconds=30,
        usage_log_csv=log_path,
        usage_context={"source": "worker", "run_id": "run-1", "task_id": "task-1"},
        trace_output_path=trace_path,
    )

    assert result.ok is True
    assert trace_path.exists()

    trace_payload = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace_payload["event_count"] == 3
    assert trace_payload["action_event_count"] == 1
    assert trace_payload["reasoning_event_count"] == 1
    assert "tool.exec" in trace_payload["action_event_types"]
    assert "reasoning.note" in trace_payload["reasoning_event_types"]

    rows = _read_rows(log_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["trace_path"] == str(trace_path.resolve())
    assert row["trace_action_count"] == "1"
    assert row["trace_reasoning_count"] == "1"
    assert json.loads(row["trace_action_types_json"]) == ["tool.exec"]
    assert json.loads(row["trace_reasoning_types_json"]) == ["reasoning.note"]


def test_persist_trace_artifact_captures_nested_item_completed_reasoning(
    tmp_path: Path,
) -> None:
    trace_path = tmp_path / "traces" / "nested.trace.json"
    started_at = datetime(2026, 3, 4, 22, 15, tzinfo=UTC)
    finished_at = datetime(2026, 3, 4, 22, 15, 1, tzinfo=UTC)
    events = [
        {
            "type": "item.completed",
            "item": {
                "id": "item_0",
                "type": "reasoning",
                "text": "**Determining recipe boundaries including variations**",
            },
        }
    ]

    (
        resolved_trace_path,
        action_count,
        action_types,
        reasoning_count,
        reasoning_types,
    ) = _persist_trace_artifact(
        trace_output_path=trace_path,
        usage_context={"source": "worker", "task_id": "task-1"},
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=1000,
        status="ok",
        exit_code=0,
        model="gpt-5.3-codex-spark",
        reasoning_effort="low",
        cmd=["codex", "exec"],
        prompt="Return JSON.",
        stdout='{"type":"item.completed","item":{"type":"reasoning","text":"trace"}}',
        stderr="",
        events=events,
        passthrough_lines=[],
    )

    assert resolved_trace_path == trace_path.resolve()
    assert action_count == 0
    assert action_types == []
    assert reasoning_count == 1
    assert reasoning_types == ["item.completed"]

    trace_payload = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace_payload["reasoning_event_count"] == 1
    assert trace_payload["reasoning_event_types"] == ["item.completed"]
    assert trace_payload["reasoning_events"] == events


def test_run_codex_exec_logs_logical_schema_path_when_provided(
    monkeypatch,
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "out.json"
    frozen_schema_path = tmp_path / "frozen.schema.json"
    logical_schema_path = tmp_path / "logical.schema.json"
    log_path = tmp_path / "codex_exec_activity.csv"
    frozen_schema_path.write_text("{}", encoding="utf-8")
    logical_schema_path.write_text("{}", encoding="utf-8")

    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        nonlocal captured_cmd
        captured_cmd = list(cmd)
        temp_output = Path(cmd[cmd.index("--output-last-message") + 1])
        temp_output.write_text('{"ok":"OK"}', encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_codex_exec(
        cd_dir=tmp_path,
        prompt="Return JSON.",
        model="gpt-5.3-codex-spark",
        sandbox="read-only",
        ask_for_approval="never",
        web_search="disabled",
        output_schema=frozen_schema_path,
        output_schema_logical_path=logical_schema_path,
        output_path=output_path,
        timeout_seconds=30,
        usage_log_csv=log_path,
    )

    assert result.ok is True
    assert str(frozen_schema_path.resolve()) in captured_cmd

    rows = _read_rows(log_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["output_schema_path"] == str(logical_schema_path.resolve())


def test_run_codex_exec_estimates_tokens_when_usage_events_missing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "out.json"
    schema_path = tmp_path / "schema.json"
    log_path = tmp_path / "codex_exec_activity.csv"
    schema_path.write_text("{}", encoding="utf-8")
    payload = '{"ok":"OK"}'

    def fake_run(cmd, **kwargs):
        temp_output = Path(cmd[cmd.index("--output-last-message") + 1])
        temp_output.write_text(payload, encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    prompt = "INPUT=/tmp/input.json\nReturn JSON."
    result = run_codex_exec(
        cd_dir=tmp_path,
        prompt=prompt,
        model="gpt-5.3-codex-spark",
        sandbox="read-only",
        ask_for_approval="never",
        web_search="disabled",
        output_schema=schema_path,
        output_path=output_path,
        timeout_seconds=30,
        usage_log_csv=log_path,
    )

    assert result.ok is True

    rows = _read_rows(log_path)
    assert len(rows) == 1
    row = rows[0]
    expected_input_tokens = (len(prompt) + 3) // 4
    expected_output_tokens = (len(payload.encode("utf-8")) + 3) // 4
    assert row["tokens_input"] == str(expected_input_tokens)
    assert row["tokens_cached_input"] == "0"
    assert row["tokens_output"] == str(expected_output_tokens)
    assert row["tokens_reasoning"] == ""
    assert row["tokens_total"] == str(expected_input_tokens + expected_output_tokens)
    usage = json.loads(row["usage_json"])
    assert usage["estimated"] is True
    assert usage["method"] == "chars_div_4"


def test_run_codex_exec_logs_timeout_and_raises(monkeypatch, tmp_path: Path) -> None:
    output_path = tmp_path / "out.json"
    schema_path = tmp_path / "schema.json"
    log_path = tmp_path / "codex_exec_activity.csv"
    schema_path.write_text("{}", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=cmd,
            timeout=kwargs["timeout"],
            output='{"type":"thread.started","thread_id":"thread-timeout"}',
            stderr="timed out",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(CodexExecTimeoutError):
        run_codex_exec(
            cd_dir=tmp_path,
            prompt="Return JSON.",
            model="gpt-5.3-codex-spark",
            sandbox="read-only",
            ask_for_approval="never",
            web_search="disabled",
            output_schema=schema_path,
            output_path=output_path,
            timeout_seconds=1,
            usage_log_csv=log_path,
            usage_context={"source": "one"},
        )

    rows = _read_rows(log_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "timeout"
    assert row["exit_code"] == ""
    assert row["source"] == "one"
    assert row["output_payload_present"] == "false"
    assert row["failure_category"] == "timeout"
    assert row["rate_limit_suspected"] == "false"


def test_run_codex_exec_accepts_nonzero_exit_with_payload_and_logs_it(
    monkeypatch,
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "out.json"
    schema_path = tmp_path / "schema.json"
    log_path = tmp_path / "codex_exec_activity.csv"
    schema_path.write_text("{}", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        temp_output = Path(cmd[cmd.index("--output-last-message") + 1])
        temp_output.write_text('{"ok":"OK"}', encoding="utf-8")
        stdout = '{"type":"turn.completed","usage":{"input_tokens":9,"output_tokens":4}}'
        return subprocess.CompletedProcess(cmd, 1, stdout=stdout, stderr="warning")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_codex_exec(
        cd_dir=tmp_path,
        prompt="Return JSON.",
        model="gpt-5.3-codex-spark",
        sandbox="read-only",
        ask_for_approval="never",
        web_search="disabled",
        output_schema=schema_path,
        output_path=output_path,
        timeout_seconds=30,
        usage_log_csv=log_path,
    )

    assert result.ok is True
    assert result.exit_code == 1
    rows = _read_rows(log_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "ok"
    assert row["accepted_nonzero_exit"] == "true"
    assert row["exit_code"] == "1"
    assert row["failure_category"] == "accepted_nonzero_exit"


def test_run_codex_exec_logs_failed_category_and_rate_limit_signal(
    monkeypatch,
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "out.json"
    schema_path = tmp_path / "schema.json"
    log_path = tmp_path / "codex_exec_activity.csv"
    schema_path.write_text("{}", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout='{"type":"thread.started","thread_id":"thread-rate-limit"}',
            stderr="HTTP 429 Too Many Requests",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_codex_exec(
        cd_dir=tmp_path,
        prompt="Return JSON.",
        model="gpt-5.3-codex-spark",
        sandbox="read-only",
        ask_for_approval="never",
        web_search="disabled",
        output_schema=schema_path,
        output_path=output_path,
        timeout_seconds=30,
        usage_log_csv=log_path,
    )

    assert result.ok is False
    rows = _read_rows(log_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "failed"
    assert row["failure_category"] == "nonzero_exit_no_payload"
    assert row["rate_limit_suspected"] == "true"


def test_run_codex_exec_passes_reasoning_effort_config(
    monkeypatch,
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "out.json"
    schema_path = tmp_path / "schema.json"
    schema_path.write_text("{}", encoding="utf-8")

    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        nonlocal captured_cmd
        captured_cmd = list(cmd)
        temp_output = Path(cmd[cmd.index("--output-last-message") + 1])
        temp_output.write_text('{"ok":"OK"}', encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_codex_exec(
        cd_dir=tmp_path,
        prompt="Return JSON.",
        model="gpt-5.3-codex-spark",
        sandbox="read-only",
        ask_for_approval="never",
        web_search="disabled",
        reasoning_effort="high",
        output_schema=schema_path,
        output_path=output_path,
        timeout_seconds=30,
    )

    assert result.ok is True
    assert "--config" in captured_cmd
    assert 'model_reasoning_effort="high"' in captured_cmd


def test_run_codex_exec_passes_env_overrides_and_logs_context(
    monkeypatch,
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "out.json"
    schema_path = tmp_path / "schema.json"
    log_path = tmp_path / "codex_exec_activity.csv"
    trace_path = tmp_path / "trace.json"
    schema_path.write_text("{}", encoding="utf-8")

    captured_env: dict[str, str] | None = None

    def fake_run(cmd, **kwargs):
        nonlocal captured_env
        captured_env = kwargs.get("env")
        temp_output = Path(cmd[cmd.index("--output-last-message") + 1])
        temp_output.write_text('{"ok":"OK"}', encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_codex_exec(
        cd_dir=tmp_path,
        prompt="Return JSON.",
        model="gpt-5.3-codex-spark",
        sandbox="read-only",
        ask_for_approval="never",
        web_search="disabled",
        output_schema=schema_path,
        output_path=output_path,
        timeout_seconds=30,
        env_overrides={"CODEX_HOME": "/tmp/codex-home"},
        usage_log_csv=log_path,
        usage_context={
            "execution_context": "scratch",
            "codex_home_path": "/tmp/codex-home",
        },
        trace_output_path=trace_path,
    )

    assert result.ok is True
    assert captured_env is not None
    assert captured_env["CODEX_HOME"] == "/tmp/codex-home"
    rows = _read_rows(log_path)
    assert rows[0]["execution_context"] == "scratch"
    assert rows[0]["codex_home_path"] == "/tmp/codex-home"
    trace_payload = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace_payload["execution_context"] == "scratch"
    assert trace_payload["codex_home_path"] == "/tmp/codex-home"


def test_extract_retry_after_seconds_parses_seconds_hint() -> None:
    assert extract_retry_after_seconds("HTTP 429 retry after 12 seconds") == 12


def test_extract_retry_after_seconds_parses_minutes_hint() -> None:
    assert extract_retry_after_seconds("Too many requests. Try again in 2 minutes.") == 120


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("WebSocket error: HTTP 403 Forbidden wss://chatgpt.com/backend-api/codex/responses", True),
        ("authentication failed: please sign in with ChatGPT", True),
        ("HTTP 429 Too Many Requests", False),
        ("schema validation failed at <root>", False),
    ],
)
def test_is_auth_failure_message(message: str, expected: bool) -> None:
    assert is_auth_failure_message(message) is expected
