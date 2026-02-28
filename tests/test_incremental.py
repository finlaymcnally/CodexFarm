import hashlib
import json
from pathlib import Path

import pytest

from codex_farm.db import PlannedTaskRow, create_run, init_db, insert_planned_tasks_for_run, open_db
from codex_farm.incremental import (
    FALLBACK_HASH_CHANGED,
    FALLBACK_NO_PRIOR_SUCCESS,
    FALLBACK_SOURCE_OUTPUT_INVALID,
    IncrementalSourceRunError,
    enumerate_input_candidates,
    plan_incremental_decisions,
    select_incremental_source_run,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _write_schema(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["ok"],
                "properties": {"ok": {"type": "string"}},
            }
        ),
        encoding="utf-8",
    )


def test_plan_incremental_decisions_without_source_queues_all(tmp_path: Path) -> None:
    db_path = tmp_path / "var" / "codex_farm.sqlite3"
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    schema_path = tmp_path / "schema.json"
    input_root.mkdir(parents=True)
    output_root.mkdir(parents=True)
    _write_schema(schema_path)

    (input_root / "a.json").write_text(json.dumps({"value": "a"}), encoding="utf-8")
    (input_root / "b.json").write_text(json.dumps({"value": "b"}), encoding="utf-8")

    conn = open_db(db_path)
    init_db(conn)
    candidates = enumerate_input_candidates(
        input_files=[input_root / "a.json", input_root / "b.json"],
        input_root=input_root,
        output_ext=".json",
    )

    decisions, summary = plan_incremental_decisions(
        conn=conn,
        pipeline_id="demo.incremental.v1",
        execution_fingerprint="fp-1",
        input_candidates=candidates,
        output_root=output_root,
        schema_path=schema_path,
        incremental_enabled=True,
        explicit_source_run_id=None,
    )

    assert len(decisions) == 2
    assert all(decision.action == "queue" for decision in decisions)
    assert summary.enabled is True
    assert summary.source_run_id is None
    assert summary.reused == 0
    assert summary.queued == 2
    assert summary.fallback_counts[FALLBACK_NO_PRIOR_SUCCESS] == 2


def test_plan_incremental_decisions_reuse_and_hash_fallback(tmp_path: Path) -> None:
    db_path = tmp_path / "var" / "codex_farm.sqlite3"
    source_input_root = tmp_path / "source-input"
    source_output_root = tmp_path / "source-output"
    current_output_root = tmp_path / "current-output"
    schema_path = tmp_path / "schema.json"
    source_input_root.mkdir(parents=True)
    source_output_root.mkdir(parents=True)
    current_output_root.mkdir(parents=True)
    _write_schema(schema_path)

    source_input_a = source_input_root / "a.json"
    source_input_b = source_input_root / "b.json"
    source_input_a.write_text(json.dumps({"value": "a"}), encoding="utf-8")
    source_input_b.write_text(json.dumps({"value": "b"}), encoding="utf-8")

    source_output_a = source_output_root / "a.json"
    source_output_b = source_output_root / "b.json"
    source_output_a.write_text(json.dumps({"ok": "A"}), encoding="utf-8")
    source_output_b.write_text(json.dumps({"ok": "B"}), encoding="utf-8")

    conn = open_db(db_path)
    init_db(conn)
    source_run_id = create_run(
        conn,
        pipeline_id="demo.incremental.v1",
        input_dir=str(source_input_root.resolve()),
        glob="**/*.json",
        output_dir=str(source_output_root.resolve()),
        config={},
        execution_fingerprint="fp-1",
    )
    insert_planned_tasks_for_run(
        conn,
        run_id=source_run_id,
        planned_tasks=[
            PlannedTaskRow(
                input_path=str(source_input_a.resolve()),
                input_hash=_sha256(source_input_a),
                rel_output_path="a.json",
                status="done",
                output_path=str(source_output_a.resolve()),
            ),
            PlannedTaskRow(
                input_path=str(source_input_b.resolve()),
                input_hash=_sha256(source_input_b),
                rel_output_path="b.json",
                status="done",
                output_path=str(source_output_b.resolve()),
            ),
        ],
    )

    current_input_root = tmp_path / "current-input"
    current_input_root.mkdir(parents=True)
    current_input_a = current_input_root / "a.json"
    current_input_b = current_input_root / "b.json"
    current_input_a.write_text(json.dumps({"value": "a"}), encoding="utf-8")
    current_input_b.write_text(json.dumps({"value": "b changed"}), encoding="utf-8")

    candidates = enumerate_input_candidates(
        input_files=[current_input_a, current_input_b],
        input_root=current_input_root,
        output_ext=".json",
    )
    decisions, summary = plan_incremental_decisions(
        conn=conn,
        pipeline_id="demo.incremental.v1",
        execution_fingerprint="fp-1",
        input_candidates=candidates,
        output_root=current_output_root,
        schema_path=schema_path,
        incremental_enabled=True,
        explicit_source_run_id=None,
    )

    assert summary.enabled is True
    assert summary.source_run_id == source_run_id
    assert summary.reused == 1
    assert summary.queued == 1
    assert summary.fallback_counts[FALLBACK_HASH_CHANGED] == 1
    reused = [decision for decision in decisions if decision.action == "reuse"]
    queued = [decision for decision in decisions if decision.action == "queue"]
    assert len(reused) == 1
    assert len(queued) == 1
    assert reused[0].rel_input_path == "a.json"
    assert reused[0].reused_from_run_id == source_run_id
    assert (current_output_root / "a.json").exists()
    assert queued[0].fallback_reason == FALLBACK_HASH_CHANGED


