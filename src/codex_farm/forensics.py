"""Failure-forensics bundle capture and query helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import shutil
import sqlite3
from typing import Literal
import uuid

from .db import (
    insert_failure_forensics as db_insert_failure_forensics,
    list_failure_forensics as db_list_failure_forensics,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FailureForensicsRequest:
    data_dir: Path
    source: Literal["worker", "one"]
    run_id: str | None
    task_id: str | None
    pipeline_id: str
    attempt_index: int | None
    terminal: bool
    input_path: Path | None
    input_hash: str | None
    rel_output_path: str | None
    worker_id: str | None
    failure_stage: str
    failure_category: str
    error_message_full: str
    error_message_summary: str
    prompt_text: str | None
    schema_path: Path | None
    output_path: Path | None
    stdout_tail: str | None
    stderr_tail: str | None
    runtime_context: dict[str, object]
    previous_error: str | None = None


@dataclass(frozen=True)
class FailureForensicsRecord:
    forensics_id: str
    source: str
    run_id: str | None
    task_id: str | None
    pipeline_id: str
    attempt_index: int | None
    terminal: bool
    failure_stage: str
    failure_category: str
    bundle_dir: Path
    metadata_path: Path
    raw_output_path: Path | None
    created_at: str


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_text_artifact(
    *,
    bundle_dir: Path,
    relative_path: str,
    text: str,
) -> dict[str, object]:
    artifact_path = bundle_dir / relative_path
    artifact_path.write_text(text, encoding="utf-8")
    return {
        "path": relative_path,
        "bytes": artifact_path.stat().st_size,
        "sha256": _sha256_file(artifact_path),
    }


def _copy_artifact(
    *,
    bundle_dir: Path,
    source_path: Path,
    relative_path: str,
) -> dict[str, object]:
    artifact_path = bundle_dir / relative_path
    shutil.copyfile(source_path, artifact_path)
    return {
        "path": relative_path,
        "bytes": artifact_path.stat().st_size,
        "sha256": _sha256_file(artifact_path),
    }


def _input_snapshot_name(input_path: Path) -> str:
    suffix = "".join(input_path.suffixes)
    if suffix:
        return f"input.snapshot{suffix}"
    return "input.snapshot"


def _bundle_dir_for_request(
    *,
    request: FailureForensicsRequest,
    forensics_id: str,
) -> Path:
    data_dir = request.data_dir.expanduser().resolve()
    root = data_dir / "forensics"
    if request.source == "worker":
        run_part = request.run_id or "unknown-run"
        task_part = request.task_id or "unknown-task"
        if request.attempt_index is None:
            attempt_part = "attempt-unknown"
        else:
            attempt_part = f"attempt-{request.attempt_index}"
        return root / "runs" / run_part / task_part / attempt_part
    return root / "one" / forensics_id


def capture_failure_forensics(
    conn: sqlite3.Connection | None,
    *,
    request: FailureForensicsRequest,
) -> FailureForensicsRecord | None:
    try:
        created_at = _utc_now_iso()
        forensics_id = uuid.uuid4().hex
        bundle_dir = _bundle_dir_for_request(request=request, forensics_id=forensics_id)
        bundle_dir.mkdir(parents=True, exist_ok=True)

        artifacts: dict[str, dict[str, object]] = {}
        raw_output_path: Path | None = None

        if request.prompt_text is not None:
            artifacts["prompt"] = _write_text_artifact(
                bundle_dir=bundle_dir,
                relative_path="prompt.txt",
                text=request.prompt_text,
            )

        if request.input_path is not None and request.input_path.exists() and request.input_path.is_file():
            input_path_resolved = request.input_path.expanduser().resolve()
            artifacts["input_snapshot"] = _copy_artifact(
                bundle_dir=bundle_dir,
                source_path=input_path_resolved,
                relative_path=_input_snapshot_name(input_path_resolved),
            )

        if request.schema_path is not None and request.schema_path.exists() and request.schema_path.is_file():
            artifacts["schema"] = _copy_artifact(
                bundle_dir=bundle_dir,
                source_path=request.schema_path.expanduser().resolve(),
                relative_path="schema.json",
            )

        if request.output_path is not None and request.output_path.exists() and request.output_path.is_file():
            raw_output_path = (bundle_dir / "output.raw.json").resolve()
            artifacts["raw_output"] = _copy_artifact(
                bundle_dir=bundle_dir,
                source_path=request.output_path.expanduser().resolve(),
                relative_path="output.raw.json",
            )

        if request.stderr_tail:
            artifacts["stderr_tail"] = _write_text_artifact(
                bundle_dir=bundle_dir,
                relative_path="stderr_tail.txt",
                text=request.stderr_tail,
            )

        if request.stdout_tail:
            artifacts["stdout_tail"] = _write_text_artifact(
                bundle_dir=bundle_dir,
                relative_path="stdout_tail.txt",
                text=request.stdout_tail,
            )

        input_path_text: str | None = None
        if request.input_path is not None:
            input_path_text = str(request.input_path.expanduser().resolve())
        output_path_text: str | None = None
        if request.output_path is not None:
            output_path_text = str(request.output_path.expanduser().resolve())
        schema_path_text: str | None = None
        if request.schema_path is not None:
            schema_path_text = str(request.schema_path.expanduser().resolve())

        metadata = {
            "schema_version": 1,
            "forensics_id": forensics_id,
            "created_at": created_at,
            "source": request.source,
            "run_id": request.run_id,
            "task_id": request.task_id,
            "pipeline_id": request.pipeline_id,
            "attempt_index": request.attempt_index,
            "terminal": request.terminal,
            "failure_stage": request.failure_stage,
            "failure_category": request.failure_category,
            "error_message_full": request.error_message_full,
            "error_message_summary": request.error_message_summary,
            "previous_error": request.previous_error,
            "input_path": input_path_text,
            "input_hash": request.input_hash,
            "rel_output_path": request.rel_output_path,
            "output_path": output_path_text,
            "schema_path": schema_path_text,
            "worker_id": request.worker_id,
            "runtime_context": request.runtime_context,
            "artifacts": artifacts,
        }

        metadata_path = (bundle_dir / "metadata.json").resolve()
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        record = FailureForensicsRecord(
            forensics_id=forensics_id,
            source=request.source,
            run_id=request.run_id,
            task_id=request.task_id,
            pipeline_id=request.pipeline_id,
            attempt_index=request.attempt_index,
            terminal=request.terminal,
            failure_stage=request.failure_stage,
            failure_category=request.failure_category,
            bundle_dir=bundle_dir.resolve(),
            metadata_path=metadata_path,
            raw_output_path=raw_output_path,
            created_at=created_at,
        )

        if conn is not None:
            try:
                db_insert_failure_forensics(
                    conn,
                    forensics_id=record.forensics_id,
                    source=record.source,
                    run_id=record.run_id,
                    task_id=record.task_id,
                    pipeline_id=record.pipeline_id,
                    attempt_index=record.attempt_index,
                    terminal=record.terminal,
                    input_path=input_path_text,
                    rel_output_path=request.rel_output_path,
                    error_summary=request.error_message_summary,
                    failure_stage=record.failure_stage,
                    failure_category=record.failure_category,
                    bundle_dir=str(record.bundle_dir),
                    metadata_path=str(record.metadata_path),
                    raw_output_path=str(record.raw_output_path) if record.raw_output_path else None,
                    created_at=record.created_at,
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.warning("Failed to insert forensics row for %s: %s", forensics_id, exc)

        return record
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Failed to capture forensics bundle: %s", exc)
        return None


def list_failure_forensics(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    task_id: str | None = None,
) -> list[dict[str, object]]:
    rows = db_list_failure_forensics(conn, run_id=run_id, task_id=task_id)
    normalized: list[dict[str, object]] = []
    for row in rows:
        item = dict(row)
        item["terminal"] = bool(item.get("terminal"))
        normalized.append(item)
    return normalized
