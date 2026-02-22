import csv
from pathlib import Path
import subprocess

import pytest

from codex_farm.codex_exec import CodexExecTimeoutError, run_codex_exec


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


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
                '{"type":"turn.completed","usage":{"input_tokens":120,"cached_input_tokens":10,"output_tokens":30}}',
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
