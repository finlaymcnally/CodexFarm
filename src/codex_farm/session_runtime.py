"""Session-aware worker runtime for persistent Codex conversation reuse."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import threading
import time

from .codex_exec import (
    CodexExecRateLimitError,
    CodexExecTimeoutError,
    extract_retry_after_seconds,
    is_auth_failure_message,
    is_rate_limit_message,
    resume_codex_session,
    start_codex_session,
)
from .db import (
    begin_task_execution,
    create_worker_session,
    finish_worker_session,
    get_run,
    get_run_throttle_state,
    infer_run_desired_concurrency,
    lease_one_task,
    link_task_to_worker_session,
    mark_task_canceled,
    mark_task_done,
    mark_task_error,
    open_db,
    requeue_task,
    requeue_task_after_rate_limit,
    run_has_waitable_work,
    run_status,
    summarize_worker_sessions_for_run,
    upsert_run_throttle_state,
    update_worker_session,
)
from .execution_context import PreparedExecutionContext, prepare_execution_context
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
from .paths import db_path_for_data_dir
from .pipeline_spec import render_prompt_template
from .rate_limit_policy import apply_rate_limit, apply_success, is_cooldown_active, should_give_up
from .run_assets import FrozenExecutionSpec, FrozenRunAssetsError, load_frozen_run_assets
from .schema_utils import SchemaValidationError, validate_json_file_against_schema
from .worker import (
    WorkerRuntimeFailure,
    _append_retry_context,
    _extract_invalid_json_schema_message,
    _heads_up_telemetry_lists,
    _heartbeat_interval_seconds,
    _is_content_filter_message,
    _parse_run_config,
    _promote_staged_output_if_owner,
    _path_from_config,
    _resolve_recipeimport_benchmark_debug,
    _resolve_recipeimport_benchmark_mode,
    _resolve_run_farm_root,
    _resolve_task_cd_dir,
    _resolve_workspace_root_override,
    _run_is_canceling,
    _stage_output_path,
    _trim_error,
)


@dataclass
class WorkerSessionState:
    session_row_id: int
    run_id: str
    worker_id: str
    runtime_mode: str
    status: str
    resume_key: str | None
    thread_id: str | None
    turn_count: int
    task_count: int
    started_at: str
    current_task_id: str | None
    project_cd_dir: Path
    prepared_execution: PreparedExecutionContext
    codex_home_path: Path | None


def _session_dir(run_output_dir: Path, session_row_id: int) -> Path:
    return run_output_dir / ".codex-farm-sessions" / str(session_row_id)


def _session_summary_path(run_output_dir: Path, session_row_id: int) -> Path:
    return _session_dir(run_output_dir, session_row_id) / "session.json"


def _session_turn_trace_path(
    run_output_dir: Path,
    *,
    session_row_id: int,
    turn_index: int,
) -> Path:
    return _session_dir(run_output_dir, session_row_id) / "turns" / f"{turn_index}.trace.json"


def _render_session_task_turn(
    *,
    template_text: str,
    task_id: str,
    session_task_index: int,
    task_prompt: str,
) -> str:
    return (
        template_text.replace("{{TASK_ID}}", task_id)
        .replace("{{SESSION_TASK_INDEX}}", str(session_task_index))
        .replace("{{TASK_PROMPT}}", task_prompt)
    )


def _read_text(path: Path | None) -> str:
    if path is None:
        return ""
    return path.read_text(encoding="utf-8")


def _write_session_artifact(
    *,
    conn,
    run_output_dir: Path,
    state: WorkerSessionState,
    end_reason: str | None = None,
) -> None:
    session_dir = _session_dir(run_output_dir, state.session_row_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    tasks = conn.execute(
        """
        SELECT task_id, input_path, status, session_task_index, session_turn_index, fresh_session_started
        FROM tasks
        WHERE session_row_id = ?
        ORDER BY session_task_index ASC, input_path ASC
        """,
        (state.session_row_id,),
    ).fetchall()
    payload = {
        "session_row_id": state.session_row_id,
        "run_id": state.run_id,
        "worker_id": state.worker_id,
        "runtime_mode": state.runtime_mode,
        "status": state.status,
        "resume_key": state.resume_key,
        "thread_id": state.thread_id,
        "turn_count": state.turn_count,
        "task_count": state.task_count,
        "current_task_id": state.current_task_id,
        "project_cd_dir": str(state.project_cd_dir),
        "effective_cd_dir": str(state.prepared_execution.cd_dir),
        "codex_home_path": str(state.codex_home_path) if state.codex_home_path is not None else None,
        "end_reason": end_reason,
        "tasks": [dict(row) for row in tasks],
    }
    _session_summary_path(run_output_dir, state.session_row_id).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _close_session(
    *,
    conn,
    run_output_dir: Path,
    state: WorkerSessionState | None,
    status: str,
    end_reason: str,
) -> None:
    if state is None:
        return
    state.status = status
    finish_worker_session(
        conn,
        session_row_id=state.session_row_id,
        status=status,
        end_reason=end_reason,
        resume_key=state.resume_key,
        thread_id=state.thread_id,
        turn_count=state.turn_count,
        task_count=state.task_count,
        last_task_id=state.current_task_id,
    )
    _write_session_artifact(
        conn=conn,
        run_output_dir=run_output_dir,
        state=state,
        end_reason=end_reason,
    )
    if state.prepared_execution.scratch_root is not None:
        shutil.rmtree(state.prepared_execution.scratch_root, ignore_errors=True)


def worker_session_loop(
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
    if run_id is None:
        raise ValueError("Session runtime requires a scoped run_id.")

    conn = open_db(db_path_for_data_dir(data_dir))
    usage_log_csv = data_dir.resolve() / "codex_exec_activity.csv"
    exit_code = 0
    active_heartbeat: LeaseHeartbeatSession | None = None
    session_state: WorkerSessionState | None = None
    frozen_execution_cache: dict[str, FrozenExecutionSpec] = {}

    def _stop_active_heartbeat() -> None:
        nonlocal active_heartbeat
        if active_heartbeat is None:
            return
        active_heartbeat.stop()
        if warning_callback is not None:
            if active_heartbeat.lost_ownership:
                warning_callback(
                    "Task lease heartbeat lost ownership; skipping stale finalization for this worker attempt."
                )
            elif active_heartbeat.last_error:
                warning_callback(
                    f"Task lease heartbeat encountered transient DB errors: {active_heartbeat.last_error}"
                )
        active_heartbeat = None

    while True:
        _stop_active_heartbeat()
        if stop_event is not None and stop_event.is_set():
            _close_session(
                conn=conn,
                run_output_dir=Path(get_run(conn, run_id)["output_dir"]).resolve(),
                state=session_state,
                status="stopped",
                end_reason="stop_requested",
            )
            return exit_code

        task = lease_one_task(
            conn,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            run_id=run_id,
        )
        if task is None:
            if session_state is not None:
                _close_session(
                    conn=conn,
                    run_output_dir=Path(get_run(conn, run_id)["output_dir"]).resolve(),
                    state=session_state,
                    status="finished",
                    end_reason="drained",
                )
                session_state = None
            if once:
                should_wait, cooldown_remaining, _reason = run_has_waitable_work(
                    conn,
                    run_id=run_id,
                    now=time.time(),
                )
                if not should_wait:
                    return exit_code
                sleep_for = poll_seconds
                if cooldown_remaining is not None and cooldown_remaining > 0:
                    sleep_for = min(poll_seconds, max(0.01, cooldown_remaining))
                time.sleep(sleep_for)
                continue
            status = run_status(conn, run_id=run_id)
            if status["status"] in {"done", "error", "canceled"}:
                return exit_code
            time.sleep(poll_seconds)
            continue

        task_lease_token = str(task.get("lease_token") or "").strip() or None
        if task_lease_token is None:
            mark_task_error(
                conn,
                task_id=str(task["task_id"]),
                error="Task lease token missing; cannot safely execute agentic task.",
            )
            exit_code = 1
            continue

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

        run = get_run(conn, str(task["run_id"]))
        run_config = _parse_run_config(run.get("config_json", "{}"))
        runtime_mode = str(run_config.get("runtime_mode") or "").strip()
        run_output_dir = Path(run["output_dir"]).resolve()
        session_task_budget = int(run_config.get("session_task_budget") or 25)
        session_reset_on_error = bool(run_config.get("session_reset_on_error", True))
        max_turns_per_task = int(run_config.get("max_turns_per_task") or 1)

        if max_turns_per_task != 1:
            mark_task_error(
                conn,
                task_id=str(task["task_id"]),
                error="structured_loop_agentic_v1 currently supports max_turns_per_task=1 only.",
                lease_token=task_lease_token,
            )
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
            _resolve_recipeimport_benchmark_debug(run_config=run_config)
        except (FileNotFoundError, ValueError) as exc:
            mark_task_error(
                conn,
                task_id=str(task["task_id"]),
                error=_trim_error(str(exc)),
                lease_token=task_lease_token,
            )
            exit_code = 1
            continue

        if recipeimport_benchmark_mode is not None:
            mark_task_error(
                conn,
                task_id=str(task["task_id"]),
                error="structured_loop_agentic_v1 does not yet support recipeimport benchmark mode.",
                lease_token=task_lease_token,
            )
            exit_code = 1
            continue

        frozen_assets_config = run_config.get("frozen_assets")
        if not isinstance(frozen_assets_config, dict):
            mark_task_error(
                conn,
                task_id=str(task["task_id"]),
                error="structured_loop_agentic_v1 requires frozen run assets.",
                lease_token=task_lease_token,
            )
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
            mark_task_error(
                conn,
                task_id=str(task["task_id"]),
                error=_trim_error(str(exc)),
                lease_token=task_lease_token,
            )
            exit_code = 1
            continue

        if runtime_mode != frozen_spec.runtime_mode:
            mark_task_error(
                conn,
                task_id=str(task["task_id"]),
                error="Run config runtime_mode does not match frozen execution assets.",
                lease_token=task_lease_token,
            )
            exit_code = 1
            continue

        selected_codex_home = _path_from_config(run_config.get("codex_home_path"))
        input_path = Path(task["input_path"]).resolve()
        final_output_path = run_output_dir / str(task["rel_output_path"])
        staged_output_path = _stage_output_path(
            run_output_dir,
            task_id=str(task["task_id"]),
            lease_token=task_lease_token,
            output_ext="".join(final_output_path.suffixes),
        )
        staged_output_path.unlink(missing_ok=True)
        project_cd_dir = _resolve_task_cd_dir(
            codex_cd_mode=frozen_spec.codex_cd_mode,
            run=run,
            input_path=input_path,
            farm_root=run_farm_root,
            workspace_root_override=workspace_root_override,
        )

        if session_state is not None and (
            session_state.task_count >= session_task_budget
            or session_state.project_cd_dir != project_cd_dir
        ):
            _close_session(
                conn=conn,
                run_output_dir=run_output_dir,
                state=session_state,
                status="finished",
                end_reason=(
                    "cd_dir_changed"
                    if session_state.project_cd_dir != project_cd_dir
                    else "session_task_budget_reached"
                ),
            )
            session_state = None

        prompt = render_prompt_template(frozen_spec.prompt_template_path, input_path)
        heads_up_enabled = parse_heads_up_enabled(
            run_config.get("heads_up_enabled"),
            default=False,
        )
        heads_up_max_tips = parse_heads_up_max_tips(
            run_config.get("heads_up_max_tips"),
            default=DEFAULT_HEADS_UP_MAX_TIPS,
        )
        input_signature = ""
        applied_heads_up_tips: list[dict] = []
        if heads_up_enabled:
            input_signature = compute_input_signature(input_path)
            applied_heads_up_tips = select_heads_up_tips(
                conn,
                pipeline_id=str(run["pipeline_id"]),
                input_signature=input_signature,
                limit=heads_up_max_tips,
            )
            prompt = append_heads_up_block(prompt, applied_heads_up_tips)

        lease_claim_index = int(task.get("attempts") or 0)
        execution_attempt_index = begin_task_execution(
            conn,
            task_id=str(task["task_id"]),
            lease_token=task_lease_token,
        )
        if execution_attempt_index is None:
            continue
        rate_limit_count = int(task.get("rate_limit_count") or 0)
        effective_execution_attempt_index = max(0, execution_attempt_index - rate_limit_count)
        prompt, retry_meta = _append_retry_context(
            prompt=prompt,
            effective_execution_attempts=effective_execution_attempt_index,
            previous_error=task.get("previous_error"),
        )
        applied_tip_ids, applied_tip_texts, applied_tip_scores = _heads_up_telemetry_lists(
            applied_heads_up_tips
        )

        if session_state is None:
            prepared_execution = prepare_execution_context(
                execution_context=frozen_spec.codex_execution_context,
                project_cd_dir=project_cd_dir,
                data_dir=data_dir,
                source="worker-session",
                codex_home_path=selected_codex_home,
                run_id=str(task["run_id"]),
            )
            session_row_id = create_worker_session(
                conn,
                run_id=str(task["run_id"]),
                worker_id=worker_id,
                runtime_mode=runtime_mode,
                status="running",
                codex_home_path=(
                    str(selected_codex_home) if selected_codex_home is not None else None
                ),
                cd_dir=str(prepared_execution.cd_dir),
            )
            session_state = WorkerSessionState(
                session_row_id=session_row_id,
                run_id=str(task["run_id"]),
                worker_id=worker_id,
                runtime_mode=runtime_mode,
                status="running",
                resume_key=None,
                thread_id=None,
                turn_count=0,
                task_count=0,
                started_at=str(run.get("created_at") or ""),
                current_task_id=None,
                project_cd_dir=project_cd_dir,
                prepared_execution=prepared_execution,
                codex_home_path=selected_codex_home,
            )

        session_task_index = session_state.task_count + 1
        session_turn_index = session_state.turn_count + 1
        task_turn_prompt = _render_session_task_turn(
            template_text=_read_text(frozen_spec.session_task_turn_template_path),
            task_id=str(task["task_id"]),
            session_task_index=session_task_index,
            task_prompt=prompt,
        )
        turn_prompt = task_turn_prompt
        turn_kind = "resume"
        if session_state.resume_key is None:
            bootstrap = _read_text(frozen_spec.session_bootstrap_prompt_path).strip()
            turn_prompt = f"{bootstrap}\n\n{task_turn_prompt}".strip()
            turn_kind = "start"

        trace_output_path = _session_turn_trace_path(
            run_output_dir,
            session_row_id=session_state.session_row_id,
            turn_index=session_turn_index,
        )
        usage_context = {
            "source": "worker",
            "pipeline_id": str(run["pipeline_id"]),
            "run_id": task["run_id"],
            "task_id": task["task_id"],
            "worker_id": worker_id,
            "input_path": str(input_path),
            "runtime_mode": runtime_mode,
            "session_row_id": session_state.session_row_id,
            "resume_key": session_state.resume_key,
            "session_task_index": session_task_index,
            "session_turn_index": session_turn_index,
            "turn_kind": turn_kind,
            "heads_up_applied": bool(applied_tip_texts),
            "heads_up_tip_count": len(applied_tip_texts),
            "heads_up_input_signature": input_signature or None,
            "heads_up_tip_ids_json": json.dumps(applied_tip_ids, sort_keys=True),
            "heads_up_tip_texts_json": json.dumps(applied_tip_texts, sort_keys=True),
            "heads_up_tip_scores_json": json.dumps(applied_tip_scores, sort_keys=True),
            "attempt_index": lease_claim_index,
            "lease_claim_index": lease_claim_index,
            "execution_attempt_index": execution_attempt_index,
            "execution_context": frozen_spec.codex_execution_context,
            "codex_home_path": (
                str(selected_codex_home) if selected_codex_home is not None else None
            ),
            **retry_meta,
        }
        linked = link_task_to_worker_session(
            conn,
            task_id=str(task["task_id"]),
            session_row_id=session_state.session_row_id,
            session_task_index=session_task_index,
            session_turn_index=session_turn_index,
            fresh_session_started=turn_kind == "start",
            lease_token=task_lease_token,
        )
        if not linked:
            staged_output_path.unlink(missing_ok=True)
            continue
        codex_stdout_tail: str | None = None
        codex_stderr_tail: str | None = None
        codex_exit_code: int | None = None

        try:
            if turn_kind == "start":
                result = start_codex_session(
                    cd_dir=session_state.prepared_execution.cd_dir,
                    prompt=turn_prompt,
                    model=frozen_spec.codex_model,
                    sandbox=frozen_spec.codex_sandbox,
                    ask_for_approval=frozen_spec.codex_ask_for_approval,
                    web_search=frozen_spec.codex_web_search,
                    output_path=staged_output_path,
                    timeout_seconds=frozen_spec.codex_timeout_seconds,
                    output_schema_logical_path=frozen_spec.logical_output_schema_source_path,
                    reasoning_effort=frozen_spec.codex_reasoning_effort,
                    env_overrides=session_state.prepared_execution.env_overrides,
                    usage_log_csv=usage_log_csv,
                    usage_context=usage_context,
                    trace_output_path=trace_output_path,
                )
            else:
                result = resume_codex_session(
                    resume_key=session_state.resume_key or "",
                    cd_dir=session_state.prepared_execution.cd_dir,
                    prompt=turn_prompt,
                    model=frozen_spec.codex_model,
                    sandbox=frozen_spec.codex_sandbox,
                    ask_for_approval=frozen_spec.codex_ask_for_approval,
                    web_search=frozen_spec.codex_web_search,
                    output_path=staged_output_path,
                    timeout_seconds=frozen_spec.codex_timeout_seconds,
                    output_schema_logical_path=frozen_spec.logical_output_schema_source_path,
                    reasoning_effort=frozen_spec.codex_reasoning_effort,
                    env_overrides=session_state.prepared_execution.env_overrides,
                    usage_log_csv=usage_log_csv,
                    usage_context=usage_context,
                    trace_output_path=trace_output_path,
                )
            codex_stdout_tail = result.stdout_tail
            codex_stderr_tail = result.stderr_tail
            codex_exit_code = result.exit_code

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
                    raise CodexExecRateLimitError(
                        (
                            "WARNING: codex rate limit (HTTP 429) detected; "
                            f"codex exit={result.exit_code}; details: {stderr}"
                        ),
                        retry_after_seconds=extract_retry_after_seconds(stderr),
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

            next_resume_key = (result.resume_key or result.thread_id or session_state.resume_key or "").strip()
            if not next_resume_key:
                raise WorkerRuntimeFailure(
                    "Codex session turn completed but no resume key or thread id was captured.",
                    failure_category="session_transport_error",
                    stdout_tail=result.stdout_tail,
                    stderr_tail=result.stderr_tail,
                )
            validate_json_file_against_schema(
                json_path=staged_output_path,
                schema_path=frozen_spec.output_schema_path,
            )
            transitioned = _promote_staged_output_if_owner(
                conn,
                task_id=str(task["task_id"]),
                lease_token=task_lease_token,
                staged_output_path=staged_output_path,
                final_output_path=final_output_path,
            )
            if not transitioned:
                staged_output_path.unlink(missing_ok=True)
                continue

            session_state.resume_key = next_resume_key
            session_state.thread_id = result.thread_id or session_state.thread_id
            session_state.turn_count = session_turn_index
            session_state.task_count = session_task_index
            session_state.current_task_id = str(task["task_id"])
            update_worker_session(
                conn,
                session_row_id=session_state.session_row_id,
                resume_key=session_state.resume_key,
                thread_id=session_state.thread_id,
                turn_count=session_state.turn_count,
                task_count=session_state.task_count,
                last_task_id=session_state.current_task_id,
            )
            _write_session_artifact(
                conn=conn,
                run_output_dir=run_output_dir,
                state=session_state,
            )
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
                                f"Resuming run {task['run_id']}; effective concurrency {recovered.concurrency_limit}/{recovered.desired_concurrency}"
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
            error_message = _trim_error(str(exc))
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
            give_up = should_give_up(throttle_after)
            staged_output_path.unlink(missing_ok=True)
            transitioned = False
            try:
                conn.execute("BEGIN IMMEDIATE")
                transitioned = requeue_task_after_rate_limit(
                    conn,
                    task_id=str(task["task_id"]),
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
            _close_session(
                conn=conn,
                run_output_dir=run_output_dir,
                state=session_state,
                status="rate_limited",
                end_reason="rate_limit",
            )
            session_state = None
            if warning_callback is not None and transitioned:
                warning_callback(
                    f"Rate limit detected for run {run_id_value}; cooling for {throttle_after.last_cooldown_seconds}s."
                )
            if give_up:
                exit_code = 1
                return exit_code
            continue

        except (CodexExecTimeoutError, SchemaValidationError, WorkerRuntimeFailure) as exc:
            if isinstance(exc, CodexExecTimeoutError):
                failure_category = "timeout"
            elif isinstance(exc, SchemaValidationError):
                failure_category = (
                    "invalid_json" if str(exc).startswith("Invalid JSON at ") else "schema_validation"
                )
            else:
                failure_category = exc.failure_category
            error_message = _trim_error(str(exc))
            capture_failure_forensics(
                conn,
                request=FailureForensicsRequest(
                    data_dir=data_dir,
                    source="worker",
                    run_id=str(task["run_id"]),
                    task_id=str(task["task_id"]),
                    pipeline_id=str(run["pipeline_id"]),
                    attempt_index=lease_claim_index,
                    terminal=False,
                    input_path=input_path,
                    input_hash=str(task.get("input_hash") or ""),
                    rel_output_path=str(task.get("rel_output_path") or ""),
                    worker_id=worker_id,
                    failure_stage="codex_exec",
                    failure_category=failure_category,
                    error_message_full=str(exc),
                    error_message_summary=error_message,
                    prompt_text=turn_prompt,
                    schema_path=frozen_spec.output_schema_path,
                    output_path=staged_output_path,
                    stdout_tail=codex_stdout_tail,
                    stderr_tail=codex_stderr_tail,
                    runtime_context={
                        "runtime_mode": runtime_mode,
                        "session_row_id": session_state.session_row_id if session_state else None,
                        "session_task_index": session_task_index,
                        "session_turn_index": session_turn_index,
                        "turn_kind": turn_kind,
                    },
                    previous_error=str(task.get("previous_error") or "").strip() or None,
                ),
            )
            staged_output_path.unlink(missing_ok=True)
            terminal_failure = (
                failure_category in {
                    "auth_failure",
                    "invalid_json_schema",
                    "content_filter",
                    "session_transport_error",
                }
                or effective_execution_attempt_index >= max_attempts
            )
            if session_state is not None and session_reset_on_error:
                _close_session(
                    conn=conn,
                    run_output_dir=run_output_dir,
                    state=session_state,
                    status="failed",
                    end_reason=failure_category,
                )
                session_state = None
            if terminal_failure:
                transitioned = mark_task_error(
                    conn,
                    task_id=str(task["task_id"]),
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
                if _run_is_canceling(conn, str(task["run_id"])):
                    mark_task_canceled(
                        conn,
                        task_id=str(task["task_id"]),
                        lease_token=task_lease_token,
                        error=error_message,
                    )
                else:
                    requeue_task(
                        conn,
                        task_id=str(task["task_id"]),
                        error=error_message,
                        lease_token=task_lease_token,
                    )

        except Exception as exc:  # pragma: no cover
            error_message = _trim_error(f"Unexpected session runtime error: {exc}")
            staged_output_path.unlink(missing_ok=True)
            if session_state is not None and session_reset_on_error:
                _close_session(
                    conn=conn,
                    run_output_dir=run_output_dir,
                    state=session_state,
                    status="failed",
                    end_reason="unexpected_exception",
                )
                session_state = None
            if effective_execution_attempt_index >= max_attempts:
                mark_task_error(
                    conn,
                    task_id=str(task["task_id"]),
                    error=error_message,
                    lease_token=task_lease_token,
                )
                exit_code = 1
            else:
                requeue_task(
                    conn,
                    task_id=str(task["task_id"]),
                    error=error_message,
                    lease_token=task_lease_token,
                )

    return exit_code
