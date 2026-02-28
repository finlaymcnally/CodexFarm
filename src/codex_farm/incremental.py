"""Planning helpers for safe incremental run reuse."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from typing import Literal
import uuid

from .db import (
    find_latest_compatible_terminal_run,
    get_run,
    list_successful_tasks_for_run,
)
from .pipeline_spec import PipelineSpec
from .schema_utils import SchemaValidationError, validate_json_file_against_schema


FALLBACK_NO_PRIOR_SUCCESS = "no_prior_success"
FALLBACK_HASH_CHANGED = "hash_changed"
FALLBACK_SOURCE_OUTPUT_MISSING = "source_output_missing"
FALLBACK_SOURCE_OUTPUT_INVALID = "source_output_invalid"
FALLBACK_REASONS = (
    FALLBACK_NO_PRIOR_SUCCESS,
    FALLBACK_HASH_CHANGED,
    FALLBACK_SOURCE_OUTPUT_MISSING,
    FALLBACK_SOURCE_OUTPUT_INVALID,
)


class IncrementalSourceRunError(RuntimeError):
    """Raised when --incremental-from points to an unusable source run."""


class SourceOutputUnavailableError(RuntimeError):
    """Raised when the prior output artifact cannot be read safely."""


@dataclass(frozen=True)
class InputCandidate:
    rel_input_path: str
    input_path: Path
    input_hash: str
    rel_output_path: str


@dataclass(frozen=True)
class IncrementalDecision:
    rel_input_path: str
    input_path: Path
    input_hash: str
    rel_output_path: str
    action: Literal["reuse", "queue"]
    reused_from_run_id: str | None
    reused_from_task_id: str | None
    output_path: Path | None
    fallback_reason: str | None


@dataclass(frozen=True)
class IncrementalSummary:
    enabled: bool
    source_run_id: str | None
    reused: int
    queued: int
    fallback_counts: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "source_run_id": self.source_run_id,
            "reused": self.reused,
            "queued": self.queued,
            "fallback_counts": dict(self.fallback_counts),
        }


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolved_path_string(path: Path | None) -> str | None:
    if path is None:
        return None
    return str(path.expanduser().resolve())


def _empty_fallback_counts() -> dict[str, int]:
    return {reason: 0 for reason in FALLBACK_REASONS}


def build_execution_fingerprint(
    *,
    pipeline: PipelineSpec,
    resolved_model: str,
    resolved_reasoning_effort: str | None,
    resolved_output_schema: Path,
    input_root: Path,
    farm_root: Path,
    workspace_root_override: Path | None,
) -> str:
    payload = {
        "pipeline_id": pipeline.pipeline_id,
        "model": resolved_model,
        "reasoning_effort": resolved_reasoning_effort,
        "prompt_template_path": _resolved_path_string(pipeline.prompt_template_path),
        "prompt_template_sha256": _hash_file(pipeline.prompt_template_path),
        "output_schema_path": _resolved_path_string(resolved_output_schema),
        "output_schema_sha256": _hash_file(resolved_output_schema),
        "codex": {
            "sandbox": pipeline.codex_sandbox,
            "ask_for_approval": pipeline.codex_ask_for_approval,
            "web_search": pipeline.codex_web_search,
            "cd_mode": pipeline.codex_cd_mode,
            "output_ext": pipeline.output_ext,
        },
        "paths": {
            "input_root": _resolved_path_string(input_root),
            "farm_root": _resolved_path_string(farm_root),
            "workspace_root_override": _resolved_path_string(workspace_root_override),
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def enumerate_input_candidates(
    *,
    input_files: list[Path],
    input_root: Path,
    output_ext: str,
) -> list[InputCandidate]:
    input_root_resolved = input_root.resolve()
    candidates: list[InputCandidate] = []
    for input_file in sorted(input_files):
        resolved_input = input_file.resolve()
        rel_input = resolved_input.relative_to(input_root_resolved).as_posix()
        rel_output = Path(rel_input).with_suffix(output_ext).as_posix()
        candidates.append(
            InputCandidate(
                rel_input_path=rel_input,
                input_path=resolved_input,
                input_hash=_hash_file(resolved_input),
                rel_output_path=rel_output,
            )
        )
    return candidates


def select_incremental_source_run(
    conn: sqlite3.Connection,
    *,
    pipeline_id: str,
    execution_fingerprint: str,
    explicit_source_run_id: str | None,
) -> dict | None:
    if explicit_source_run_id is None:
        return find_latest_compatible_terminal_run(
            conn,
            pipeline_id=pipeline_id,
            execution_fingerprint=execution_fingerprint,
        )

    try:
        source_run = get_run(conn, explicit_source_run_id)
    except KeyError as exc:
        raise IncrementalSourceRunError(
            f"--incremental-from {explicit_source_run_id} was not found"
        ) from exc

    if source_run["status"] not in {"done", "error"}:
        raise IncrementalSourceRunError(
            f"--incremental-from {explicit_source_run_id} must reference a terminal run"
        )
    if source_run["pipeline_id"] != pipeline_id:
        raise IncrementalSourceRunError(
            f"--incremental-from {explicit_source_run_id} is not compatible with the current pipeline and execution fingerprint"
        )
    if source_run.get("execution_fingerprint") != execution_fingerprint:
        raise IncrementalSourceRunError(
            f"--incremental-from {explicit_source_run_id} is not compatible with the current pipeline and execution fingerprint"
        )
    return source_run


def materialize_reused_output(
    *,
    source_output_path: Path,
    destination_output_path: Path,
) -> None:
    source_resolved = source_output_path.expanduser().resolve()
    if not source_resolved.exists() or not source_resolved.is_file():
        raise SourceOutputUnavailableError(
            f"Source output is missing: {source_resolved}"
        )
    if not os.access(source_resolved, os.R_OK):
        raise SourceOutputUnavailableError(
            f"Source output is unreadable: {source_resolved}"
        )

    destination_resolved = destination_output_path.expanduser().resolve()
    destination_resolved.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination_resolved.with_name(
        f".{destination_resolved.name}.tmp-{uuid.uuid4().hex}"
    )

    try:
        with source_resolved.open("rb") as source_fh, temp_path.open("wb") as temp_fh:
            shutil.copyfileobj(source_fh, temp_fh, length=1024 * 1024)
        os.replace(temp_path, destination_resolved)
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        if not source_resolved.exists() or not os.access(source_resolved, os.R_OK):
            raise SourceOutputUnavailableError(str(exc)) from exc
        raise
    finally:
        temp_path.unlink(missing_ok=True)


def plan_incremental_decisions(
    *,
    conn: sqlite3.Connection,
    pipeline_id: str,
    execution_fingerprint: str,
    input_candidates: list[InputCandidate],
    output_root: Path,
    schema_path: Path,
    incremental_enabled: bool,
    explicit_source_run_id: str | None,
) -> tuple[list[IncrementalDecision], IncrementalSummary]:
    if not incremental_enabled:
        decisions = [
            IncrementalDecision(
                rel_input_path=candidate.rel_input_path,
                input_path=candidate.input_path,
                input_hash=candidate.input_hash,
                rel_output_path=candidate.rel_output_path,
                action="queue",
                reused_from_run_id=None,
                reused_from_task_id=None,
                output_path=None,
                fallback_reason=None,
            )
            for candidate in input_candidates
        ]
        return (
            decisions,
            IncrementalSummary(
                enabled=False,
                source_run_id=None,
                reused=0,
                queued=0,
                fallback_counts=_empty_fallback_counts(),
            ),
        )

    source_run = select_incremental_source_run(
        conn,
        pipeline_id=pipeline_id,
        execution_fingerprint=execution_fingerprint,
        explicit_source_run_id=explicit_source_run_id,
    )

    fallback_counts = _empty_fallback_counts()
    if source_run is None:
        decisions = []
        for candidate in input_candidates:
            fallback_counts[FALLBACK_NO_PRIOR_SUCCESS] += 1
            decisions.append(
                IncrementalDecision(
                    rel_input_path=candidate.rel_input_path,
                    input_path=candidate.input_path,
                    input_hash=candidate.input_hash,
                    rel_output_path=candidate.rel_output_path,
                    action="queue",
                    reused_from_run_id=None,
                    reused_from_task_id=None,
                    output_path=None,
                    fallback_reason=FALLBACK_NO_PRIOR_SUCCESS,
                )
            )
        return (
            decisions,
            IncrementalSummary(
                enabled=True,
                source_run_id=None,
                reused=0,
                queued=len(decisions),
                fallback_counts=fallback_counts,
            ),
        )

    source_input_root = Path(str(source_run["input_dir"])).expanduser().resolve()
    source_run_id = str(source_run["run_id"])
    source_tasks_by_rel_input: dict[str, dict] = {}
    for source_task in list_successful_tasks_for_run(conn, run_id=source_run_id):
        try:
            rel_input = (
                Path(str(source_task["input_path"]))
                .expanduser()
                .resolve()
                .relative_to(source_input_root)
                .as_posix()
            )
        except ValueError:
            continue
        source_tasks_by_rel_input[rel_input] = source_task

    output_root_resolved = output_root.resolve()
    decisions: list[IncrementalDecision] = []
    reused = 0

    for candidate in input_candidates:
        source_task = source_tasks_by_rel_input.get(candidate.rel_input_path)
        if source_task is None:
            fallback_counts[FALLBACK_NO_PRIOR_SUCCESS] += 1
            decisions.append(
                IncrementalDecision(
                    rel_input_path=candidate.rel_input_path,
                    input_path=candidate.input_path,
                    input_hash=candidate.input_hash,
                    rel_output_path=candidate.rel_output_path,
                    action="queue",
                    reused_from_run_id=None,
                    reused_from_task_id=None,
                    output_path=None,
                    fallback_reason=FALLBACK_NO_PRIOR_SUCCESS,
                )
            )
            continue

        if str(source_task["input_hash"]) != candidate.input_hash:
            fallback_counts[FALLBACK_HASH_CHANGED] += 1
            decisions.append(
                IncrementalDecision(
                    rel_input_path=candidate.rel_input_path,
                    input_path=candidate.input_path,
                    input_hash=candidate.input_hash,
                    rel_output_path=candidate.rel_output_path,
                    action="queue",
                    reused_from_run_id=None,
                    reused_from_task_id=None,
                    output_path=None,
                    fallback_reason=FALLBACK_HASH_CHANGED,
                )
            )
            continue

        raw_source_output = source_task.get("output_path")
        if not isinstance(raw_source_output, str) or not raw_source_output.strip():
            fallback_counts[FALLBACK_SOURCE_OUTPUT_MISSING] += 1
            decisions.append(
                IncrementalDecision(
                    rel_input_path=candidate.rel_input_path,
                    input_path=candidate.input_path,
                    input_hash=candidate.input_hash,
                    rel_output_path=candidate.rel_output_path,
                    action="queue",
                    reused_from_run_id=None,
                    reused_from_task_id=None,
                    output_path=None,
                    fallback_reason=FALLBACK_SOURCE_OUTPUT_MISSING,
                )
            )
            continue

        source_output_path = Path(raw_source_output).expanduser().resolve()
        destination_output_path = output_root_resolved / candidate.rel_output_path
        try:
            materialize_reused_output(
                source_output_path=source_output_path,
                destination_output_path=destination_output_path,
            )
        except SourceOutputUnavailableError:
            fallback_counts[FALLBACK_SOURCE_OUTPUT_MISSING] += 1
            decisions.append(
                IncrementalDecision(
                    rel_input_path=candidate.rel_input_path,
                    input_path=candidate.input_path,
                    input_hash=candidate.input_hash,
                    rel_output_path=candidate.rel_output_path,
                    action="queue",
                    reused_from_run_id=None,
                    reused_from_task_id=None,
                    output_path=None,
                    fallback_reason=FALLBACK_SOURCE_OUTPUT_MISSING,
                )
            )
            continue

        try:
            validate_json_file_against_schema(
                json_path=destination_output_path,
                schema_path=schema_path,
            )
        except SchemaValidationError:
            destination_output_path.unlink(missing_ok=True)
            fallback_counts[FALLBACK_SOURCE_OUTPUT_INVALID] += 1
            decisions.append(
                IncrementalDecision(
                    rel_input_path=candidate.rel_input_path,
                    input_path=candidate.input_path,
                    input_hash=candidate.input_hash,
                    rel_output_path=candidate.rel_output_path,
                    action="queue",
                    reused_from_run_id=None,
                    reused_from_task_id=None,
                    output_path=None,
                    fallback_reason=FALLBACK_SOURCE_OUTPUT_INVALID,
                )
            )
            continue

        reused += 1
        decisions.append(
            IncrementalDecision(
                rel_input_path=candidate.rel_input_path,
                input_path=candidate.input_path,
                input_hash=candidate.input_hash,
                rel_output_path=candidate.rel_output_path,
                action="reuse",
                reused_from_run_id=source_run_id,
                reused_from_task_id=str(source_task["task_id"]),
                output_path=destination_output_path,
                fallback_reason=None,
            )
        )

    queued = len(decisions) - reused
    return (
        decisions,
        IncrementalSummary(
            enabled=True,
            source_run_id=source_run_id,
            reused=reused,
            queued=queued,
            fallback_counts=fallback_counts,
        ),
    )
