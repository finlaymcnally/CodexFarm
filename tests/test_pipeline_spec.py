import json
from pathlib import Path

import pytest

from codex_farm.paths import find_repo_root
from codex_farm.pipeline_spec import load_pipelines, render_prompt_template


def test_load_pipelines_reads_known_specs() -> None:
    repo_root = find_repo_root()
    pipelines = load_pipelines(repo_root / "pipelines")

    assert "recipe.schemaorg.normalize.v1" in pipelines
    assert "recipe.schemaorg.to_proprietary.v1" in pipelines
    assert pipelines["recipe.schemaorg.normalize.v1"].codex_cd_mode == "asset_root"
    assert pipelines["recipe.schemaorg.normalize.v1"].source_path.name.endswith(".json")


def test_render_prompt_template_replaces_input_path(tmp_path: Path) -> None:
    template = tmp_path / "template.txt"
    input_file = tmp_path / "recipe.json"

    template.write_text("Path={{INPUT_PATH}}", encoding="utf-8")
    input_file.write_text("{}", encoding="utf-8")

    rendered = render_prompt_template(template, input_file)

    assert "{{INPUT_PATH}}" not in rendered
    assert str(input_file.resolve()) in rendered


def test_load_pipelines_reads_explicit_codex_cd_mode(tmp_path: Path) -> None:
    for folder in ("pipelines", "prompts", "schemas"):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)

    pipeline_payload = {
        "pipeline_id": "demo.cd.mode.v1",
        "description": "demo",
        "prompt_template_path": "prompts/demo.txt",
        "output_schema_path": "schemas/demo.schema.json",
        "codex_cd_mode": "input_file_dir",
    }
    (tmp_path / "pipelines" / "demo.cd.mode.v1.json").write_text(
        json.dumps(pipeline_payload, indent=2) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "prompts" / "demo.txt").write_text("INPUT={{INPUT_PATH}}\n", encoding="utf-8")
    (tmp_path / "schemas" / "demo.schema.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["ok"],
                "properties": {"ok": {"type": "string"}},
                "additionalProperties": False,
            }
        ),
        encoding="utf-8",
    )

    loaded = load_pipelines(tmp_path / "pipelines")
    assert loaded["demo.cd.mode.v1"].codex_cd_mode == "input_file_dir"


def test_load_pipelines_rejects_unknown_codex_cd_mode(tmp_path: Path) -> None:
    for folder in ("pipelines", "prompts", "schemas"):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)

    payload = {
        "pipeline_id": "demo.bad.mode.v1",
        "description": "demo",
        "prompt_template_path": "prompts/demo.txt",
        "output_schema_path": "schemas/demo.schema.json",
        "codex_cd_mode": "somewhere_else",
    }
    (tmp_path / "pipelines" / "demo.bad.mode.v1.json").write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "prompts" / "demo.txt").write_text("INPUT={{INPUT_PATH}}\n", encoding="utf-8")
    (tmp_path / "schemas" / "demo.schema.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["ok"],
                "properties": {"ok": {"type": "string"}},
                "additionalProperties": False,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_pipelines(tmp_path / "pipelines")


def test_load_pipelines_reads_explicit_codex_reasoning_effort(tmp_path: Path) -> None:
    for folder in ("pipelines", "prompts", "schemas"):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)

    payload = {
        "pipeline_id": "demo.effort.v1",
        "description": "demo",
        "prompt_template_path": "prompts/demo.txt",
        "output_schema_path": "schemas/demo.schema.json",
        "codex_reasoning_effort": "high",
    }
    (tmp_path / "pipelines" / "demo.effort.v1.json").write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "prompts" / "demo.txt").write_text("INPUT={{INPUT_PATH}}\n", encoding="utf-8")
    (tmp_path / "schemas" / "demo.schema.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["ok"],
                "properties": {"ok": {"type": "string"}},
                "additionalProperties": False,
            }
        ),
        encoding="utf-8",
    )

    loaded = load_pipelines(tmp_path / "pipelines")
    assert loaded["demo.effort.v1"].codex_reasoning_effort == "high"


def test_load_pipelines_rejects_unknown_codex_reasoning_effort(tmp_path: Path) -> None:
    for folder in ("pipelines", "prompts", "schemas"):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)

    payload = {
        "pipeline_id": "demo.bad.effort.v1",
        "description": "demo",
        "prompt_template_path": "prompts/demo.txt",
        "output_schema_path": "schemas/demo.schema.json",
        "codex_reasoning_effort": "ultra",
    }
    (tmp_path / "pipelines" / "demo.bad.effort.v1.json").write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "prompts" / "demo.txt").write_text("INPUT={{INPUT_PATH}}\n", encoding="utf-8")
    (tmp_path / "schemas" / "demo.schema.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["ok"],
                "properties": {"ok": {"type": "string"}},
                "additionalProperties": False,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_pipelines(tmp_path / "pipelines")
