"""Worker loop that claims tasks and runs codex exec."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import json
import os
import re
import shutil
import threading
import time
import tempfile
from pathlib import Path

from .codex_exec import (
    CodexExecRateLimitError,
    CodexExecTimeoutError,
    extract_retry_after_seconds,
    is_auth_failure_message,
    is_rate_limit_message,
    run_codex_exec,
)
from .db import (
    begin_task_execution,
    get_run,
    get_run_throttle_state,
    infer_run_desired_concurrency,
    lease_one_task,
    mark_task_canceled,
    mark_task_done,
    mark_task_error,
    open_db,
    requeue_task,
    requeue_task_after_rate_limit,
    run_has_waitable_work,
    run_status,
    upsert_run_throttle_state,
)
from .execution_context import prepare_execution_context
from .forensics import FailureForensicsRequest, capture_failure_forensics
from .heads_up import (
    DEFAULT_HEADS_UP_MAX_TIPS,
    append_heads_up_block,
    compute_input_signature,
    parse_heads_up_enabled,
    parse_heads_up_max_tips,
    record_tip_usage,
    select_heads_up_tips,
)
from .lease_heartbeat import LeaseContext, LeaseHeartbeatSession
from .paths import db_path_for_data_dir, resolve_farm_root
from .pipeline_spec import load_pipelines, render_prompt_template
from .rate_limit_policy import apply_rate_limit, apply_success, is_cooldown_active, should_give_up
from .recipeimport_benchmark_eval import (
    RECIPEIMPORT_BENCHMARK_MODE,
    PreparedLineLabelBenchmarkArtifacts,
    prepare_line_label_benchmark_artifacts,
    write_line_label_benchmark_artifacts,
)
from .run_assets import FrozenExecutionSpec, FrozenRunAssetsError, load_frozen_run_assets
from .schema_utils import SchemaValidationError, validate_json_file_against_schema


CODEX_REASONING_EFFORT_VALUES = {"none", "minimal", "low", "medium", "high", "xhigh"}
RECIPEIMPORT_BENCHMARK_MODE_VALUES = {RECIPEIMPORT_BENCHMARK_MODE}
_INVALID_JSON_SCHEMA_PATTERN = re.compile(r"invalid_json_schema", re.IGNORECASE)
_CONTENT_FILTER_PATTERN = re.compile(r"\bcontent_filter\b", re.IGNORECASE)


def _extract_invalid_json_schema_message(text: str) -> str | None:
    for line in (text or "").splitlines():
        if _INVALID_JSON_SCHEMA_PATTERN.search(line):
            return line.strip()
    return None


def _is_content_filter_message(text: str) -> bool:
    if not text.strip():
        return False
    return _CONTENT_FILTER_PATTERN.search(text) is not None


class WorkerRuntimeFailure(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        failure_category: str,
        stdout_tail: str | None = None,
        stderr_tail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_category = failure_category
        self.stdout_tail = stdout_tail
        self.stderr_tail = stderr_tail


def _trim_error(text: str, limit: int = 1800) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _append_retry_context(
    *,
    prompt: str,
    effective_execution_attempts: int,
    previous_error: object,
) -> tuple[str, dict[str, object]]:
    retry_meta: dict[str, object] = {
        "retry_context_applied": False,
        "retry_previous_error": None,
    }
    if effective_execution_attempts <= 1:
        return prompt, retry_meta
    if not isinstance(previous_error, str):
        return prompt, retry_meta

    cleaned_error = previous_error.strip()
    if not cleaned_error:
        return prompt, retry_meta

    retry_error = _trim_error(cleaned_error, limit=900)
    retry_meta["retry_context_applied"] = True
    retry_meta["retry_previous_error"] = retry_error
    return (
        f"{prompt.rstrip()}\n\n"
        "Retry context:\n"
        f"This is effective execution attempt {effective_execution_attempts} for this input. "
        "The previous attempt failed with the following error:\n"
        f"{retry_error}\n"
        "Fix that failure mode directly and return only JSON matching the configured output schema.\n"
    ), retry_meta


def _heartbeat_interval_seconds(lease_seconds: int) -> float:
    if lease_seconds <= 0:
        return 1.0
    return max(0.2, min(10.0, lease_seconds / 3.0))


def _stage_output_path(
    run_output_dir: Path,
    *,
    task_id: str,
    lease_token: str,
    output_ext: str,
) -> Path:
    return run_output_dir / ".codex-farm-stage" / task_id / f"{lease_token}{output_ext}"


def _trace_output_path(
    run_output_dir: Path,
    *,
    task_id: str,
    lease_token: str,
) -> Path:
    return run_output_dir / ".codex-farm-traces" / task_id / f"{lease_token}.trace.json"


def _promote_staged_output_if_owner(
    conn,
    *,
    task_id: str,
    lease_token: str,
    staged_output_path: Path,
    final_output_path: Path,
    finalize_task: bool = True,
) -> bool:
    if not staged_output_path.exists():
        return False
    final_output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        owner = conn.execute(
            """
            SELECT status, lease_token
            FROM tasks
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if owner is None:
            conn.rollback()
            return False
        if str(owner["status"]) != "running" or str(owner["lease_token"] or "") != lease_token:
            conn.rollback()
            return False
        os.replace(staged_output_path, final_output_path)
        if finalize_task:
            transitioned = mark_task_done(
                conn,
                task_id=task_id,
                output_path=str(final_output_path),
                lease_token=lease_token,
                commit=False,
            )
            if not transitioned:
                conn.rollback()
                return False
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def _heads_up_telemetry_lists(tips: list[dict]) -> tuple[list[str], list[str], list[float]]:
    tip_ids: list[str] = []
    tip_texts: list[str] = []
    tip_scores: list[float] = []
    for row in tips:
        raw_tip_id = row.get("tip_id")
        if raw_tip_id:
            tip_ids.append(str(raw_tip_id))

        tip_text = str(row.get("tip_text", "")).strip()
        if tip_text:
            tip_texts.append(tip_text)

        raw_score = row.get("score")
        if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool):
            tip_scores.append(float(raw_score))
            continue
        if isinstance(raw_score, str):
            try:
                tip_scores.append(float(raw_score))
            except ValueError:
                continue
    return tip_ids, tip_texts, tip_scores


