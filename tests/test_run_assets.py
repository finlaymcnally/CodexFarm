import json
from pathlib import Path

import pytest

from codex_farm.pipeline_spec import load_pipelines
from codex_farm.run_assets import (
    FrozenRunAssetsError,
    freeze_run_assets,
    load_frozen_run_assets,
)
from codex_farm.runtime_modes import CLASSIC_TASK_FARM_V1


def _write_pack(root: Path, pipeline_id: str) -> None:
    for folder in ("pipelines", "prompts", "schemas"):
        (root / folder).mkdir(parents=True, exist_ok=True)

    slug = pipeline_id.replace(".", "_")
    prompt_rel = Path("prompts") / f"{slug}.txt"
    schema_rel = Path("schemas") / f"{slug}.schema.json"
    pipeline_payload = {
        "pipeline_id": pipeline_id,
        "description": "Demo frozen assets pack",
        "prompt_template_path": prompt_rel.as_posix(),
        "output_schema_path": schema_rel.as_posix(),
        "input_glob_default": "**/*.json",
        "output_ext": ".json",
        "codex_model": "gpt-5.3-codex-spark",
        "codex_sandbox": "read-only",
        "codex_ask_for_approval": "never",
        "codex_web_search": "disabled",
        "codex_timeout_seconds": 180,
        "codex_cd_mode": "asset_root",
        "codex_execution_context": "scratch",
        "codex_home_profile": "recipe",
    }
    (root / "pipelines" / f"{pipeline_id}.json").write_text(
        json.dumps(pipeline_payload, indent=2) + "\n",
        encoding="utf-8",
    )
    (root / prompt_rel).write_text(
        "Frozen base prompt for {{INPUT_PATH}}\n",
        encoding="utf-8",
    )
    (root / schema_rel).write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["ok"],
                "properties": {"ok": {"type": "string"}},
                "additionalProperties": False,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def test_freeze_run_assets_round_trip(tmp_path: Path) -> None:
    pack_root = tmp_path / "pack"
    data_dir = tmp_path / "var"
    data_dir.mkdir(parents=True, exist_ok=True)
    pipeline_id = "demo.frozen.assets.v1"
    _write_pack(pack_root, pipeline_id)
    pipeline = load_pipelines(pack_root / "pipelines")[pipeline_id]

    pointer = freeze_run_assets(
        run_id="run-123",
        data_dir=data_dir,
        pipeline=pipeline,
        runtime_mode=CLASSIC_TASK_FARM_V1,
        resolved_model="gpt-model-override",
        resolved_reasoning_effort="high",
        resolved_output_schema_path=pipeline.output_schema_path,
    )

    assert pointer == {
        "version": 1,
        "manifest_relpath": "run_assets/run-123/manifest.json",
    }
    manifest_path = data_dir / "run_assets" / "run-123" / "manifest.json"
    assert manifest_path.exists()
    assert (manifest_path.parent / "pipeline.source.json").exists()
    assert (manifest_path.parent / "effective_pipeline.json").exists()
    assert (manifest_path.parent / "prompt.template.txt").exists()
    assert (manifest_path.parent / "output.schema.json").exists()

    manifest, execution = load_frozen_run_assets(
        data_dir=data_dir,
        frozen_assets_config=pointer,
    )
    assert manifest.run_id == "run-123"
    assert manifest.pipeline_id == pipeline_id
    assert execution.pipeline_id == pipeline_id
    assert execution.codex_model == "gpt-model-override"
    assert execution.runtime_mode == CLASSIC_TASK_FARM_V1
    assert execution.codex_reasoning_effort == "high"
    assert execution.codex_execution_context == "scratch"
    assert execution.codex_home_profile == "recipe"
    assert execution.prompt_template_path == manifest.prompt_template_path
    assert execution.output_schema_path == manifest.output_schema_path
    assert execution.logical_output_schema_source_path == pipeline.output_schema_path.resolve()

    effective_payload = json.loads(manifest.effective_pipeline_path.read_text(encoding="utf-8"))
    assert effective_payload["codex_model"] == "gpt-model-override"
    assert effective_payload["codex_reasoning_effort"] == "high"
    assert effective_payload["codex_execution_context"] == "scratch"
    assert effective_payload["codex_home_profile"] == "recipe"
    assert effective_payload["prompt_template_relpath"] == "prompt.template.txt"
    assert effective_payload["output_schema_relpath"] == "output.schema.json"


