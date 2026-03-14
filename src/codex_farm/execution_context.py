"""Prepare the effective codex exec working directory and env overrides."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import uuid


@dataclass(frozen=True)
class PreparedExecutionContext:
    cd_dir: Path
    env_overrides: dict[str, str]
    scratch_root: Path | None
    metadata: dict[str, object]


def _timestamp_now() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%d_%H.%M.%S")


def prepare_execution_context(
    *,
    execution_context: str,
    project_cd_dir: Path,
    data_dir: Path,
    source: str,
    codex_home_path: Path | None,
    run_id: str | None = None,
    task_id: str | None = None,
    lease_token: str | None = None,
) -> PreparedExecutionContext:
    resolved_project_cd_dir = project_cd_dir.expanduser().resolve()
    resolved_data_dir = data_dir.expanduser().resolve()
    env_overrides: dict[str, str] = {}
    if codex_home_path is not None:
        env_overrides["CODEX_HOME"] = str(codex_home_path.expanduser().resolve())

    scratch_root: Path | None = None
    effective_cd_dir = resolved_project_cd_dir
    if execution_context == "scratch":
        execution_root = resolved_data_dir / "execution_contexts"
        if run_id and task_id and lease_token:
            scratch_root = execution_root / str(run_id) / str(task_id) / str(lease_token)
        else:
            scratch_root = execution_root / f"{source}-{_timestamp_now()}-{uuid.uuid4().hex[:8]}"
        scratch_root.mkdir(parents=True, exist_ok=False)
        effective_cd_dir = scratch_root
        marker_path = scratch_root / "context.json"
        marker_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "created_at_utc": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "source": source,
                    "execution_context": execution_context,
                    "project_cd_dir": str(resolved_project_cd_dir),
                    "codex_home_path": env_overrides.get("CODEX_HOME"),
                    "run_id": run_id,
                    "task_id": task_id,
                    "lease_token": lease_token,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    metadata = {
        "codex_execution_context": execution_context,
        "project_cd_dir": str(resolved_project_cd_dir),
        "effective_cd_dir": str(effective_cd_dir),
        "scratch_root": str(scratch_root) if scratch_root is not None else None,
        "codex_home_path": env_overrides.get("CODEX_HOME"),
    }
    return PreparedExecutionContext(
        cd_dir=effective_cd_dir,
        env_overrides=env_overrides,
        scratch_root=scratch_root,
        metadata=metadata,
    )
