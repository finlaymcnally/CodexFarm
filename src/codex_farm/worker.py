"""Worker loop that claims tasks and runs codex exec."""

from __future__ import annotations

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
from .paths import db_path_for_data_dir, find_repo_root
from .pipeline_spec import load_pipelines, render_prompt_template
from .schema_utils import SchemaValidationError, validate_json_file_against_schema


def _trim_error(text: str, limit: int = 1800) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def worker_loop(
    *,
    data_dir: Path,
    worker_id: str,
    run_id: str | None,
    lease_seconds: int,
    max_attempts: int,
    poll_seconds: float,
    once: bool,
) -> int:
    repo_root = find_repo_root()
    pipelines = load_pipelines(repo_root / "pipelines")

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
        prompt = render_prompt_template(spec.prompt_template_path, input_path)

        try:
            result = run_codex_exec(
                workdir=repo_root,
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
