"""Worker loop that claims tasks and runs codex exec."""

from __future__ import annotations

import json
import time
from pathlib import Path

from .codex_exec import CodexExecTimeoutError, run_codex_exec
from .db import (
    get_run,
    lease_one_task,
    mark_task_done,
    mark_task_error,
    open_db,
    requeue_task,
)
from .paths import db_path_for_data_dir, resolve_farm_root
from .pipeline_spec import PipelineSpec, load_pipelines, render_prompt_template
from .schema_utils import SchemaValidationError, validate_json_file_against_schema


def _trim_error(text: str, limit: int = 1800) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


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


def _resolve_task_cd_dir(
    *,
    spec: PipelineSpec,
    run: dict[str, object],
    input_path: Path,
    farm_root: Path,
    workspace_root_override: Path | None,
) -> Path:
    if workspace_root_override is not None:
        cd_dir = workspace_root_override
    elif spec.codex_cd_mode == "asset_root":
        cd_dir = farm_root
    elif spec.codex_cd_mode == "input_dir":
        cd_dir = Path(str(run["input_dir"])).expanduser().resolve()
    else:
        cd_dir = input_path.parent

    if not cd_dir.exists() or not cd_dir.is_dir():
        raise FileNotFoundError(f"Computed codex --cd directory does not exist: {cd_dir}")
    return cd_dir


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
) -> int:
    pipeline_cache: dict[Path, dict] = {}

    conn = open_db(db_path_for_data_dir(data_dir))
    exit_code = 0

    while True:
        task = lease_one_task(
            conn,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            run_id=run_id,
        )

        if task is None:
            if once:
                return exit_code
            time.sleep(poll_seconds)
            continue

        if task["attempts"] > max_attempts:
            mark_task_error(
                conn,
                task_id=task["task_id"],
                error=f"Max attempts exceeded ({max_attempts}) before processing",
            )
            exit_code = 1
            continue

        run = get_run(conn, task["run_id"])
        run_config = _parse_run_config(run.get("config_json", "{}"))
        try:
            run_farm_root = _resolve_run_farm_root(
                run_config=run_config,
                worker_root=farm_root,
            )
            if run_farm_root not in pipeline_cache:
                pipeline_cache[run_farm_root] = load_pipelines(run_farm_root / "pipelines")
            pipelines = pipeline_cache[run_farm_root]
            workspace_root_override = _resolve_workspace_root_override(run_config=run_config)
        except (FileNotFoundError, ValueError) as exc:
            mark_task_error(
                conn,
                task_id=task["task_id"],
                error=_trim_error(str(exc)),
            )
            exit_code = 1
            continue

        pipeline_id = run["pipeline_id"]
        spec = pipelines.get(pipeline_id)
        if spec is None:
            mark_task_error(
                conn,
                task_id=task["task_id"],
                error=f"Unknown pipeline_id: {pipeline_id}",
            )
            exit_code = 1
            continue

        input_path = Path(task["input_path"]).resolve()
        output_path = Path(run["output_dir"]).resolve() / task["rel_output_path"]
        try:
            cd_dir = _resolve_task_cd_dir(
                spec=spec,
                run=run,
                input_path=input_path,
                farm_root=run_farm_root,
                workspace_root_override=workspace_root_override,
            )
        except FileNotFoundError as exc:
            mark_task_error(
                conn,
                task_id=task["task_id"],
                error=_trim_error(str(exc)),
            )
            exit_code = 1
            continue
        prompt = render_prompt_template(spec.prompt_template_path, input_path)

        try:
            result = run_codex_exec(
                cd_dir=cd_dir,
                prompt=prompt,
                model=spec.codex_model,
                sandbox=spec.codex_sandbox,
                ask_for_approval=spec.codex_ask_for_approval,
                web_search=spec.codex_web_search,
                output_schema=spec.output_schema_path,
                output_path=output_path,
                timeout_seconds=spec.codex_timeout_seconds,
            )
            if not result.ok:
                stderr = result.stderr_tail or "no stderr"
                raise RuntimeError(
                    f"codex exec failed (exit={result.exit_code}): {stderr}"
                )

            validate_json_file_against_schema(
                json_path=output_path,
                schema_path=spec.output_schema_path,
            )
            mark_task_done(conn, task_id=task["task_id"], output_path=str(output_path))

        except (CodexExecTimeoutError, SchemaValidationError, RuntimeError) as exc:
            output_path.unlink(missing_ok=True)
            error_message = _trim_error(str(exc))
            if task["attempts"] >= max_attempts:
                mark_task_error(conn, task_id=task["task_id"], error=error_message)
                exit_code = 1
            else:
                requeue_task(conn, task_id=task["task_id"], error=error_message)

        except Exception as exc:  # pragma: no cover
            output_path.unlink(missing_ok=True)
            error_message = _trim_error(f"Unexpected worker error: {exc}")
            if task["attempts"] >= max_attempts:
                mark_task_error(conn, task_id=task["task_id"], error=error_message)
                exit_code = 1
            else:
                requeue_task(conn, task_id=task["task_id"], error=error_message)

    return exit_code