def test_select_incremental_source_run_explicit_rejects_non_terminal(tmp_path: Path) -> None:
    db_path = tmp_path / "var" / "codex_farm.sqlite3"
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir(parents=True)
    output_root.mkdir(parents=True)

    conn = open_db(db_path)
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id="demo.incremental.v1",
        input_dir=str(input_root.resolve()),
        glob="**/*.json",
        output_dir=str(output_root.resolve()),
        config={},
        execution_fingerprint="fp-1",
    )

    with pytest.raises(IncrementalSourceRunError) as excinfo:
        select_incremental_source_run(
            conn,
            pipeline_id="demo.incremental.v1",
            execution_fingerprint="fp-1",
            explicit_source_run_id=run_id,
        )
    assert "must reference a terminal run" in str(excinfo.value)


def test_plan_incremental_decisions_invalid_source_output_fallback(tmp_path: Path) -> None:
    db_path = tmp_path / "var" / "codex_farm.sqlite3"
    source_input_root = tmp_path / "source-input"
    source_output_root = tmp_path / "source-output"
    current_input_root = tmp_path / "current-input"
    current_output_root = tmp_path / "current-output"
    schema_path = tmp_path / "schema.json"
    source_input_root.mkdir(parents=True)
    source_output_root.mkdir(parents=True)
    current_input_root.mkdir(parents=True)
    current_output_root.mkdir(parents=True)
    _write_schema(schema_path)

    source_input = source_input_root / "a.json"
    source_input.write_text(json.dumps({"value": "a"}), encoding="utf-8")
    source_output = source_output_root / "a.json"
    source_output.write_text(json.dumps({"not_ok": "bad"}), encoding="utf-8")

    conn = open_db(db_path)
    init_db(conn)
    source_run_id = create_run(
        conn,
        pipeline_id="demo.incremental.v1",
        input_dir=str(source_input_root.resolve()),
        glob="**/*.json",
        output_dir=str(source_output_root.resolve()),
        config={},
        execution_fingerprint="fp-1",
    )
    insert_planned_tasks_for_run(
        conn,
        run_id=source_run_id,
        planned_tasks=[
            PlannedTaskRow(
                input_path=str(source_input.resolve()),
                input_hash=_sha256(source_input),
                rel_output_path="a.json",
                status="done",
                output_path=str(source_output.resolve()),
            )
        ],
    )

    current_input = current_input_root / "a.json"
    current_input.write_text(source_input.read_text(encoding="utf-8"), encoding="utf-8")

    candidates = enumerate_input_candidates(
        input_files=[current_input],
        input_root=current_input_root,
        output_ext=".json",
    )
    decisions, summary = plan_incremental_decisions(
        conn=conn,
        pipeline_id="demo.incremental.v1",
        execution_fingerprint="fp-1",
        input_candidates=candidates,
        output_root=current_output_root,
        schema_path=schema_path,
        incremental_enabled=True,
        explicit_source_run_id=source_run_id,
    )

    assert len(decisions) == 1
    assert decisions[0].action == "queue"
    assert decisions[0].fallback_reason == FALLBACK_SOURCE_OUTPUT_INVALID
    assert summary.reused == 0
    assert summary.queued == 1
    assert summary.fallback_counts[FALLBACK_SOURCE_OUTPUT_INVALID] == 1
    assert not (current_output_root / "a.json").exists()
