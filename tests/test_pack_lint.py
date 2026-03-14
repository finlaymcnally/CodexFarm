import json
from pathlib import Path

from codex_farm.pack_lint import lint_exit_code, lint_pack, lint_schema_file


def _make_pack_root(root: Path) -> None:
    for folder in ("pipelines", "prompts", "schemas"):
        (root / folder).mkdir(parents=True, exist_ok=True)


def _write_pipeline(
    root: Path,
    *,
    filename: str,
    pipeline_id: str,
    prompt_rel: str,
    schema_rel: str,
    prompt_input_mode: str | None = None,
    codex_execution_context: str | None = None,
    codex_home_profile: str | None = None,
) -> None:
    payload = {
        "pipeline_id": pipeline_id,
        "description": f"Pipeline {pipeline_id}",
        "prompt_template_path": prompt_rel,
        "output_schema_path": schema_rel,
        "input_glob_default": "**/*.json",
        "output_ext": ".json",
        "codex_model": "gpt-5.3-codex-spark",
        "codex_sandbox": "read-only",
        "codex_ask_for_approval": "never",
        "codex_web_search": "disabled",
        "codex_timeout_seconds": 180,
        "codex_cd_mode": "asset_root",
    }
    if prompt_input_mode is not None:
        payload["prompt_input_mode"] = prompt_input_mode
    if codex_execution_context is not None:
        payload["codex_execution_context"] = codex_execution_context
    if codex_home_profile is not None:
        payload["codex_home_profile"] = codex_home_profile
    (root / "pipelines" / filename).write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_heads_up_assets(root: Path) -> None:
    (root / "prompts" / "heads_up_distiller_v1.txt").write_text(
        "Return tip candidates only.\n",
        encoding="utf-8",
    )
    (root / "schemas" / "heads_up_tipset_v1.schema.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["tips"],
                "properties": {
                    "tips": {
                        "type": "array",
                        "items": {"type": "string"},
                    }
                },
                "additionalProperties": False,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _demo_schema_payload() -> dict[str, object]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["ok", "source_path"],
        "properties": {
            "ok": {"type": "string"},
            "source_path": {"type": "string"},
        },
        "additionalProperties": False,
    }


def test_lint_pack_clean_pack_reports_no_findings(tmp_path: Path) -> None:
    _make_pack_root(tmp_path)
    _write_pipeline(
        tmp_path,
        filename="demo.clean.v1.json",
        pipeline_id="demo.clean.v1",
        prompt_rel="prompts/demo_clean_v1.txt",
        schema_rel="schemas/demo_clean_v1.schema.json",
    )
    (tmp_path / "prompts" / "demo_clean_v1.txt").write_text("INPUT={{INPUT_PATH}}\n", encoding="utf-8")
    _write_json(tmp_path / "schemas" / "demo_clean_v1.schema.json", _demo_schema_payload())
    _write_heads_up_assets(tmp_path)

    report = lint_pack(root=tmp_path)

    assert report.error_count == 0
    assert report.warning_count == 0
    assert report.findings == []
    assert lint_exit_code(report, strict=False) == 0


def test_lint_pack_reports_missing_prompt_file(tmp_path: Path) -> None:
    _make_pack_root(tmp_path)
    _write_pipeline(
        tmp_path,
        filename="demo.missing.prompt.v1.json",
        pipeline_id="demo.missing.prompt.v1",
        prompt_rel="prompts/missing.txt",
        schema_rel="schemas/demo_missing_prompt_v1.schema.json",
    )
    _write_json(tmp_path / "schemas" / "demo_missing_prompt_v1.schema.json", _demo_schema_payload())
    _write_heads_up_assets(tmp_path)

    report = lint_pack(root=tmp_path)
    codes = {finding.code for finding in report.findings}

    assert "pipeline.missing_prompt_template" in codes
    assert report.error_count >= 1


def test_lint_pack_reports_missing_inline_prompt_token(tmp_path: Path) -> None:
    _make_pack_root(tmp_path)
    _write_pipeline(
        tmp_path,
        filename="demo.inline.v1.json",
        pipeline_id="demo.inline.v1",
        prompt_rel="prompts/demo_inline_v1.txt",
        schema_rel="schemas/demo_inline_v1.schema.json",
        prompt_input_mode="inline",
    )
    (tmp_path / "prompts" / "demo_inline_v1.txt").write_text("INPUT={{INPUT_PATH}}\n", encoding="utf-8")
    _write_json(tmp_path / "schemas" / "demo_inline_v1.schema.json", _demo_schema_payload())
    _write_heads_up_assets(tmp_path)

    report = lint_pack(root=tmp_path)

    assert any(
        finding.code == "pipeline.prompt_missing_required_token"
        and finding.pipeline_id == "demo.inline.v1"
        for finding in report.findings
    )


def test_lint_pack_accepts_inline_prompt_with_input_text_token(tmp_path: Path) -> None:
    _make_pack_root(tmp_path)
    _write_pipeline(
        tmp_path,
        filename="demo.inline.ok.v1.json",
        pipeline_id="demo.inline.ok.v1",
        prompt_rel="prompts/demo_inline_ok_v1.txt",
        schema_rel="schemas/demo_inline_ok_v1.schema.json",
        prompt_input_mode="inline",
    )
    (tmp_path / "prompts" / "demo_inline_ok_v1.txt").write_text(
        "PAYLOAD={{INPUT_TEXT}}\n",
        encoding="utf-8",
    )
    _write_json(tmp_path / "schemas" / "demo_inline_ok_v1.schema.json", _demo_schema_payload())
    _write_heads_up_assets(tmp_path)

    report = lint_pack(root=tmp_path)

    assert not any(
        finding.code == "pipeline.prompt_missing_required_token"
        and finding.pipeline_id == "demo.inline.ok.v1"
        for finding in report.findings
    )


