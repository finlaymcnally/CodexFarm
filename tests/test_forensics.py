import json
from pathlib import Path

from codex_farm.db import init_db, open_db
from codex_farm.forensics import (
    FailureForensicsRequest,
    capture_failure_forensics,
    list_failure_forensics,
)


def test_capture_failure_forensics_copies_input_prompt_schema_and_raw_output(tmp_path: Path) -> None:
    data_dir = tmp_path / "var"
    db_path = data_dir / "codex_farm.sqlite3"
    conn = open_db(db_path)
    init_db(conn)

    input_path = tmp_path / "in" / "sample.json"
    schema_path = tmp_path / "schemas" / "schema.json"
    output_path = tmp_path / "out" / "sample.json"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text('{"name":"Example"}\n', encoding="utf-8")
    schema_path.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["name", "required_field"],
                "properties": {
                    "name": {"type": "string"},
                    "required_field": {"type": "string"},
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    output_path.write_text('{"name":"Example"}\n', encoding="utf-8")

    request = FailureForensicsRequest(
        data_dir=data_dir,
        source="worker",
        run_id="run-1",
        task_id="task-1",
        pipeline_id="demo.forensics.v1",
        attempt_index=1,
        terminal=True,
        input_path=input_path,
        input_hash="abc123",
        rel_output_path="sample.json",
        worker_id="worker-1",
        failure_stage="schema_validation",
        failure_category="schema_validation",
        error_message_full="Schema validation failed at <root>: required_field is required",
        error_message_summary="Schema validation failed at <root>: required_field is required",
        prompt_text="Input file path: /tmp/in/sample.json\nReturn JSON only.\n",
        schema_path=schema_path,
        output_path=output_path,
        stdout_tail="stdout warning",
        stderr_tail="stderr warning",
        runtime_context={"branch": "unit-test"},
        previous_error=None,
    )

    record = capture_failure_forensics(conn, request=request)
    assert record is not None
    assert record.run_id == "run-1"
    assert record.task_id == "task-1"
    assert record.attempt_index == 1
    assert record.bundle_dir.exists()
    assert record.metadata_path.exists()
    assert record.raw_output_path is not None
    assert record.raw_output_path.exists()
    assert record.raw_output_path.read_text(encoding="utf-8") == output_path.read_text(encoding="utf-8")

    metadata = json.loads(record.metadata_path.read_text(encoding="utf-8"))
    assert metadata["schema_version"] == 1
    assert metadata["pipeline_id"] == "demo.forensics.v1"
    assert metadata["failure_stage"] == "schema_validation"
    assert metadata["failure_category"] == "schema_validation"
    assert metadata["artifacts"]["prompt"]["path"] == "prompt.txt"
    assert metadata["artifacts"]["schema"]["path"] == "schema.json"
    assert metadata["artifacts"]["raw_output"]["path"] == "output.raw.json"
    assert metadata["artifacts"]["input_snapshot"]["path"].startswith("input.snapshot")

    rows = list_failure_forensics(conn, run_id="run-1")
    assert len(rows) == 1
    assert rows[0]["forensics_id"] == record.forensics_id
    assert rows[0]["task_id"] == "task-1"
    assert rows[0]["attempt_index"] == 1
    assert rows[0]["failure_stage"] == "schema_validation"
    assert rows[0]["failure_category"] == "schema_validation"
    assert rows[0]["terminal"] is True