def _parse_run_config(raw: str) -> dict[str, object]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _path_from_config(value: object) -> Path | None:
    if isinstance(value, str) and value.strip():
        return Path(value).expanduser().resolve()
    return None


def _resolve_run_farm_root(
    *,
    run_config: dict[str, object],
    worker_root: Path | None,
) -> Path:
    configured = _path_from_config(run_config.get("farm_root"))
    if configured is not None:
        return resolve_farm_root(configured)
    return resolve_farm_root(worker_root)


def _resolve_workspace_root_override(
    *,
    run_config: dict[str, object],
) -> Path | None:
    configured = _path_from_config(run_config.get("workspace_root"))
    if configured is None:
        return None
    if not configured.exists() or not configured.is_dir():
        raise FileNotFoundError(
            f"workspace_root does not exist or is not a directory: {configured}"
        )
    return configured


def _resolve_model_override(
    *,
    run_config: dict[str, object],
) -> str | None:
    configured = run_config.get("codex_model")
    if not isinstance(configured, str):
        return None
    model = configured.strip()
    return model or None


def _resolve_reasoning_effort_override(
    *,
    run_config: dict[str, object],
) -> str | None:
    configured = run_config.get("codex_reasoning_effort")
    if not isinstance(configured, str):
        return None
    normalized = configured.strip().lower()
    if not normalized:
        return None
    if normalized not in CODEX_REASONING_EFFORT_VALUES:
        raise ValueError(
            "Invalid codex_reasoning_effort in run config. "
            f"Expected one of: {', '.join(sorted(CODEX_REASONING_EFFORT_VALUES))}"
        )
    return normalized


def _resolve_output_schema_override(
    *,
    run_config: dict[str, object],
) -> Path | None:
    configured = _path_from_config(run_config.get("output_schema_path_override"))
    if configured is None:
        return None
    if not configured.exists() or not configured.is_file():
        raise FileNotFoundError(
            "output_schema_path_override does not exist or is not a file: "
            f"{configured}"
        )
    return configured


def _resolve_recipeimport_benchmark_mode(
    *,
    run_config: dict[str, object],
) -> str | None:
    configured = run_config.get("recipeimport_benchmark_mode")
    if configured is None:
        return None
    if not isinstance(configured, str):
        raise ValueError("recipeimport_benchmark_mode in run config must be a string.")
    normalized = configured.strip().lower()
    if not normalized:
        return None
    if normalized not in RECIPEIMPORT_BENCHMARK_MODE_VALUES:
        allowed = ", ".join(sorted(RECIPEIMPORT_BENCHMARK_MODE_VALUES))
        raise ValueError(
            "Invalid recipeimport_benchmark_mode in run config. "
            f"Expected one of: {allowed}"
        )
    return normalized


def _resolve_recipeimport_benchmark_debug(
    *,
    run_config: dict[str, object],
) -> bool:
    configured = run_config.get("recipeimport_benchmark_debug")
    if isinstance(configured, bool):
        return configured
    if configured is None:
        return False
    if isinstance(configured, str):
        normalized = configured.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    raise ValueError(
        "recipeimport_benchmark_debug in run config must be boolean or boolean-like string."
    )


def _resolve_task_cd_dir(
    *,
    codex_cd_mode: str,
    run: dict[str, object],
    input_path: Path,
    farm_root: Path,
    workspace_root_override: Path | None,
) -> Path:
    if workspace_root_override is not None:
        cd_dir = workspace_root_override
    elif codex_cd_mode == "asset_root":
        cd_dir = farm_root
    elif codex_cd_mode == "input_dir":
        cd_dir = Path(str(run["input_dir"])).expanduser().resolve()
    else:
        cd_dir = input_path.parent

    if not cd_dir.exists() or not cd_dir.is_dir():
        raise FileNotFoundError(f"Computed codex --cd directory does not exist: {cd_dir}")
    return cd_dir


def _run_is_canceling(conn, run_id: str) -> bool:
    run = get_run(conn, run_id)
    return str(run.get("control_state", "active")) in {"cancel_requested", "canceled"}