def test_lint_pack_accepts_scratch_execution_context_fields(tmp_path: Path) -> None:
    _make_pack_root(tmp_path)
    _write_pipeline(
        tmp_path,
        filename="demo.scratch.v1.json",
        pipeline_id="demo.scratch.v1",
        prompt_rel="prompts/demo_scratch_v1.txt",
        schema_rel="schemas/demo_scratch_v1.schema.json",
        codex_execution_context="scratch",
        codex_home_profile="recipe",
    )
    (tmp_path / "prompts" / "demo_scratch_v1.txt").write_text("INPUT={{INPUT_PATH}}\n", encoding="utf-8")
    _write_json(tmp_path / "schemas" / "demo_scratch_v1.schema.json", _demo_schema_payload())
    _write_heads_up_assets(tmp_path)

    report = lint_pack(root=tmp_path)

    assert report.error_count == 0


def test_lint_pack_reports_duplicate_pipeline_ids(tmp_path: Path) -> None:
    _make_pack_root(tmp_path)
    _write_pipeline(
        tmp_path,
        filename="demo.dup.one.json",
        pipeline_id="demo.dup.v1",
        prompt_rel="prompts/demo_dup_v1.txt",
        schema_rel="schemas/demo_dup_v1.schema.json",
    )
    _write_pipeline(
        tmp_path,
        filename="demo.dup.two.json",
        pipeline_id="demo.dup.v1",
        prompt_rel="prompts/demo_dup_v1.txt",
        schema_rel="schemas/demo_dup_v1.schema.json",
    )
    (tmp_path / "prompts" / "demo_dup_v1.txt").write_text("INPUT={{INPUT_PATH}}\n", encoding="utf-8")
    _write_json(tmp_path / "schemas" / "demo_dup_v1.schema.json", _demo_schema_payload())
    _write_heads_up_assets(tmp_path)

    report = lint_pack(root=tmp_path)

    assert any(finding.code == "pipeline.duplicate_id" for finding in report.findings)


def test_lint_pack_reports_missing_path_prompt_token_for_default_mode(tmp_path: Path) -> None:
    _make_pack_root(tmp_path)
    _write_pipeline(
        tmp_path,
        filename="demo.path.token.v1.json",
        pipeline_id="demo.path.token.v1",
        prompt_rel="prompts/demo_path_token_v1.txt",
        schema_rel="schemas/demo_path_token_v1.schema.json",
    )
    (tmp_path / "prompts" / "demo_path_token_v1.txt").write_text("INPUT={{INPUT_TEXT}}\n", encoding="utf-8")
    _write_json(tmp_path / "schemas" / "demo_path_token_v1.schema.json", _demo_schema_payload())
    _write_heads_up_assets(tmp_path)

    report = lint_pack(root=tmp_path)

    assert any(
        finding.code == "pipeline.prompt_missing_required_token"
        and finding.pipeline_id == "demo.path.token.v1"
        for finding in report.findings
    )


def test_lint_pack_reports_invalid_schema_json(tmp_path: Path) -> None:
    _make_pack_root(tmp_path)
    _write_pipeline(
        tmp_path,
        filename="demo.bad.json.v1.json",
        pipeline_id="demo.bad.json.v1",
        prompt_rel="prompts/demo_bad_json_v1.txt",
        schema_rel="schemas/demo_bad_json_v1.schema.json",
    )
    (tmp_path / "prompts" / "demo_bad_json_v1.txt").write_text("INPUT={{INPUT_PATH}}\n", encoding="utf-8")
    (tmp_path / "schemas" / "demo_bad_json_v1.schema.json").write_text("{\n", encoding="utf-8")
    _write_heads_up_assets(tmp_path)

    report = lint_pack(root=tmp_path)

    invalid_json_findings = [
        finding
        for finding in report.findings
        if finding.code == "schema.invalid_json"
        and finding.path.endswith("demo_bad_json_v1.schema.json")
    ]
    assert invalid_json_findings
    assert not any(
        finding.code == "schema.invalid_definition"
        and finding.path.endswith("demo_bad_json_v1.schema.json")
        for finding in report.findings
    )


def test_lint_pack_reports_invalid_schema_definition(tmp_path: Path) -> None:
    _make_pack_root(tmp_path)
    _write_pipeline(
        tmp_path,
        filename="demo.bad.definition.v1.json",
        pipeline_id="demo.bad.definition.v1",
        prompt_rel="prompts/demo_bad_definition_v1.txt",
        schema_rel="schemas/demo_bad_definition_v1.schema.json",
    )
    (tmp_path / "prompts" / "demo_bad_definition_v1.txt").write_text(
        "INPUT={{INPUT_PATH}}\n",
        encoding="utf-8",
    )
    _write_json(
        tmp_path / "schemas" / "demo_bad_definition_v1.schema.json",
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": 7,
            "properties": {},
            "required": [],
        },
    )
    _write_heads_up_assets(tmp_path)

    report = lint_pack(root=tmp_path)

    assert any(finding.code == "schema.invalid_definition" for finding in report.findings)


def test_lint_schema_file_warns_when_properties_are_not_required(tmp_path: Path) -> None:
    schema_path = tmp_path / "bad_required.schema.json"
    _write_json(
        schema_path,
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            },
            "required": [],
            "additionalProperties": False,
        },
    )

    report = lint_schema_file(schema_path=schema_path)

    assert report.error_count == 0
    assert report.warning_count >= 1
    assert any(finding.code == "schema.properties_not_in_required" for finding in report.findings)
    assert lint_exit_code(report, strict=False) == 0
    assert lint_exit_code(report, strict=True) == 1
