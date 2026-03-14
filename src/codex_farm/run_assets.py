"""Freeze and load per-run pipeline assets for deterministic worker execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import uuid

from .pipeline_spec import PipelineSpec


FROZEN_ASSETS_VERSION = 1


class FrozenRunAssetsError(ValueError):
    """Raised when frozen run assets are missing, malformed, or tampered."""


@dataclass(frozen=True)
class FrozenRunAssetsManifest:
    schema_version: int
    run_id: str
    pipeline_id: str
    manifest_path: Path
    effective_pipeline_path: Path
    prompt_template_path: Path
    output_schema_path: Path
    logical_output_schema_source_path: Path
    hashes: dict[str, str]
    source_metadata: dict[str, str | None]


@dataclass(frozen=True)
class FrozenExecutionSpec:
    pipeline_id: str
    description: str
    output_ext: str
    codex_model: str
    codex_sandbox: str
    codex_ask_for_approval: str
    codex_web_search: str
    codex_reasoning_effort: str | None
    codex_timeout_seconds: int
    codex_cd_mode: str
    codex_execution_context: str
    codex_home_profile: str | None
    prompt_template_path: Path
    output_schema_path: Path
    logical_output_schema_source_path: Path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_write(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _json_read(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FrozenRunAssetsError(f"Invalid JSON in frozen assets file {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise FrozenRunAssetsError(f"Expected object JSON in frozen assets file {path}")
    return raw


def _string_field(payload: dict[str, object], key: str, *, where: str) -> str:
    value = payload.get(key)
    if isinstance(value, str) and value.strip():
        return value
    raise FrozenRunAssetsError(f"Missing or invalid '{key}' in {where}")


def _optional_string_field(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        trimmed = value.strip()
        return trimmed or None
    raise FrozenRunAssetsError(f"Expected '{key}' to be a string or null")


def _int_field(payload: dict[str, object], key: str, *, where: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise FrozenRunAssetsError(f"Missing or invalid '{key}' in {where}")
    return value


def _resolve_under_data_dir(*, data_dir: Path, relpath: str) -> Path:
    if not relpath.strip():
        raise FrozenRunAssetsError("frozen_assets.manifest_relpath must be a non-empty string")
    rel = Path(relpath)
    if rel.is_absolute():
        raise FrozenRunAssetsError("frozen_assets.manifest_relpath must be relative to data_dir")
    resolved_data_dir = data_dir.resolve()
    candidate = (resolved_data_dir / rel).resolve()
    try:
        candidate.relative_to(resolved_data_dir)
    except ValueError as exc:
        raise FrozenRunAssetsError(
            f"frozen_assets.manifest_relpath escapes data_dir: {relpath}"
        ) from exc
    return candidate


def _manifest_snapshot_files(manifest_payload: dict[str, object]) -> dict[str, str]:
    files_raw = manifest_payload.get("files")
    if not isinstance(files_raw, dict):
        raise FrozenRunAssetsError("Manifest is missing object field 'files'")
    files = {
        "pipeline_source_relpath": _string_field(files_raw, "pipeline_source_relpath", where="manifest.files"),
        "effective_pipeline_relpath": _string_field(
            files_raw, "effective_pipeline_relpath", where="manifest.files"
        ),
        "prompt_template_relpath": _string_field(
            files_raw, "prompt_template_relpath", where="manifest.files"
        ),
        "output_schema_relpath": _string_field(files_raw, "output_schema_relpath", where="manifest.files"),
    }
    return files


def _manifest_hashes(manifest_payload: dict[str, object]) -> dict[str, str]:
    hashes_raw = manifest_payload.get("hashes")
    if not isinstance(hashes_raw, dict):
        raise FrozenRunAssetsError("Manifest is missing object field 'hashes'")
    hashes = {
        "pipeline_source_sha256": _string_field(
            hashes_raw, "pipeline_source_sha256", where="manifest.hashes"
        ),
        "effective_pipeline_sha256": _string_field(
            hashes_raw, "effective_pipeline_sha256", where="manifest.hashes"
        ),
        "prompt_template_sha256": _string_field(
            hashes_raw, "prompt_template_sha256", where="manifest.hashes"
        ),
        "output_schema_sha256": _string_field(hashes_raw, "output_schema_sha256", where="manifest.hashes"),
    }
    return hashes


def freeze_run_assets(
    *,
    run_id: str,
    data_dir: Path,
    pipeline: PipelineSpec,
    resolved_model: str,
    resolved_reasoning_effort: str | None,
    resolved_output_schema_path: Path,
) -> dict[str, object]:
    """Write frozen execution assets for a run and return config_json metadata."""
    data_dir_resolved = data_dir.resolve()
    run_assets_root = data_dir_resolved / "run_assets"
    run_assets_root.mkdir(parents=True, exist_ok=True)

    final_snapshot_dir = run_assets_root / run_id
    if final_snapshot_dir.exists():
        raise FrozenRunAssetsError(
            f"Frozen snapshot directory already exists for run {run_id}: {final_snapshot_dir}"
        )

    stage_snapshot_dir = run_assets_root / f".{run_id}.stage-{uuid.uuid4().hex[:10]}"
    stage_snapshot_dir.mkdir(parents=True, exist_ok=False)

    pipeline_source_path = pipeline.source_path.resolve()
    prompt_source_path = pipeline.prompt_template_path.resolve()
    output_schema_source_path = resolved_output_schema_path.resolve()
    output_schema_source_kind = (
        "pipeline_default"
        if output_schema_source_path == pipeline.output_schema_path.resolve()
        else "override"
    )

    files = {
        "pipeline_source_relpath": "pipeline.source.json",
        "effective_pipeline_relpath": "effective_pipeline.json",
        "prompt_template_relpath": "prompt.template.txt",
        "output_schema_relpath": "output.schema.json",
    }
    pipeline_source_copy_path = stage_snapshot_dir / files["pipeline_source_relpath"]
    effective_pipeline_path = stage_snapshot_dir / files["effective_pipeline_relpath"]
    prompt_template_copy_path = stage_snapshot_dir / files["prompt_template_relpath"]
    output_schema_copy_path = stage_snapshot_dir / files["output_schema_relpath"]
    manifest_path = stage_snapshot_dir / "manifest.json"

    try:
        shutil.copyfile(pipeline_source_path, pipeline_source_copy_path)
        shutil.copyfile(prompt_source_path, prompt_template_copy_path)
        shutil.copyfile(output_schema_source_path, output_schema_copy_path)

        effective_pipeline = {
            "pipeline_id": pipeline.pipeline_id,
            "description": pipeline.description,
            "output_ext": pipeline.output_ext,
            "codex_model": resolved_model,
            "codex_sandbox": pipeline.codex_sandbox,
            "codex_ask_for_approval": pipeline.codex_ask_for_approval,
            "codex_web_search": pipeline.codex_web_search,
            "codex_reasoning_effort": resolved_reasoning_effort,
            "codex_timeout_seconds": pipeline.codex_timeout_seconds,
            "codex_cd_mode": pipeline.codex_cd_mode,
            "codex_execution_context": pipeline.codex_execution_context,
            "codex_home_profile": pipeline.codex_home_profile,
            "prompt_template_relpath": files["prompt_template_relpath"],
            "output_schema_relpath": files["output_schema_relpath"],
            "logical_output_schema_source_path": str(output_schema_source_path),
        }
        _json_write(effective_pipeline_path, effective_pipeline)

        hashes = {
            "pipeline_source_sha256": _sha256_file(pipeline_source_copy_path),
            "effective_pipeline_sha256": _sha256_file(effective_pipeline_path),
            "prompt_template_sha256": _sha256_file(prompt_template_copy_path),
            "output_schema_sha256": _sha256_file(output_schema_copy_path),
        }
        manifest = {
            "schema_version": FROZEN_ASSETS_VERSION,
            "run_id": run_id,
            "pipeline_id": pipeline.pipeline_id,
            "created_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source_metadata": {
                "pipeline_source_path": str(pipeline_source_path),
                "prompt_source_path": str(prompt_source_path),
                "output_schema_source_path": str(output_schema_source_path),
                "output_schema_source_kind": output_schema_source_kind,
            },
            "files": files,
            "hashes": hashes,
        }
        _json_write(manifest_path, manifest)

        stage_snapshot_dir.replace(final_snapshot_dir)
    except Exception:
        shutil.rmtree(stage_snapshot_dir, ignore_errors=True)
        raise

    manifest_relpath = Path("run_assets") / run_id / "manifest.json"
    return {
        "version": FROZEN_ASSETS_VERSION,
        "manifest_relpath": manifest_relpath.as_posix(),
    }


def cleanup_frozen_run_assets(
    *,
    data_dir: Path,
    frozen_assets_config: dict[str, object] | None,
) -> None:
    """Best-effort cleanup when run creation fails after snapshot creation."""
    if not isinstance(frozen_assets_config, dict):
        return
    manifest_relpath = frozen_assets_config.get("manifest_relpath")
    if not isinstance(manifest_relpath, str):
        return

    try:
        manifest_path = _resolve_under_data_dir(
            data_dir=data_dir.resolve(),
            relpath=manifest_relpath,
        )
    except FrozenRunAssetsError:
        return
    shutil.rmtree(manifest_path.parent, ignore_errors=True)


def load_frozen_run_assets(
    *,
    data_dir: Path,
    frozen_assets_config: dict[str, object],
) -> tuple[FrozenRunAssetsManifest, FrozenExecutionSpec]:
    """Load and verify frozen execution assets from a run config pointer."""
    config_version = frozen_assets_config.get("version")
    if config_version != FROZEN_ASSETS_VERSION:
        raise FrozenRunAssetsError(
            f"Unsupported frozen assets config version: {config_version!r}"
        )
    manifest_relpath = frozen_assets_config.get("manifest_relpath")
    if not isinstance(manifest_relpath, str):
        raise FrozenRunAssetsError("Missing or invalid frozen_assets.manifest_relpath")
    manifest_path = _resolve_under_data_dir(
        data_dir=data_dir.resolve(),
        relpath=manifest_relpath,
    )
    if not manifest_path.exists() or not manifest_path.is_file():
        raise FrozenRunAssetsError(f"Frozen assets manifest is missing: {manifest_path}")

    manifest_payload = _json_read(manifest_path)
    manifest_schema_version = _int_field(manifest_payload, "schema_version", where="manifest")
    if manifest_schema_version != FROZEN_ASSETS_VERSION:
        raise FrozenRunAssetsError(
            f"Unsupported frozen assets manifest schema_version: {manifest_schema_version}"
        )

    manifest_run_id = _string_field(manifest_payload, "run_id", where="manifest")
    manifest_pipeline_id = _string_field(manifest_payload, "pipeline_id", where="manifest")

    source_metadata_raw = manifest_payload.get("source_metadata")
    if not isinstance(source_metadata_raw, dict):
        raise FrozenRunAssetsError("Manifest is missing object field 'source_metadata'")
    source_metadata: dict[str, str | None] = {}
    for key in (
        "pipeline_source_path",
        "prompt_source_path",
        "output_schema_source_path",
        "output_schema_source_kind",
    ):
        value = source_metadata_raw.get(key)
        if value is None:
            source_metadata[key] = None
            continue
        if not isinstance(value, str):
            raise FrozenRunAssetsError(
                f"Manifest source_metadata.{key} must be a string or null"
            )
        source_metadata[key] = value

    file_relpaths = _manifest_snapshot_files(manifest_payload)
    hashes = _manifest_hashes(manifest_payload)
    snapshot_dir = manifest_path.parent

    pipeline_source_path = (snapshot_dir / file_relpaths["pipeline_source_relpath"]).resolve()
    effective_pipeline_path = (snapshot_dir / file_relpaths["effective_pipeline_relpath"]).resolve()
    prompt_template_path = (snapshot_dir / file_relpaths["prompt_template_relpath"]).resolve()
    output_schema_path = (snapshot_dir / file_relpaths["output_schema_relpath"]).resolve()

    file_to_hash_key = {
        pipeline_source_path: "pipeline_source_sha256",
        effective_pipeline_path: "effective_pipeline_sha256",
        prompt_template_path: "prompt_template_sha256",
        output_schema_path: "output_schema_sha256",
    }
    for path, hash_key in file_to_hash_key.items():
        if not path.exists() or not path.is_file():
            raise FrozenRunAssetsError(
                f"Frozen asset file is missing: {path}"
            )
        actual_hash = _sha256_file(path)
        expected_hash = hashes[hash_key]
        if actual_hash != expected_hash:
            raise FrozenRunAssetsError(
                f"Frozen asset hash mismatch for {path.name}"
            )

    effective_pipeline = _json_read(effective_pipeline_path)
    if _string_field(effective_pipeline, "pipeline_id", where="effective_pipeline") != manifest_pipeline_id:
        raise FrozenRunAssetsError("Frozen effective pipeline_id does not match manifest")

    prompt_relpath = _string_field(
        effective_pipeline, "prompt_template_relpath", where="effective_pipeline"
    )
    if prompt_relpath != file_relpaths["prompt_template_relpath"]:
        raise FrozenRunAssetsError("Frozen prompt relpath mismatch between manifest and effective pipeline")
    schema_relpath = _string_field(
        effective_pipeline, "output_schema_relpath", where="effective_pipeline"
    )
    if schema_relpath != file_relpaths["output_schema_relpath"]:
        raise FrozenRunAssetsError("Frozen output schema relpath mismatch between manifest and effective pipeline")

    logical_output_schema_source_path_raw = _optional_string_field(
        effective_pipeline,
        "logical_output_schema_source_path",
    ) or source_metadata.get("output_schema_source_path")
    if not logical_output_schema_source_path_raw:
        raise FrozenRunAssetsError("Frozen effective pipeline is missing logical_output_schema_source_path")

    raw_reasoning_effort = effective_pipeline.get("codex_reasoning_effort")
    if raw_reasoning_effort is not None and not isinstance(raw_reasoning_effort, str):
        raise FrozenRunAssetsError(
            "Frozen effective pipeline codex_reasoning_effort must be string or null"
        )
    raw_codex_home_profile = effective_pipeline.get("codex_home_profile")
    if raw_codex_home_profile is not None and not isinstance(raw_codex_home_profile, str):
        raise FrozenRunAssetsError(
            "Frozen effective pipeline codex_home_profile must be string or null"
        )
    raw_execution_context = (
        _optional_string_field(effective_pipeline, "codex_execution_context") or "project"
    )
    if raw_execution_context not in {"project", "scratch"}:
        raise FrozenRunAssetsError(
            "Frozen effective pipeline codex_execution_context must be 'project' or 'scratch'"
        )

    timeout_seconds = _int_field(
        effective_pipeline, "codex_timeout_seconds", where="effective_pipeline"
    )
    if timeout_seconds < 1:
        raise FrozenRunAssetsError(
            "Frozen effective pipeline codex_timeout_seconds must be >= 1"
        )

    logical_output_schema_source_path = Path(logical_output_schema_source_path_raw).expanduser().resolve()

    manifest = FrozenRunAssetsManifest(
        schema_version=manifest_schema_version,
        run_id=manifest_run_id,
        pipeline_id=manifest_pipeline_id,
        manifest_path=manifest_path,
        effective_pipeline_path=effective_pipeline_path,
        prompt_template_path=prompt_template_path,
        output_schema_path=output_schema_path,
        logical_output_schema_source_path=logical_output_schema_source_path,
        hashes=hashes,
        source_metadata=source_metadata,
    )

    execution_spec = FrozenExecutionSpec(
        pipeline_id=manifest_pipeline_id,
        description=_string_field(effective_pipeline, "description", where="effective_pipeline"),
        output_ext=_string_field(effective_pipeline, "output_ext", where="effective_pipeline"),
        codex_model=_string_field(effective_pipeline, "codex_model", where="effective_pipeline"),
        codex_sandbox=_string_field(effective_pipeline, "codex_sandbox", where="effective_pipeline"),
        codex_ask_for_approval=_string_field(
            effective_pipeline,
            "codex_ask_for_approval",
            where="effective_pipeline",
        ),
        codex_web_search=_string_field(effective_pipeline, "codex_web_search", where="effective_pipeline"),
        codex_reasoning_effort=raw_reasoning_effort,
        codex_timeout_seconds=timeout_seconds,
        codex_cd_mode=_string_field(effective_pipeline, "codex_cd_mode", where="effective_pipeline"),
        codex_execution_context=raw_execution_context,
        codex_home_profile=raw_codex_home_profile.strip() if isinstance(raw_codex_home_profile, str) else None,
        prompt_template_path=prompt_template_path,
        output_schema_path=output_schema_path,
        logical_output_schema_source_path=logical_output_schema_source_path,
    )
    return manifest, execution_spec