def worker_loop(
    *,
    data_dir: Path,
    worker_id: str,
    run_id: str | None,
    lease_seconds: int,
    max_attempts: int,
    poll_seconds: float,
    once: bool,
    farm_root: Path | None = None,
    stop_event: threading.Event | None = None,
    warning_callback: Callable[[str], None] | None = None,
) -> int:
    pipeline_cache: dict[Path, dict] = {}
    frozen_execution_cache: dict[str, FrozenExecutionSpec] = {}

    conn = open_db(db_path_for_data_dir(data_dir))
    usage_log_csv = data_dir.resolve() / "codex_exec_activity.csv"
    exit_code = 0
    active_heartbeat: LeaseHeartbeatSession | None = None

    def _stop_active_heartbeat() -> None:
        nonlocal active_heartbeat
        if active_heartbeat is None:
            return
        active_heartbeat.stop()
        if warning_callback is not None:
            if active_heartbeat.lost_ownership:
                warning_callback(
                    (
                        "Task lease heartbeat lost ownership; "
                        "skipping stale finalization for this worker attempt."
                    )
                )
            elif active_heartbeat.last_error:
                warning_callback(
                    f"Task lease heartbeat encountered transient DB errors: {active_heartbeat.last_error}"
                )
        active_heartbeat = None

    while True:
        _stop_active_heartbeat()
        if stop_event is not None and stop_event.is_set():
            return exit_code

        task = lease_one_task(
            conn,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            run_id=run_id,
        )

        if task is None:
            if once:
                if run_id is None:
                    return exit_code
                try:
                    should_wait, cooldown_remaining, _reason = run_has_waitable_work(
                        conn,
                        run_id=run_id,
                        now=time.time(),
                    )
                except KeyError:
                    return exit_code
                if not should_wait:
                    return exit_code
                sleep_for = poll_seconds
                if cooldown_remaining is not None and cooldown_remaining > 0:
                    sleep_for = min(poll_seconds, max(0.01, cooldown_remaining))
                time.sleep(sleep_for)
                continue
            if run_id is not None:
                try:
                    scoped_status = run_status(conn, run_id=run_id)
                except KeyError:
                    return exit_code
                if scoped_status["status"] in {"done", "error", "canceled"}:
                    return exit_code
            time.sleep(poll_seconds)
            continue

        task_lease_token = task.get("lease_token")
        if not isinstance(task_lease_token, str) or not task_lease_token:
            task_lease_token = None
        else:
            active_heartbeat = LeaseHeartbeatSession(
                context=LeaseContext(
                    db_path=db_path_for_data_dir(data_dir),
                    task_id=str(task["task_id"]),
                    lease_token=task_lease_token,
                    lease_seconds=lease_seconds,
                    interval_seconds=_heartbeat_interval_seconds(lease_seconds),
                )
            )
            active_heartbeat.start()

        run = get_run(conn, task["run_id"])
        pipeline_id = str(run["pipeline_id"])
        run_config = _parse_run_config(run.get("config_json", "{}"))

        task_input_path: Path | None = None
        raw_input_path = task.get("input_path")
        if isinstance(raw_input_path, str) and raw_input_path.strip():
            task_input_path = Path(raw_input_path).expanduser().resolve()
        raw_input_hash = task.get("input_hash")
        task_input_hash = raw_input_hash if isinstance(raw_input_hash, str) else None
        raw_rel_output_path = task.get("rel_output_path")
        task_rel_output_path = (
            raw_rel_output_path
            if isinstance(raw_rel_output_path, str) and raw_rel_output_path.strip()
            else None
        )
        lease_claim_index = int(task.get("attempts") or 0)
        execution_attempts_before = int(task.get("execution_attempts") or 0)
        rate_limit_count = int(task.get("rate_limit_count") or 0)
        effective_execution_attempts_before = max(0, execution_attempts_before - rate_limit_count)
        previous_error = task.get("previous_error")
        previous_error_text = (
            previous_error.strip()
            if isinstance(previous_error, str) and previous_error.strip()
            else None
        )

        def capture_worker_forensics(
            *,
            terminal: bool,
            failure_stage: str,
            failure_category: str,
            error_message_full: str,
            error_message_summary: str,
            prompt_text: str | None,
            schema_path: Path | None,
            output_path_forensics: Path | None,
            stdout_tail: str | None,
            stderr_tail: str | None,
            runtime_context: dict[str, object],
        ) -> None:
            capture_failure_forensics(
                conn,
                request=FailureForensicsRequest(
                    data_dir=data_dir,
                    source="worker",
                    run_id=str(task["run_id"]),
                    task_id=str(task["task_id"]),
                    pipeline_id=pipeline_id,
                    attempt_index=lease_claim_index,
                    terminal=terminal,
                    input_path=task_input_path,
                    input_hash=task_input_hash,
                    rel_output_path=task_rel_output_path,
                    worker_id=worker_id,
                    failure_stage=failure_stage,
                    failure_category=failure_category,
                    error_message_full=error_message_full,
                    error_message_summary=error_message_summary,
                    prompt_text=prompt_text,
                    schema_path=schema_path,
                    output_path=output_path_forensics,
                    stdout_tail=stdout_tail,
                    stderr_tail=stderr_tail,
                    runtime_context=runtime_context,
                    previous_error=previous_error_text,
                ),
            )

        if stop_event is not None and stop_event.is_set():
            requeue_task(
                conn,
                task_id=task["task_id"],
                error="Halting queued task after HTTP 429 rate-limit warning from another worker.",
                lease_token=task_lease_token,
            )
            _stop_active_heartbeat()
            return exit_code

        if effective_execution_attempts_before >= max_attempts:
            error_full = f"Max attempts exceeded ({max_attempts}) before processing"
            error_summary = _trim_error(error_full)
            capture_worker_forensics(
                terminal=True,
                failure_stage="preflight",
                failure_category="config_error",
                error_message_full=error_full,
                error_message_summary=error_summary,
                prompt_text=None,
                schema_path=None,
                output_path_forensics=None,
                stdout_tail=None,
                stderr_tail=None,
                runtime_context={
                    "branch": "attempt_budget_guard",
                    "max_attempts": max_attempts,
                    "lease_claim_index": lease_claim_index,
                    "execution_attempts_before": execution_attempts_before,
                    "effective_execution_attempts_before": effective_execution_attempts_before,
                },
            )
            transitioned = mark_task_error(
                conn,
                task_id=task["task_id"],
                error=error_summary,
                lease_token=task_lease_token,
            )
            if transitioned:
                exit_code = 1
            continue

        try:
            run_farm_root = _resolve_run_farm_root(
                run_config=run_config,
                worker_root=farm_root,
            )
            workspace_root_override = _resolve_workspace_root_override(run_config=run_config)
            recipeimport_benchmark_mode = _resolve_recipeimport_benchmark_mode(
                run_config=run_config
            )
            recipeimport_benchmark_debug = _resolve_recipeimport_benchmark_debug(
                run_config=run_config
            )
        except (FileNotFoundError, ValueError) as exc:
            error_full = str(exc)
            error_summary = _trim_error(error_full)
            capture_worker_forensics(
                terminal=True,
                failure_stage="preflight",
                failure_category="config_error",
                error_message_full=error_full,
                error_message_summary=error_summary,
                prompt_text=None,
                schema_path=None,
                output_path_forensics=None,
                stdout_tail=None,
                stderr_tail=None,
                runtime_context={
                    "branch": "run_config",
                    "run_config": run_config,
                },
            )
            transitioned = mark_task_error(
                conn,
                task_id=task["task_id"],
                error=error_summary,
                lease_token=task_lease_token,
            )
            if transitioned:
                exit_code = 1
            continue

        prompt_template_path: Path
        codex_sandbox: str
        codex_ask_for_approval: str
        codex_web_search: str
        codex_timeout_seconds: int
        codex_cd_mode: str
        codex_execution_context: str
        selected_model: str
        selected_effort: str | None
        selected_output_schema: Path
        selected_output_schema_logical: Path
        selected_codex_home = _path_from_config(run_config.get("codex_home_path"))
        prepared_benchmark_artifacts: PreparedLineLabelBenchmarkArtifacts | None = None
        has_frozen_assets = "frozen_assets" in run_config
        frozen_assets_config = run_config.get("frozen_assets")
        if has_frozen_assets:
            if not isinstance(frozen_assets_config, dict):
                error_full = (
                    f"Frozen run assets are missing or invalid for run {run['run_id']}; "
                    "this run requires frozen assets and cannot fall back to live pipeline files. "
                    "Details: frozen_assets config must be an object"
                )
                error_summary = _trim_error(error_full)
                capture_worker_forensics(
                    terminal=True,
                    failure_stage="preflight",
                    failure_category="config_error",
                    error_message_full=error_full,
                    error_message_summary=error_summary,
                    prompt_text=None,
                    schema_path=None,
                    output_path_forensics=None,
                    stdout_tail=None,
                    stderr_tail=None,
                    runtime_context={
                        "branch": "frozen_assets_config",
                        "has_frozen_assets": True,
                    },
                )
                transitioned = mark_task_error(
                    conn,
                    task_id=task["task_id"],
                    error=error_summary,
                    lease_token=task_lease_token,
                )
                if transitioned:
                    exit_code = 1
                continue
            try:
                run_key = str(run["run_id"])
                if run_key not in frozen_execution_cache:
                    _, frozen_spec = load_frozen_run_assets(
                        data_dir=data_dir,
                        frozen_assets_config=frozen_assets_config,
                    )
                    frozen_execution_cache[run_key] = frozen_spec
                frozen_spec = frozen_execution_cache[run_key]
            except FrozenRunAssetsError as exc:
                error_full = (
                    f"Frozen run assets are missing or invalid for run {run['run_id']}; "
                    "this run requires frozen assets and cannot fall back to live pipeline files. "
                    f"Details: {exc}"
                )
                error_summary = _trim_error(error_full)
                capture_worker_forensics(
                    terminal=True,
                    failure_stage="preflight",
                    failure_category="config_error",
                    error_message_full=error_full,
                    error_message_summary=error_summary,
                    prompt_text=None,
                    schema_path=None,
                    output_path_forensics=None,
                    stdout_tail=None,
                    stderr_tail=None,
                    runtime_context={
                        "branch": "frozen_assets_load",
                        "has_frozen_assets": True,
                    },
                )
                transitioned = mark_task_error(
                    conn,
                    task_id=task["task_id"],
                    error=error_summary,
                    lease_token=task_lease_token,
                )
                if transitioned:
                    exit_code = 1
                continue

            if frozen_spec.pipeline_id != pipeline_id:
                error_full = (
                    "Frozen run assets pipeline mismatch: "
                    f"run={pipeline_id}, frozen={frozen_spec.pipeline_id}"
                )
                error_summary = _trim_error(error_full)
                capture_worker_forensics(
                    terminal=True,
                    failure_stage="preflight",
                    failure_category="config_error",
                    error_message_full=error_full,
                    error_message_summary=error_summary,
                    prompt_text=None,
                    schema_path=None,
                    output_path_forensics=None,
                    stdout_tail=None,
                    stderr_tail=None,
                    runtime_context={
                        "branch": "frozen_assets_pipeline_mismatch",
                        "frozen_pipeline_id": frozen_spec.pipeline_id,
                    },
                )
                transitioned = mark_task_error(
                    conn,
                    task_id=task["task_id"],
                    error=error_summary,
                    lease_token=task_lease_token,
                )
                if transitioned:
                    exit_code = 1
                continue

            prompt_template_path = frozen_spec.prompt_template_path
            codex_sandbox = frozen_spec.codex_sandbox
            codex_ask_for_approval = frozen_spec.codex_ask_for_approval
            codex_web_search = frozen_spec.codex_web_search
            codex_timeout_seconds = frozen_spec.codex_timeout_seconds
            codex_cd_mode = frozen_spec.codex_cd_mode
            codex_execution_context = frozen_spec.codex_execution_context
            selected_model = frozen_spec.codex_model
            selected_effort = frozen_spec.codex_reasoning_effort
            selected_output_schema = frozen_spec.output_schema_path
            selected_output_schema_logical = frozen_spec.logical_output_schema_source_path
        else:
            try:
                if run_farm_root not in pipeline_cache:
                    pipeline_cache[run_farm_root] = load_pipelines(run_farm_root / "pipelines")
                pipelines = pipeline_cache[run_farm_root]
                spec = pipelines.get(pipeline_id)
                if spec is None:
                    error_full = f"Unknown pipeline_id: {pipeline_id}"
                    error_summary = _trim_error(error_full)
                    capture_worker_forensics(
                        terminal=True,
                        failure_stage="preflight",
                        failure_category="config_error",
                        error_message_full=error_full,
                        error_message_summary=error_summary,
                        prompt_text=None,
                        schema_path=None,
                        output_path_forensics=None,
                        stdout_tail=None,
                        stderr_tail=None,
                        runtime_context={
                            "branch": "pipeline_lookup",
                            "run_farm_root": str(run_farm_root),
                        },
                    )
                    transitioned = mark_task_error(
                        conn,
                        task_id=task["task_id"],
                        error=error_summary,
                        lease_token=task_lease_token,
                    )
                    if transitioned:
                        exit_code = 1
                    continue
                model_override = _resolve_model_override(run_config=run_config)
                selected_model = model_override if model_override is not None else spec.codex_model
                effort_override = _resolve_reasoning_effort_override(run_config=run_config)
                selected_effort = (
                    effort_override
                    if effort_override is not None
                    else spec.codex_reasoning_effort
                )
                output_schema_override = _resolve_output_schema_override(run_config=run_config)
                selected_output_schema = (
                    output_schema_override
                    if output_schema_override is not None
                    else spec.output_schema_path
                )
                selected_output_schema_logical = selected_output_schema
                prompt_template_path = spec.prompt_template_path
                codex_sandbox = spec.codex_sandbox
                codex_ask_for_approval = spec.codex_ask_for_approval
                codex_web_search = spec.codex_web_search
                codex_timeout_seconds = spec.codex_timeout_seconds
                codex_cd_mode = spec.codex_cd_mode
                codex_execution_context = spec.codex_execution_context
            except (FileNotFoundError, ValueError) as exc:
                error_full = str(exc)
                error_summary = _trim_error(error_full)
                capture_worker_forensics(
                    terminal=True,
                    failure_stage="preflight",
                    failure_category="config_error",
                    error_message_full=error_full,
                    error_message_summary=error_summary,
                    prompt_text=None,
                    schema_path=None,
                    output_path_forensics=None,
                    stdout_tail=None,
                    stderr_tail=None,
                    runtime_context={
                        "branch": "pipeline_resolution",
                        "run_farm_root": str(run_farm_root),
                    },
                )
                transitioned = mark_task_error(
                    conn,
                    task_id=task["task_id"],
                    error=error_summary,
                    lease_token=task_lease_token,
                )
                if transitioned:
                    exit_code = 1
                continue

        heads_up_enabled = parse_heads_up_enabled(
            run_config.get("heads_up_enabled"),
            default=False,
        )
        heads_up_max_tips = parse_heads_up_max_tips(
            run_config.get("heads_up_max_tips"),
            default=DEFAULT_HEADS_UP_MAX_TIPS,
        )

        input_path = Path(task["input_path"]).resolve()
        final_output_path = Path(run["output_dir"]).resolve() / task["rel_output_path"]
        if task_lease_token is None:
            error_full = "Task lease token missing; cannot safely execute task."
            error_summary = _trim_error(error_full)
            capture_worker_forensics(
                terminal=True,
                failure_stage="preflight",
                failure_category="config_error",
                error_message_full=error_full,
                error_message_summary=error_summary,
                prompt_text=None,
                schema_path=selected_output_schema,
                output_path_forensics=final_output_path,
                stdout_tail=None,
                stderr_tail=None,
                runtime_context={"branch": "missing_lease_token"},
            )
            transitioned = mark_task_error(
                conn,
                task_id=task["task_id"],
                error=error_summary,
                lease_token=task_lease_token,
            )
            if transitioned:
                exit_code = 1
            continue
        staged_output_path = _stage_output_path(
            Path(run["output_dir"]).resolve(),
            task_id=str(task["task_id"]),
            lease_token=task_lease_token,
            output_ext="".join(final_output_path.suffixes),
        )
        trace_output_path = _trace_output_path(
            Path(run["output_dir"]).resolve(),
            task_id=str(task["task_id"]),
            lease_token=task_lease_token,
        )
        staged_output_path.unlink(missing_ok=True)
        try:
            cd_dir = _resolve_task_cd_dir(
                codex_cd_mode=codex_cd_mode,
                run=run,
                input_path=input_path,
                farm_root=run_farm_root,
                workspace_root_override=workspace_root_override,
            )
        except FileNotFoundError as exc:
            error_full = str(exc)
            error_summary = _trim_error(error_full)
            capture_worker_forensics(
                terminal=True,
                failure_stage="preflight",
                failure_category="config_error",
                error_message_full=error_full,
                error_message_summary=error_summary,
                prompt_text=None,
                schema_path=selected_output_schema,
                output_path_forensics=final_output_path,
                stdout_tail=None,
                stderr_tail=None,
                runtime_context={
                    "branch": "cd_dir_resolution",
                    "codex_cd_mode": codex_cd_mode,
                    "codex_execution_context": codex_execution_context,
                    "run_farm_root": str(run_farm_root),
                    "codex_home_path": (
                        str(selected_codex_home) if selected_codex_home is not None else None
                    ),
                },
            )
            transitioned = mark_task_error(
                conn,
                task_id=task["task_id"],
                error=error_summary,
                lease_token=task_lease_token,
            )
            if transitioned:
                exit_code = 1
            continue
        prompt = render_prompt_template(prompt_template_path, input_path)
        input_signature = ""
        applied_heads_up_tips: list[dict] = []
        if heads_up_enabled:
            input_signature = compute_input_signature(input_path)
            applied_heads_up_tips = select_heads_up_tips(
                conn,
                pipeline_id=pipeline_id,
                input_signature=input_signature,
                limit=heads_up_max_tips,
            )
            prompt = append_heads_up_block(prompt, applied_heads_up_tips)
        execution_attempt_index = begin_task_execution(
            conn,
            task_id=str(task["task_id"]),
            lease_token=task_lease_token,
        )
        if execution_attempt_index is None:
            staged_output_path.unlink(missing_ok=True)
            continue
        effective_execution_attempt_index = max(
            0,
            execution_attempt_index - rate_limit_count,
        )
        prompt, retry_meta = _append_retry_context(
            prompt=prompt,
            effective_execution_attempts=effective_execution_attempt_index,
            previous_error=task.get("previous_error"),
        )
        applied_tip_ids, applied_tip_texts, applied_tip_scores = _heads_up_telemetry_lists(
            applied_heads_up_tips
        )
        codex_stdout_tail: str | None = None
        codex_stderr_tail: str | None = None
        codex_exit_code: int | None = None
        raw_benchmark_output_text: str | None = None
        prepared_execution = None

        try:
            prepared_execution = prepare_execution_context(
                execution_context=codex_execution_context,
                project_cd_dir=cd_dir,
                data_dir=data_dir,
                source="worker",
                codex_home_path=selected_codex_home,
                run_id=str(task["run_id"]),
                task_id=str(task["task_id"]),
                lease_token=task_lease_token,
            )
            usage_context = {
                "source": "worker",
                "pipeline_id": pipeline_id,
                "run_id": task["run_id"],
                "task_id": task["task_id"],
                "worker_id": worker_id,
                "input_path": str(input_path),
                "recipeimport_benchmark_mode": recipeimport_benchmark_mode,
                "recipeimport_benchmark_debug": recipeimport_benchmark_debug,
                "heads_up_applied": bool(applied_tip_texts),
                "heads_up_tip_count": len(applied_tip_texts),
                "heads_up_input_signature": input_signature or None,
                "heads_up_tip_ids_json": json.dumps(applied_tip_ids, sort_keys=True),
                "heads_up_tip_texts_json": json.dumps(applied_tip_texts, sort_keys=True),
                "heads_up_tip_scores_json": json.dumps(applied_tip_scores, sort_keys=True),
                "attempt_index": lease_claim_index,
                "lease_claim_index": lease_claim_index,
                "execution_attempt_index": execution_attempt_index,
                "execution_context": codex_execution_context,
                "codex_home_path": (
                    str(selected_codex_home) if selected_codex_home is not None else None
                ),
                **retry_meta,
            }
            result = run_codex_exec(
                cd_dir=prepared_execution.cd_dir,
                prompt=prompt,
                model=selected_model,
                sandbox=codex_sandbox,
                ask_for_approval=codex_ask_for_approval,
                web_search=codex_web_search,
                reasoning_effort=selected_effort,
                output_schema=selected_output_schema,
                output_schema_logical_path=selected_output_schema_logical,
                output_path=staged_output_path,
                timeout_seconds=codex_timeout_seconds,
                env_overrides=prepared_execution.env_overrides,
                usage_log_csv=usage_log_csv,
                usage_context=usage_context,
                trace_output_path=trace_output_path,
            )
            codex_stdout_tail = result.stdout_tail
            codex_stderr_tail = result.stderr_tail
            codex_exit_code = result.exit_code
            stderr = "no stderr"
            if not result.ok:
                combined_tails = "\n".join(
                    part
                    for part in (result.stderr_tail, result.stdout_tail)
                    if isinstance(part, str) and part.strip()
                ).strip()
                stderr = combined_tails or result.stderr_tail or "no stderr"
                if is_auth_failure_message(stderr):
                    raise WorkerRuntimeFailure(
                        (
                            "codex auth failed: run `codex` once and sign in with ChatGPT, "
                            "then retry this run. "
                            f"codex exit={result.exit_code}; details: {stderr}"
                        ),
                        failure_category="auth_failure",
                        stdout_tail=result.stdout_tail,
                        stderr_tail=result.stderr_tail,
                    )
                invalid_schema_message = _extract_invalid_json_schema_message(stderr)
                if invalid_schema_message:
                    raise WorkerRuntimeFailure(
                        (
                            f"codex invalid_json_schema returned from codex API (exit={result.exit_code}): "
                            f"{invalid_schema_message}"
                        ),
                        failure_category="invalid_json_schema",
                        stdout_tail=result.stdout_tail,
                        stderr_tail=result.stderr_tail,
                    )
                if _is_content_filter_message(stderr):
                    raise WorkerRuntimeFailure(
                        (
                            f"codex content_filter blocked response stream (exit={result.exit_code}): "
                            f"{stderr}"
                        ),
                        failure_category="content_filter",
                        stdout_tail=result.stdout_tail,
                        stderr_tail=result.stderr_tail,
                    )
                if is_rate_limit_message(stderr):
                    retry_after_seconds = extract_retry_after_seconds(stderr)
                    raise CodexExecRateLimitError(
                        (
                            "WARNING: codex rate limit (HTTP 429) detected; "
                                "entering adaptive cooldown. "
                                f"codex exit={result.exit_code}; details: {stderr}"
                            ),
                            retry_after_seconds=retry_after_seconds,
                            stderr_tail=stderr,
                        )
                failure_category = (
                    "runtime_zero_no_payload"
                    if result.exit_code == 0
                    else "runtime_nonzero_no_payload"
                )
                raise WorkerRuntimeFailure(
                    f"codex exec failed (exit={result.exit_code}): {stderr}",
                    failure_category=failure_category,
                    stdout_tail=result.stdout_tail,
                    stderr_tail=result.stderr_tail,
                )

            if recipeimport_benchmark_mode == RECIPEIMPORT_BENCHMARK_MODE:
                tmp_payload_path: Path | None = None
                try:
                    raw_benchmark_output_text = staged_output_path.read_text(encoding="utf-8")
                    prepared_benchmark_artifacts = prepare_line_label_benchmark_artifacts(
                        input_path=input_path,
                        output_path=staged_output_path,
                    )
                    canonical_payload = {
                        "line_predictions": [
                            {
                                "line_index": row.line_index,
                                "label": row.label,
                                "confidence": row.confidence,
                                "evidence_line_indices": list(row.evidence_line_indices),
                                "reasoning_tags": list(row.reasoning_tags),
                            }
                            for row in prepared_benchmark_artifacts.calibrated_predictions
                        ],
                    }
                    staged_output_path.write_text(
                        json.dumps(canonical_payload, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    with tempfile.NamedTemporaryFile(
                        "w",
                        encoding="utf-8",
                        suffix=".json",
                        delete=False,
                    ) as handle:
                        tmp_payload_path = Path(handle.name)
                        json.dump(
                            canonical_payload,
                            handle,
                        )
                        handle.flush()
                    validate_json_file_against_schema(
                        json_path=tmp_payload_path,
                        schema_path=selected_output_schema,
                    )
                except ValueError as exc:
                    raise WorkerRuntimeFailure(
                        (
                            "recipeimport benchmark mode expects line-label benchmark payloads; "
                            f"failed to parse benchmark artifacts: {exc}"
                        ),
                        failure_category="benchmark_contract_error",
                        stdout_tail=codex_stdout_tail,
                        stderr_tail=codex_stderr_tail,
                    ) from exc
                finally:
                    if tmp_payload_path is not None:
                        tmp_payload_path.unlink(missing_ok=True)
            else:
                validate_json_file_against_schema(
                    json_path=staged_output_path,
                    schema_path=selected_output_schema,
                )
            transitioned = _promote_staged_output_if_owner(
                conn,
                task_id=task["task_id"],
                lease_token=task_lease_token,
                staged_output_path=staged_output_path,
                final_output_path=final_output_path,
                finalize_task=recipeimport_benchmark_mode != RECIPEIMPORT_BENCHMARK_MODE,
            )
            if not transitioned:
                staged_output_path.unlink(missing_ok=True)
                continue
            if (
                recipeimport_benchmark_mode == RECIPEIMPORT_BENCHMARK_MODE
            ):
                if prepared_benchmark_artifacts is None:
                    raise WorkerRuntimeFailure(
                        (
                            "recipeimport benchmark mode was enabled but benchmark artifacts "
                            "were not prepared before output promotion."
                        ),
                        failure_category="benchmark_contract_error",
                        stdout_tail=codex_stdout_tail,
                        stderr_tail=codex_stderr_tail,
                    )
                try:
                    write_line_label_benchmark_artifacts(
                        run_output_dir=Path(run["output_dir"]).resolve(),
                        run_id=str(task["run_id"]),
                        task_id=str(task["task_id"]),
                        pipeline_id=pipeline_id,
                        input_path=input_path,
                        output_path=final_output_path,
                        output_schema_path=selected_output_schema,
                        output_schema_logical_path=selected_output_schema_logical,
                        selected_model=selected_model,
                        prompt_text=prompt,
                        prepared=prepared_benchmark_artifacts,
                        debug_enabled=recipeimport_benchmark_debug,
                        raw_model_output_text=raw_benchmark_output_text,
                        stdout_tail=codex_stdout_tail,
                        stderr_tail=codex_stderr_tail,
                    )
                except Exception as exc:
                    raise WorkerRuntimeFailure(
                        (
                            "failed to persist recipeimport benchmark artifacts "
                            f"for task {task['task_id']}: {exc}"
                        ),
                        failure_category="benchmark_artifact_write_error",
                        stdout_tail=codex_stdout_tail,
                        stderr_tail=codex_stderr_tail,
                    ) from exc
                transitioned = mark_task_done(
                    conn,
                    task_id=task["task_id"],
                    output_path=str(final_output_path),
                    lease_token=task_lease_token,
                )
                if not transitioned:
                    continue
            throttle_before = get_run_throttle_state(conn, str(task["run_id"]))
            if throttle_before is not None:
                now_epoch = time.time()
                recovered = apply_success(throttle_before, now=now_epoch)
                if recovered is not None:
                    upsert_run_throttle_state(conn, state=recovered)
                    if warning_callback is not None:
                        was_cooldown = is_cooldown_active(throttle_before, now=now_epoch)
                        now_cooldown = is_cooldown_active(recovered, now=now_epoch)
                        if was_cooldown and not now_cooldown:
                            warning_callback(
                                (
                                    f"Resuming run {task['run_id']}; "
                                    f"effective concurrency "
                                    f"{recovered.concurrency_limit}/{recovered.desired_concurrency}"
                                )
                            )
                        elif recovered.concurrency_limit > throttle_before.concurrency_limit:
                            warning_callback(
                                (
                                    f"Recovered run {task['run_id']}; "
                                    f"effective concurrency "
                                    f"{throttle_before.concurrency_limit} -> "
                                    f"{recovered.concurrency_limit}/"
                                    f"{recovered.desired_concurrency}"
                                )
                            )
            if applied_tip_ids:
                record_tip_usage(
                    conn,
                    run_id=str(task["run_id"]),
                    task_id=str(task["task_id"]),
                    tip_ids=applied_tip_ids,
                    outcome="done",
                )

        except CodexExecRateLimitError as exc:
            error_full = str(exc)
            error_message = _trim_error(error_full)
            run_id_value = str(task["run_id"])
            now_epoch = time.time()
            throttle_before = get_run_throttle_state(conn, run_id_value)
            desired_concurrency = (
                throttle_before.desired_concurrency
                if throttle_before is not None
                else infer_run_desired_concurrency(conn, run_id=run_id_value)
            )
            throttle_after = apply_rate_limit(
                throttle_before,
                run_id=run_id_value,
                desired_concurrency=desired_concurrency,
                now=now_epoch,
                retry_after_seconds=exc.retry_after_seconds,
            )
            throttle_after = replace(
                throttle_after,
                last_rate_limit_error=error_message,
            )
            give_up = should_give_up(throttle_after)
            capture_worker_forensics(
                terminal=give_up,
                failure_stage="codex_exec",
                failure_category="rate_limit",
                error_message_full=error_full,
                error_message_summary=error_message,
                prompt_text=prompt,
                schema_path=selected_output_schema,
                output_path_forensics=staged_output_path,
                stdout_tail=codex_stdout_tail,
                stderr_tail=codex_stderr_tail,
                runtime_context={
                    "branch": "rate_limit",
                    "codex_exit_code": codex_exit_code,
                    "codex_model": selected_model,
                    "codex_reasoning_effort": selected_effort,
                    "codex_cd_mode": codex_cd_mode,
                    "codex_execution_context": codex_execution_context,
                    "recipeimport_benchmark_mode": recipeimport_benchmark_mode,
                    "recipeimport_benchmark_debug": recipeimport_benchmark_debug,
                    "project_cd_dir": str(cd_dir),
                    "cd_dir": str(
                        prepared_execution.cd_dir if prepared_execution is not None else cd_dir
                    ),
                    "codex_home_path": (
                        str(selected_codex_home) if selected_codex_home is not None else None
                    ),
                    "retry_after_seconds": exc.retry_after_seconds,
                    "cooldown_seconds": throttle_after.last_cooldown_seconds,
                    "concurrency_limit": throttle_after.concurrency_limit,
                    "desired_concurrency": throttle_after.desired_concurrency,
                    "consecutive_rate_limits": throttle_after.consecutive_rate_limits,
                    "lease_claim_index": lease_claim_index,
                    "execution_attempt_index": execution_attempt_index,
                    "effective_execution_attempt_index": effective_execution_attempt_index,
                    **(
                        prepared_execution.metadata
                        if prepared_execution is not None
                        else {}
                    ),
                },
            )
            staged_output_path.unlink(missing_ok=True)
            transitioned = False
            try:
                conn.execute("BEGIN IMMEDIATE")
                transitioned = requeue_task_after_rate_limit(
                    conn,
                    task_id=task["task_id"],
                    lease_token=task_lease_token,
                    commit=False,
                )
                if transitioned:
                    upsert_run_throttle_state(conn, state=throttle_after, commit=False)
                    conn.commit()
                else:
                    conn.rollback()
            except Exception:
                conn.rollback()
                raise
            if warning_callback is not None and transitioned:
                previous_limit = (
                    throttle_before.concurrency_limit
                    if throttle_before is not None
                    else desired_concurrency
                )
                warning_callback(
                    (
                        f"Rate limit detected for run {run_id_value}; "
                        f"cooling for {throttle_after.last_cooldown_seconds}s; "
                        f"effective concurrency {previous_limit} -> "
                        f"{throttle_after.concurrency_limit}"
                    )
                )
                if give_up:
                    warning_callback(
                        (
                            f"Rate-limit recovery budget exhausted for run {run_id_value}; "
                            "stopping this invocation with queued work preserved."
                        )
                    )
            if give_up and transitioned:
                exit_code = 1
                _stop_active_heartbeat()
                return exit_code
            if give_up and not transitioned:
                exit_code = 1
                _stop_active_heartbeat()
                return exit_code
            continue

        except (CodexExecTimeoutError, SchemaValidationError, WorkerRuntimeFailure) as exc:
            if isinstance(exc, CodexExecTimeoutError):
                failure_stage = "codex_exec"
                failure_category = "timeout"
                stdout_tail = exc.stdout_tail or codex_stdout_tail
                stderr_tail = exc.stderr_tail or codex_stderr_tail
            elif isinstance(exc, SchemaValidationError):
                failure_stage = "schema_validation"
                if recipeimport_benchmark_mode == RECIPEIMPORT_BENCHMARK_MODE:
                    failure_category = "benchmark_contract_error"
                elif str(exc).startswith("Invalid JSON at "):
                    failure_category = "invalid_json"
                else:
                    failure_category = "schema_validation"
                stdout_tail = codex_stdout_tail
                stderr_tail = codex_stderr_tail
            else:
                failure_stage = "codex_exec"
                failure_category = exc.failure_category
                stdout_tail = exc.stdout_tail or codex_stdout_tail
                stderr_tail = exc.stderr_tail or codex_stderr_tail

            error_full = str(exc)
            error_message = _trim_error(error_full)
            if failure_category == "benchmark_artifact_write_error":
                final_output_path.unlink(missing_ok=True)
            terminal_failure = (
                failure_category == "auth_failure"
                or failure_category == "invalid_json_schema"
                or failure_category == "content_filter"
                or failure_category == "benchmark_contract_error"
                or effective_execution_attempt_index >= max_attempts
            )
            capture_worker_forensics(
                terminal=terminal_failure,
                failure_stage=failure_stage,
                failure_category=failure_category,
                error_message_full=error_full,
                error_message_summary=error_message,
                prompt_text=prompt,
                schema_path=selected_output_schema,
                output_path_forensics=staged_output_path,
                stdout_tail=stdout_tail,
                stderr_tail=stderr_tail,
                runtime_context={
                    "branch": "execution_failure",
                    "codex_exit_code": codex_exit_code,
                    "codex_model": selected_model,
                    "codex_reasoning_effort": selected_effort,
                    "codex_cd_mode": codex_cd_mode,
                    "codex_execution_context": codex_execution_context,
                    "recipeimport_benchmark_mode": recipeimport_benchmark_mode,
                    "recipeimport_benchmark_debug": recipeimport_benchmark_debug,
                    "project_cd_dir": str(cd_dir),
                    "cd_dir": str(
                        prepared_execution.cd_dir if prepared_execution is not None else cd_dir
                    ),
                    "codex_home_path": (
                        str(selected_codex_home) if selected_codex_home is not None else None
                    ),
                    "max_attempts": max_attempts,
                    "heads_up_enabled": heads_up_enabled,
                    "heads_up_tip_count": len(applied_tip_texts),
                    "retry_meta": retry_meta,
                    "lease_claim_index": lease_claim_index,
                    "execution_attempt_index": execution_attempt_index,
                    "effective_execution_attempt_index": effective_execution_attempt_index,
                    **(
                        prepared_execution.metadata
                        if prepared_execution is not None
                        else {}
                    ),
                },
            )
            staged_output_path.unlink(missing_ok=True)
            if terminal_failure:
                transitioned = mark_task_error(
                    conn,
                    task_id=task["task_id"],
                    error=error_message,
                    lease_token=task_lease_token,
                )
                if transitioned and applied_tip_ids:
                    record_tip_usage(
                        conn,
                        run_id=str(task["run_id"]),
                        task_id=str(task["task_id"]),
                        tip_ids=applied_tip_ids,
                        outcome="error",
                    )
                if transitioned:
                    exit_code = 1
            else:
                try:
                    canceling = _run_is_canceling(conn, str(task["run_id"]))
                except KeyError:
                    canceling = False
                if canceling:
                    mark_task_canceled(
                        conn,
                        task_id=task["task_id"],
                        lease_token=task_lease_token,
                        error=error_message,
                    )
                else:
                    requeue_task(
                        conn,
                        task_id=task["task_id"],
                        error=error_message,
                        lease_token=task_lease_token,
                    )

        except Exception as exc:  # pragma: no cover
            error_full = f"Unexpected worker error: {exc}"
            error_message = _trim_error(error_full)
            terminal_failure = effective_execution_attempt_index >= max_attempts
            capture_worker_forensics(
                terminal=terminal_failure,
                failure_stage="postprocess",
                failure_category="unexpected_exception",
                error_message_full=error_full,
                error_message_summary=error_message,
                prompt_text=prompt,
                schema_path=selected_output_schema,
                output_path_forensics=staged_output_path,
                stdout_tail=codex_stdout_tail,
                stderr_tail=codex_stderr_tail,
                runtime_context={
                    "branch": "unexpected_exception",
                    "codex_exit_code": codex_exit_code,
                    "codex_model": selected_model,
                    "codex_reasoning_effort": selected_effort,
                    "codex_cd_mode": codex_cd_mode,
                    "codex_execution_context": codex_execution_context,
                    "project_cd_dir": str(cd_dir),
                    "cd_dir": str(
                        prepared_execution.cd_dir if prepared_execution is not None else cd_dir
                    ),
                    "codex_home_path": (
                        str(selected_codex_home) if selected_codex_home is not None else None
                    ),
                    "max_attempts": max_attempts,
                    "lease_claim_index": lease_claim_index,
                    "execution_attempt_index": execution_attempt_index,
                    "effective_execution_attempt_index": effective_execution_attempt_index,
                    **(
                        prepared_execution.metadata
                        if prepared_execution is not None
                        else {}
                    ),
                },
            )
            staged_output_path.unlink(missing_ok=True)
            if effective_execution_attempt_index >= max_attempts:
                transitioned = mark_task_error(
                    conn,
                    task_id=task["task_id"],
                    error=error_message,
                    lease_token=task_lease_token,
                )
                if transitioned and applied_tip_ids:
                    record_tip_usage(
                        conn,
                        run_id=str(task["run_id"]),
                        task_id=str(task["task_id"]),
                        tip_ids=applied_tip_ids,
                        outcome="error",
                    )
                if transitioned:
                    exit_code = 1
            else:
                try:
                    canceling = _run_is_canceling(conn, str(task["run_id"]))
                except KeyError:
                    canceling = False
                if canceling:
                    mark_task_canceled(
                        conn,
                        task_id=task["task_id"],
                        lease_token=task_lease_token,
                        error=error_message,
                    )
                else:
                    requeue_task(
                        conn,
                        task_id=task["task_id"],
                        error=error_message,
                        lease_token=task_lease_token,
                    )
        finally:
            if prepared_execution is not None and prepared_execution.scratch_root is not None:
                shutil.rmtree(prepared_execution.scratch_root, ignore_errors=True)

    return exit_code
