import csv
import json
from pathlib import Path

from typer.testing import CliRunner

from codex_farm.cli import app


runner = CliRunner()


def _write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = sorted({key for row in rows for key in row})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_stats_dashboard_writes_static_bundle(tmp_path: Path) -> None:
    data_dir = tmp_path / "var"
    csv_path = data_dir / "codex_exec_activity.csv"
    rows = [
        {
            "logged_at_utc": "2026-02-22T18:00:00.000Z",
            "duration_ms": "1000",
            "status": "ok",
            "exit_code": "0",
            "accepted_nonzero_exit": "false",
            "output_payload_present": "true",
            "output_bytes": "175",
            "tokens_input": "120",
            "tokens_cached_input": "10",
            "tokens_output": "30",
            "tokens_total": "150",
            "source": "worker",
            "pipeline_id": "demo.echo.v1",
            "run_id": "run-1",
            "task_id": "task-1",
            "worker_id": "worker-1",
            "model": "gpt-5.3-codex-spark",
            "sandbox": "read-only",
            "prompt_chars": "202",
            "input_path": "/tmp/input-a.json",
            "output_path": "/tmp/output-a.json",
        },
        {
            "logged_at_utc": "2026-02-22T18:05:00.000Z",
            "duration_ms": "2400",
            "status": "failed",
            "exit_code": "1",
            "accepted_nonzero_exit": "false",
            "output_payload_present": "false",
            "output_bytes": "0",
            "tokens_input": "50",
            "tokens_output": "25",
            "tokens_total": "",
            "source": "worker",
            "pipeline_id": "demo.echo.v1",
            "run_id": "run-1",
            "task_id": "task-2",
            "worker_id": "worker-2",
            "model": "gpt-5.3-codex-spark",
            "sandbox": "read-only",
            "input_path": "/tmp/input-b.json",
            "output_path": "/tmp/output-b.json",
        },
        {
            "logged_at_utc": "2026-02-22T18:10:00.000Z",
            "duration_ms": "500",
            "status": "timeout",
            "exit_code": "",
            "accepted_nonzero_exit": "false",
            "output_payload_present": "false",
            "output_bytes": "0",
            "tokens_input": "2",
            "tokens_output": "3",
            "tokens_total": "",
            "source": "one",
            "pipeline_id": "",
            "run_id": "",
            "task_id": "",
            "worker_id": "",
            "model": "gpt-5.3-codex-spark",
            "sandbox": "workspace-write",
            "input_path": "/tmp/input-c.json",
            "output_path": "/tmp/output-c.json",
        },
    ]
    _write_rows(csv_path, rows)

    result = runner.invoke(
        app,
        [
            "stats-dashboard",
            "--data-dir",
            str(data_dir),
            "--recent-limit",
            "100",
        ],
    )
    assert result.exit_code == 0, result.stdout

    dashboard_dir = data_dir / "analytics-dashboard"
    index_path = dashboard_dir / "index.html"
    data_path = dashboard_dir / "assets" / "dashboard_data.json"
    js_path = dashboard_dir / "assets" / "dashboard.js"
    css_path = dashboard_dir / "assets" / "style.css"
    assert index_path.exists()
    assert data_path.exists()
    assert js_path.exists()
    assert css_path.exists()

    payload = json.loads(data_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["row_count"] == 3
    assert payload["summary"]["status_counts"]["ok"] == 1
    assert payload["summary"]["status_counts"]["failed"] == 1
    assert payload["summary"]["status_counts"]["timeout"] == 1
    assert payload["summary"]["status_counts"]["other"] == 0
    assert payload["summary"]["success_rate_pct"] == 33.3
    assert payload["summary"]["unique_runs"] == 1
    assert payload["duration"]["avg_ms"] == 1300
    assert payload["duration"]["p95_ms"] == 2400
    assert payload["tokens"]["total"] == 230
    assert payload["tokens"]["cached_input"] == 10

    source_rows = {row["source"]: row for row in payload["source_breakdown"]}
    assert source_rows["worker"]["calls"] == 2
    assert source_rows["one"]["calls"] == 1

    index_html = index_path.read_text(encoding="utf-8")
    assert 'id="dashboard-data"' in index_html
    assert "dashboard_data.json" in js_path.read_text(encoding="utf-8")


def test_stats_dashboard_handles_missing_csv(tmp_path: Path) -> None:
    data_dir = tmp_path / "var"
    data_dir.mkdir(parents=True, exist_ok=True)

    result = runner.invoke(
        app,
        [
            "stats-dashboard",
            "--data-dir",
            str(data_dir),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "warning: Telemetry CSV does not exist" in result.stdout

    payload = json.loads(
        (data_dir / "analytics-dashboard" / "assets" / "dashboard_data.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["row_count"] == 0
    assert payload["warnings"]