def test_load_frozen_run_assets_rejects_unknown_manifest_version(tmp_path: Path) -> None:
    pack_root = tmp_path / "pack"
    data_dir = tmp_path / "var"
    data_dir.mkdir(parents=True, exist_ok=True)
    pipeline_id = "demo.frozen.bad.version.v1"
    _write_pack(pack_root, pipeline_id)
    pipeline = load_pipelines(pack_root / "pipelines")[pipeline_id]

    pointer = freeze_run_assets(
        run_id="run-version",
        data_dir=data_dir,
        pipeline=pipeline,
        runtime_mode=CLASSIC_TASK_FARM_V1,
        resolved_model=pipeline.codex_model,
        resolved_reasoning_effort=pipeline.codex_reasoning_effort,
        resolved_output_schema_path=pipeline.output_schema_path,
    )

    manifest_path = data_dir / "run_assets" / "run-version" / "manifest.json"
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_payload["schema_version"] = 999
    manifest_path.write_text(json.dumps(manifest_payload, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(FrozenRunAssetsError, match="Unsupported frozen assets manifest schema_version"):
        load_frozen_run_assets(
            data_dir=data_dir,
            frozen_assets_config=pointer,
        )


def test_load_frozen_run_assets_rejects_hash_mismatch(tmp_path: Path) -> None:
    pack_root = tmp_path / "pack"
    data_dir = tmp_path / "var"
    data_dir.mkdir(parents=True, exist_ok=True)
    pipeline_id = "demo.frozen.hash.v1"
    _write_pack(pack_root, pipeline_id)
    pipeline = load_pipelines(pack_root / "pipelines")[pipeline_id]

    pointer = freeze_run_assets(
        run_id="run-hash",
        data_dir=data_dir,
        pipeline=pipeline,
        runtime_mode=CLASSIC_TASK_FARM_V1,
        resolved_model=pipeline.codex_model,
        resolved_reasoning_effort=None,
        resolved_output_schema_path=pipeline.output_schema_path,
    )

    frozen_prompt = data_dir / "run_assets" / "run-hash" / "prompt.template.txt"
    frozen_prompt.write_text("tampered", encoding="utf-8")

    with pytest.raises(FrozenRunAssetsError, match="Frozen asset hash mismatch"):
        load_frozen_run_assets(
            data_dir=data_dir,
            frozen_assets_config=pointer,
        )


def test_freeze_run_assets_rejects_duplicate_run_id(tmp_path: Path) -> None:
    pack_root = tmp_path / "pack"
    data_dir = tmp_path / "var"
    data_dir.mkdir(parents=True, exist_ok=True)
    pipeline_id = "demo.frozen.duplicate.v1"
    _write_pack(pack_root, pipeline_id)
    pipeline = load_pipelines(pack_root / "pipelines")[pipeline_id]

    freeze_run_assets(
        run_id="same-run",
        data_dir=data_dir,
        pipeline=pipeline,
        runtime_mode=CLASSIC_TASK_FARM_V1,
        resolved_model=pipeline.codex_model,
        resolved_reasoning_effort=None,
        resolved_output_schema_path=pipeline.output_schema_path,
    )
    with pytest.raises(FrozenRunAssetsError, match="already exists"):
        freeze_run_assets(
            run_id="same-run",
            data_dir=data_dir,
            pipeline=pipeline,
            runtime_mode=CLASSIC_TASK_FARM_V1,
            resolved_model=pipeline.codex_model,
            resolved_reasoning_effort=None,
            resolved_output_schema_path=pipeline.output_schema_path,
        )
