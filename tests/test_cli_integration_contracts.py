import csv
import json
from pathlib import Path

from typer.testing import CliRunner

from codex_farm.cli import app
from codex_farm.codex_exec import CodexExecResult
from codex_farm.doctor import CheckResult
from codex_farm.db import (
    create_run,
    enqueue_tasks_for_run,
    get_run,
    init_db,
    lease_one_task,
    mark_task_done,
    mark_task_error,
    open_db,
    run_status,
    set_run_control_state,
    upsert_heads_up_tips,
)
from codex_farm.forensics import FailureForensicsRequest, capture_failure_forensics


runner = CliRunner()


def _write_pipeline_pack(
    root: Path,
    pipeline_id: str,
    *,
    codex_execution_context: str | None = None,
    codex_home_profile: str | None = None,
) -> None:
    for folder in ("pipelines", "prompts", "schemas"):
        (root / folder).mkdir(parents=True, exist_ok=True)

    slug = pipeline_id.replace(".", "_")
    pipeline_path = root / "pipelines" / f"{pipeline_id}.json"
    prompt_rel = Path("prompts") / f"{slug}.txt"
    schema_rel = Path("schemas") / f"{slug}.schema.json"

    pipeline_payload = {
        "pipeline_id": pipeline_id,
        "description": f"Pipeline {pipeline_id}",
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
    }
    if codex_execution_context is not None:
        pipeline_payload["codex_execution_context"] = codex_execution_context
    if codex_home_profile is not None:
        pipeline_payload["codex_home_profile"] = codex_home_profile
    (root / "pipelines" / f"{pipeline_id}.json").write_text(
        json.dumps(pipeline_payload, indent=2) + "\n",
        encoding="utf-8",
    )

    (root / prompt_rel).write_text("Input file path: {{INPUT_PATH}}\n", encoding="utf-8")

    schema_payload = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["ok", "source_path"],
        "properties": {
            "ok": {"type": "string"},
            "source_path": {"type": "string"},
        },
    }
    (root / schema_rel).write_text(json.dumps(schema_payload, indent=2) + "\n", encoding="utf-8")


def _write_benchmark_pipeline_pack(root: Path, pipeline_id: str) -> None:
    for folder in ("pipelines", "prompts", "schemas"):
        (root / folder).mkdir(parents=True, exist_ok=True)

    slug = pipeline_id.replace(".", "_")
    prompt_rel = Path("prompts") / f"{slug}.txt"
    schema_rel = Path("schemas") / f"{slug}.schema.json"

    pipeline_payload = {
        "pipeline_id": pipeline_id,
        "description": f"Benchmark pipeline {pipeline_id}",
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
    }
    (root / "pipelines" / f"{pipeline_id}.json").write_text(
        json.dumps(pipeline_payload, indent=2) + "\n",
        encoding="utf-8",
    )
    (root / prompt_rel).write_text("Input file path: {{INPUT_PATH}}\n", encoding="utf-8")
    (root / schema_rel).write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["line_predictions"],
                "properties": {
                    "line_predictions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": [
                                "line_index",
                                "label",
                                "confidence",
                                "evidence_line_indices",
                                "reasoning_tags",
                            ],
                            "properties": {
                                "line_index": {"type": "integer", "minimum": 0},
                                "label": {"type": "string", "minLength": 1},
                                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                                "evidence_line_indices": {
                                    "type": "array",
                                    "items": {"type": "integer", "minimum": 0},
                                },
                                "reasoning_tags": {
                                    "type": "array",
                                    "items": {"type": "string", "minLength": 1},
                                },
                            },
                        },
                    }
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_heads_up_assets(root: Path) -> None:
    (root / "prompts" / "heads_up_distiller_v1.txt").write_text(
        "Return only tipset JSON.\n",
        encoding="utf-8",
    )
    (root / "schemas" / "heads_up_tipset_v1.schema.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["tips"],
                "properties": {
                    "tips": {
                        "type": "array",
                        "items": {"type": "string"},
                    }
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def test_lint_json_contract_clean_pack(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    _write_pipeline_pack(pack, "demo.lint.clean.v1")
    _write_heads_up_assets(pack)

    result = runner.invoke(
        app,
        [
            "lint",
            "--root",
            str(pack),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["target"]["kind"] == "pack"
    assert payload["target"]["root"] == str(pack.resolve())
    assert payload["ok"] is True
    assert payload["error_count"] == 0
    assert payload["warning_count"] == 0
    assert payload["scanned"]["pipeline_files"] == 1
    assert payload["scanned"]["schema_files"] >= 1
    assert payload["findings"] == []


def test_lint_json_contract_broken_pack_reports_multiple_findings(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    _write_pipeline_pack(pack, "demo.lint.broken.v1")

    (pack / "prompts" / "demo_lint_broken_v1.txt").unlink()
    (pack / "schemas" / "demo_lint_broken_v1.schema.json").write_text("{\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "lint",
            "--root",
            str(pack),
            "--json",
        ],
    )

    assert result.exit_code == 1, result.stderr
    payload = json.loads(result.stdout)
    codes = {row["code"] for row in payload["findings"]}
    assert payload["target"]["kind"] == "pack"
    assert payload["ok"] is False
    assert payload["error_count"] >= 2
    assert "pipeline.missing_prompt_template" in codes
    assert "schema.invalid_json" in codes


def test_lint_schema_json_contract_and_strict_exit(tmp_path: Path) -> None:
    schema_path = tmp_path / "caller.schema.json"
    schema_path.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                },
                "required": [],
                "additionalProperties": False,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "lint",
            "--schema",
            str(schema_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["target"]["kind"] == "schema"
    assert payload["target"]["path"] == str(schema_path.resolve())
    assert payload["ok"] is True
    assert payload["error_count"] == 0
    assert payload["warning_count"] >= 1
    assert any(
        row["code"] == "schema.properties_not_in_required"
        for row in payload["findings"]
    )

    strict_result = runner.invoke(
        app,
        [
            "lint",
            "--schema",
            str(schema_path),
            "--strict",
            "--json",
        ],
    )
    assert strict_result.exit_code == 1, strict_result.stderr


def test_lint_reports_missing_sentinels_for_explicit_near_miss_root(tmp_path: Path) -> None:
    near_miss = tmp_path / "near_miss"
    (near_miss / "pipelines").mkdir(parents=True)

    result = runner.invoke(
        app,
        [
            "lint",
            "--root",
            str(near_miss),
            "--json",
        ],
    )

    assert result.exit_code == 1, result.stderr
    payload = json.loads(result.stdout)
    assert any(
        row["code"] == "pack.missing_sentinel_dirs"
        for row in payload["findings"]
    )


def test_pipelines_list_root_override_wins_over_env(tmp_path: Path) -> None:
    env_pack = tmp_path / "env_pack"
    root_pack = tmp_path / "root_pack"
    _write_pipeline_pack(env_pack, "env.pipeline.v1")
    _write_pipeline_pack(root_pack, "root.pipeline.v1")

    result = runner.invoke(
        app,
        [
            "pipelines",
            "list",
            "--root",
            str(root_pack),
            "--json",
        ],
        env={"CODEX_FARM_ROOT": str(env_pack)},
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    pipeline_ids = {row["pipeline_id"] for row in payload}
    assert pipeline_ids == {"root.pipeline.v1"}


def test_models_list_json_contract(monkeypatch) -> None:
    def fake_list_codex_models():
        return [
            {
                "slug": "gpt-5.3-codex",
                "display_name": "GPT 5.3 Codex",
                "description": "Primary model",
                "supported_reasoning_efforts": ["none", "low", "high"],
            },
            {
                "slug": "gpt-5.3-codex-mini",
                "display_name": "GPT 5.3 Codex Mini",
                "description": "",
            },
        ]

    monkeypatch.setattr("codex_farm.cli.list_codex_models", fake_list_codex_models)

    result = runner.invoke(app, ["models", "list", "--json"])

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert [row["slug"] for row in payload] == ["gpt-5.3-codex", "gpt-5.3-codex-mini"]
    assert payload[0]["supported_reasoning_efforts"] == ["none", "low", "high"]


def test_process_json_stdout_contract_and_workspace_root(monkeypatch, tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    workspace_root = tmp_path / "workspace"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    data_dir = tmp_path / "var"
    schema_override_path = tmp_path / "caller.schema.json"
    pipeline_id = "demo.contract.v1"

    _write_pipeline_pack(pack, pipeline_id)
    schema_override_path.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["ok", "source_path"],
                "properties": {
                    "ok": {"type": "string"},
                    "source_path": {"type": "string"},
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    workspace_root.mkdir(parents=True)
    input_dir.mkdir(parents=True)
    (input_dir / "a.json").write_text("{}", encoding="utf-8")
    (input_dir / "b.json").write_text("{}", encoding="utf-8")

    captured_cd_dirs: list[str] = []
    captured_models: list[str] = []
    captured_efforts: list[str] = []
    captured_schema_paths: list[str] = []
    captured_logical_schema_paths: list[str] = []

    def fake_run_codex_exec(**kwargs):
        captured_cd_dirs.append(str(kwargs["cd_dir"]))
        captured_models.append(str(kwargs["model"]))
        captured_efforts.append(str(kwargs.get("reasoning_effort")))
        captured_schema_paths.append(str(kwargs["output_schema"]))
        captured_logical_schema_paths.append(str(kwargs.get("output_schema_logical_path")))
        output_path = kwargs["output_path"]
        output_path.parent.mkdir(parents=True, exist_ok=True)

        prompt_line = kwargs["prompt"].strip().splitlines()[-1]
        input_path = prompt_line.replace("Input file path: ", "")

        output_path.write_text(
            json.dumps({"ok": "OK", "source_path": input_path}),
            encoding="utf-8",
        )
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)

    result = runner.invoke(
        app,
        [
            "process",
            "--root",
            str(pack),
            "--workspace-root",
            str(workspace_root),
            "--pipeline",
            pipeline_id,
            "--in",
            str(input_dir),
            "--out",
            str(output_dir),
            "--workers",
            "2",
            "--model",
            "gpt-test-override",
            "--reasoning-effort",
            "high",
            "--output-schema",
            str(schema_override_path),
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr

    payload = json.loads(result.stdout)
    assert payload["pipeline_id"] == pipeline_id
    assert payload["status"] == "done"
    assert payload["counts"]["done"] == 2
    assert payload["counts"]["error"] == 0
    assert payload["counts"]["total"] == 2
    assert payload["input_dir"] == str(input_dir.resolve())
    assert payload["output_dir"] == str(output_dir.resolve())
    assert payload["farm_root"] == str(pack.resolve())
    assert payload["workspace_root"] == str(workspace_root.resolve())
    assert payload["codex_execution_context"] == "project"
    assert payload["codex_home_path"] is None
    assert payload["codex_model"] == "gpt-test-override"
    assert payload["codex_reasoning_effort"] == "high"
    assert payload["output_schema_path"] == str(schema_override_path.resolve())
    assert payload["heads_up_enabled"] is False
    assert payload["heads_up_max_tips"] == 3
    assert payload["heads_up_tips_applied"] == 0
    assert payload["heads_up_tips_added"] == 0
    assert payload["incremental"] == {
        "enabled": False,
        "source_run_id": None,
        "reused": 0,
        "queued": 0,
        "fallback_counts": {
            "no_prior_success": 0,
            "hash_changed": 0,
            "source_output_missing": 0,
            "source_output_invalid": 0,
        },
    }
    assert isinstance(payload["telemetry_report"], dict)
    assert payload["telemetry_report"]["schema_version"] == 2
    assert payload["telemetry_report"]["filters"]["run_id"] == payload["run_id"]
    assert payload["telemetry_report"]["filters"]["pipeline_id"] == pipeline_id
    assert payload["exit_code"] == 0
    conn = open_db(data_dir / "codex_farm.sqlite3")
    run = get_run(conn, payload["run_id"])
    run_config = json.loads(run["config_json"])
    assert isinstance(run_config.get("frozen_assets"), dict)
    process_manifest_relpath = str(run_config["frozen_assets"]["manifest_relpath"])
    assert (data_dir / process_manifest_relpath).exists()
    assert all(path == str(workspace_root.resolve()) for path in captured_cd_dirs)
    assert captured_models == ["gpt-test-override", "gpt-test-override"]
    assert captured_efforts == ["high", "high"]
    assert captured_logical_schema_paths == [
        str(schema_override_path.resolve()),
        str(schema_override_path.resolve()),
    ]
    assert all(path != str(schema_override_path.resolve()) for path in captured_schema_paths)


def test_process_resolves_profile_codex_home_and_uses_scratch_context(
    monkeypatch,
    tmp_path: Path,
) -> None:
    pack = tmp_path / "pack"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    data_dir = tmp_path / "var"
    pipeline_id = "demo.scratch.contract.v1"
    codex_home_path = tmp_path / "recipe-home"

    _write_pipeline_pack(
        pack,
        pipeline_id,
        codex_execution_context="scratch",
        codex_home_profile="recipe",
    )
    input_dir.mkdir(parents=True)
    (input_dir / "a.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CODEX_FARM_CODEX_HOME_RECIPE", str(codex_home_path))

    captured_cd_dirs: list[str] = []
    captured_envs: list[dict[str, str] | None] = []

    def fake_run_codex_exec(**kwargs):
        captured_cd_dirs.append(str(kwargs["cd_dir"]))
        captured_envs.append(kwargs.get("env_overrides"))
        output_path = kwargs["output_path"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_line = kwargs["prompt"].strip().splitlines()[-1]
        input_path = prompt_line.replace("Input file path: ", "")
        output_path.write_text(
            json.dumps({"ok": "OK", "source_path": input_path}),
            encoding="utf-8",
        )
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)

    result = runner.invoke(
        app,
        [
            "process",
            "--root",
            str(pack),
            "--pipeline",
            pipeline_id,
            "--in",
            str(input_dir),
            "--out",
            str(output_dir),
            "--workers",
            "1",
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["codex_execution_context"] == "scratch"
    assert payload["codex_home_path"] == str(codex_home_path.resolve())
    assert len(captured_cd_dirs) == 1
    assert captured_cd_dirs[0].startswith(str((data_dir / "execution_contexts").resolve()))
    assert captured_envs == [{"CODEX_HOME": str(codex_home_path.resolve())}]

    conn = open_db(data_dir / "codex_farm.sqlite3")
    run = get_run(conn, payload["run_id"])
    run_config = json.loads(run["config_json"])
    assert run_config["codex_home_path"] == str(codex_home_path.resolve())


def test_process_login_precheck_fails_fast_before_run_creation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("CODEX_FARM_SKIP_LOGIN_PRECHECK", raising=False)

    def fake_execution_checks(
        login_timeout_seconds: int = 20,
        smoke_timeout_seconds: int = 60,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ):
        return (
            [
                CheckResult(
                    name="codex login status",
                    ok=False,
                    detail="Not logged in using ChatGPT",
                ),
                CheckResult(
                    name="codex non-interactive check",
                    ok=False,
                    detail="Skipped because login status check failed",
                ),
            ],
            False,
        )

    monkeypatch.setattr("codex_farm.cli.run_codex_execution_checks", fake_execution_checks)

    pack = tmp_path / "pack"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    data_dir = tmp_path / "var"
    pipeline_id = "demo.contract.precheck.v1"
    _write_pipeline_pack(pack, pipeline_id)
    input_dir.mkdir(parents=True)
    (input_dir / "a.json").write_text("{}", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "process",
            "--root",
            str(pack),
            "--pipeline",
            pipeline_id,
            "--in",
            str(input_dir),
            "--out",
            str(output_dir),
            "--data-dir",
            str(data_dir),
        ],
    )

    assert result.exit_code == 1
    assert "codex execution precheck failed before `process`" in result.stderr
    db_path = data_dir / "codex_farm.sqlite3"
    assert not db_path.exists()


def test_process_login_precheck_fails_fast_on_websocket_auth_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("CODEX_FARM_SKIP_LOGIN_PRECHECK", raising=False)

    def fake_execution_checks(
        login_timeout_seconds: int = 20,
        smoke_timeout_seconds: int = 60,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ):
        return (
            [
                CheckResult(
                    name="codex login status",
                    ok=True,
                    detail="Logged in using ChatGPT",
                ),
                CheckResult(
                    name="codex non-interactive check",
                    ok=False,
                    detail=(
                        "WebSocket error: HTTP 403 Forbidden "
                        "wss://chatgpt.com/backend-api/codex/responses; "
                        "authentication appears invalid for this machine. "
                        "Run `codex` once and sign in with ChatGPT."
                    ),
                ),
            ],
            False,
        )

    monkeypatch.setattr("codex_farm.cli.run_codex_execution_checks", fake_execution_checks)

    pack = tmp_path / "pack"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    data_dir = tmp_path / "var"
    pipeline_id = "demo.contract.precheck.websocket.v1"
    _write_pipeline_pack(pack, pipeline_id)
    input_dir.mkdir(parents=True)
    (input_dir / "a.json").write_text("{}", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "process",
            "--root",
            str(pack),
            "--pipeline",
            pipeline_id,
            "--in",
            str(input_dir),
            "--out",
            str(output_dir),
            "--data-dir",
            str(data_dir),
        ],
    )

    assert result.exit_code == 1
    assert "codex execution precheck failed before `process`" in result.stderr
    assert "403 Forbidden" in result.stderr
    db_path = data_dir / "codex_farm.sqlite3"
    assert not db_path.exists()


def test_process_no_login_precheck_bypasses_login_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("CODEX_FARM_SKIP_LOGIN_PRECHECK", raising=False)

    def fake_execution_checks(
        login_timeout_seconds: int = 20,
        smoke_timeout_seconds: int = 60,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ):
        return (
            [
                CheckResult(
                    name="codex login status",
                    ok=False,
                    detail="Not logged in using ChatGPT",
                ),
                CheckResult(
                    name="codex non-interactive check",
                    ok=False,
                    detail="Skipped because login status check failed",
                ),
            ],
            False,
        )

    monkeypatch.setattr("codex_farm.cli.run_codex_execution_checks", fake_execution_checks)

    pack = tmp_path / "pack"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    data_dir = tmp_path / "var"
    pipeline_id = "demo.contract.no.precheck.v1"
    _write_pipeline_pack(pack, pipeline_id)
    input_dir.mkdir(parents=True)
    (input_dir / "a.json").write_text("{}", encoding="utf-8")

    def fake_run_codex_exec(**kwargs):
        output_path = kwargs["output_path"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_line = kwargs["prompt"].strip().splitlines()[-1]
        input_path = prompt_line.replace("Input file path: ", "")
        output_path.write_text(
            json.dumps({"ok": "OK", "source_path": input_path}),
            encoding="utf-8",
        )
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)

    result = runner.invoke(
        app,
        [
            "process",
            "--root",
            str(pack),
            "--pipeline",
            pipeline_id,
            "--in",
            str(input_dir),
            "--out",
            str(output_dir),
            "--data-dir",
            str(data_dir),
            "--no-login-precheck",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "done"
    assert payload["counts"]["done"] == 1


def test_process_login_precheck_uses_selected_model_and_effort(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("CODEX_FARM_SKIP_LOGIN_PRECHECK", raising=False)
    captured: list[dict[str, str | None]] = []

    def fake_execution_checks(
        login_timeout_seconds: int = 20,
        smoke_timeout_seconds: int = 60,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ):
        captured.append(
            {
                "model": model,
                "reasoning_effort": reasoning_effort,
            }
        )
        return (
            [
                CheckResult(
                    name="codex login status",
                    ok=True,
                    detail="Logged in using ChatGPT",
                ),
                CheckResult(
                    name="codex non-interactive check",
                    ok=True,
                    detail="OK",
                ),
            ],
            True,
        )

    monkeypatch.setattr("codex_farm.cli.run_codex_execution_checks", fake_execution_checks)

    pack = tmp_path / "pack"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    data_dir = tmp_path / "var"
    pipeline_id = "demo.contract.precheck.model.v1"
    _write_pipeline_pack(pack, pipeline_id)
    input_dir.mkdir(parents=True)
    (input_dir / "a.json").write_text("{}", encoding="utf-8")

    def fake_run_codex_exec(**kwargs):
        output_path = kwargs["output_path"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_line = kwargs["prompt"].strip().splitlines()[-1]
        input_path = prompt_line.replace("Input file path: ", "")
        output_path.write_text(
            json.dumps({"ok": "OK", "source_path": input_path}),
            encoding="utf-8",
        )
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)

    result = runner.invoke(
        app,
        [
            "process",
            "--root",
            str(pack),
            "--pipeline",
            pipeline_id,
            "--in",
            str(input_dir),
            "--out",
            str(output_dir),
            "--data-dir",
            str(data_dir),
            "--model",
            "gpt-5.1-codex-mini",
            "--reasoning-effort",
            "medium",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert captured == [
        {
            "model": "gpt-5.1-codex-mini",
            "reasoning_effort": "medium",
        }
    ]


def test_worker_run_id_login_precheck_uses_persisted_codex_home(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("CODEX_FARM_SKIP_LOGIN_PRECHECK", raising=False)
    data_dir = tmp_path / "var"
    data_dir.mkdir(parents=True)
    conn = open_db(data_dir / "codex_farm.sqlite3")
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id="demo.worker.precheck.v1",
        input_dir=str((tmp_path / "input").resolve()),
        glob="**/*.json",
        output_dir=str((tmp_path / "output").resolve()),
        config={
            "farm_root": str(tmp_path.resolve()),
            "codex_home_path": str((tmp_path / "recipe-home").resolve()),
        },
    )

    captured: list[dict[str, object]] = []

    def fake_execution_checks(
        login_timeout_seconds: int = 20,
        smoke_timeout_seconds: int = 60,
        model: str | None = None,
        reasoning_effort: str | None = None,
        env_overrides: dict[str, str] | None = None,
    ):
        captured.append(
            {
                "model": model,
                "reasoning_effort": reasoning_effort,
                "env_overrides": env_overrides,
            }
        )
        return (
            [
                CheckResult(name="codex login status", ok=True, detail="Logged in using ChatGPT"),
                CheckResult(name="codex non-interactive check", ok=True, detail="OK"),
            ],
            True,
        )

    monkeypatch.setattr("codex_farm.cli.run_codex_execution_checks", fake_execution_checks)
    monkeypatch.setattr("codex_farm.cli.worker_loop", lambda **kwargs: 0)

    result = runner.invoke(
        app,
        [
            "worker",
            "--data-dir",
            str(data_dir),
            "--run-id",
            run_id,
            "--once",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert captured == [
        {
            "model": None,
            "reasoning_effort": None,
            "env_overrides": {"CODEX_HOME": str((tmp_path / "recipe-home").resolve())},
        }
    ]


def test_run_progress_json_contract(tmp_path: Path) -> None:
    data_dir = tmp_path / "var"
    db_path = data_dir / "codex_farm.sqlite3"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    first_input = input_dir / "a.json"
    second_input = input_dir / "b.json"
    first_input.write_text("{}", encoding="utf-8")
    second_input.write_text("{}", encoding="utf-8")

    conn = open_db(db_path)
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id="demo.progress.v1",
        input_dir=str(input_dir),
        glob="**/*.json",
        output_dir=str(output_dir),
        config={},
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[first_input, second_input],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=".json",
    )
    running_task = lease_one_task(conn, worker_id="w1", lease_seconds=30, run_id=run_id)
    assert running_task is not None
    errored_task = lease_one_task(conn, worker_id="w2", lease_seconds=30, run_id=run_id)
    assert errored_task is not None
    mark_task_error(
        conn,
        task_id=errored_task["task_id"],
        error="simulated terminal failure",
    )

    result = runner.invoke(
        app,
        [
            "run",
            "progress",
            "--run-id",
            run_id,
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["run_id"] == run_id
    assert payload["pipeline_id"] == "demo.progress.v1"
    assert payload["status"] == "running"
    assert payload["counts"]["total"] == 2
    assert payload["counts"]["running"] == 1
    assert payload["counts"]["error"] == 1
    assert payload["progress"]["completed"] == 1
    assert payload["progress"]["remaining"] == 1
    assert payload["progress"]["percent_complete"] == 50.0
    assert len(payload["running_tasks"]) == 1
    assert payload["running_tasks"][0]["task_id"] == running_task["task_id"]
    assert len(payload["recent_errors"]) == 1
    assert payload["recent_errors"][0]["task_id"] == errored_task["task_id"]
    assert isinstance(payload["snapshot_at_utc"], str) and payload["snapshot_at_utc"]


def test_run_progress_watch_json_stops_on_terminal_run(tmp_path: Path) -> None:
    data_dir = tmp_path / "var"
    db_path = data_dir / "codex_farm.sqlite3"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    source = input_dir / "a.json"
    source.write_text("{}", encoding="utf-8")

    conn = open_db(db_path)
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id="demo.progress.watch.v1",
        input_dir=str(input_dir),
        glob="**/*.json",
        output_dir=str(output_dir),
        config={},
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[source],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=".json",
    )
    leased = lease_one_task(conn, worker_id="w1", lease_seconds=30, run_id=run_id)
    assert leased is not None
    output_path = output_dir / "a.json"
    output_path.write_text("{}", encoding="utf-8")
    mark_task_done(
        conn,
        task_id=leased["task_id"],
        output_path=str(output_path),
    )

    result = runner.invoke(
        app,
        [
            "run",
            "progress",
            "--run-id",
            run_id,
            "--data-dir",
            str(data_dir),
            "--watch",
            "--poll-seconds",
            "0.1",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["run_id"] == run_id
    assert payload["status"] == "done"
    assert payload["counts"]["done"] == 1


def test_process_progress_events_emit_machine_readable_stderr(
    monkeypatch,
    tmp_path: Path,
) -> None:
    pack = tmp_path / "pack"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    data_dir = tmp_path / "var"
    pipeline_id = "demo.progress.events.v1"
    _write_pipeline_pack(pack, pipeline_id)
    input_dir.mkdir(parents=True)
    (input_dir / "a.json").write_text("{}", encoding="utf-8")

    def fake_run_codex_exec(**kwargs):
        output_path = kwargs["output_path"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_line = kwargs["prompt"].strip().splitlines()[-1]
        input_path = prompt_line.replace("Input file path: ", "")
        output_path.write_text(
            json.dumps(
                {
                    "ok": "OK",
                    "source_path": input_path,
                }
            ),
            encoding="utf-8",
        )
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)

    result = runner.invoke(
        app,
        [
            "process",
            "--root",
            str(pack),
            "--pipeline",
            pipeline_id,
            "--in",
            str(input_dir),
            "--out",
            str(output_dir),
            "--workers",
            "1",
            "--data-dir",
            str(data_dir),
            "--progress-events",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "done"
    assert payload["progress_events_enabled"] is True

    event_prefix = "__codex_farm_progress__ "
    event_lines = [
        line
        for line in result.stderr.splitlines()
        if line.startswith(event_prefix)
    ]
    assert event_lines
    events = [json.loads(line[len(event_prefix) :]) for line in event_lines]
    assert events[0]["event"] == "run_started"
    assert any(event["event"] == "run_progress" for event in events)
    assert events[-1]["event"] == "run_finished"
    assert events[0]["run_id"] == payload["run_id"]
    assert events[-1]["run_id"] == payload["run_id"]
    assert events[-1]["exit_code"] == 0
    assert events[-1]["counts"]["done"] == 1


def test_run_telemetry_json_contract(tmp_path: Path) -> None:
    data_dir = tmp_path / "var"
    db_path = data_dir / "codex_farm.sqlite3"
    csv_path = data_dir / "codex_exec_activity.csv"
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    sample_input = input_dir / "a.json"
    sample_input.write_text("{}", encoding="utf-8")

    conn = open_db(db_path)
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id="demo.contract.v1",
        input_dir=str(input_dir),
        glob="**/*.json",
        output_dir=str(output_dir),
        config={},
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[sample_input],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=".json",
    )
    task = lease_one_task(conn, worker_id="w1", lease_seconds=30, run_id=run_id)
    assert task is not None
    mark_task_error(
        conn,
        task_id=task["task_id"],
        error=(
            "Schema validation failed at recipeInstructions[0].text: "
            "'text' is a required property"
        ),
    )

    rows = [
        {
            "logged_at_utc": "2026-02-28T15:12:00.000Z",
            "status": "failed",
            "run_id": run_id,
            "pipeline_id": "demo.contract.v1",
            "source": "worker",
            "duration_ms": "2000",
            "tokens_total": "400",
            "tokens_reasoning": "220",
            "failure_category": "nonzero_exit_no_payload",
            "retry_context_applied": "true",
            "retry_previous_error": (
                "Schema validation failed at recipeInstructions[0].text: "
                "'text' is a required property"
            ),
            "retry_previous_error_sha256": "abc123",
            "heads_up_applied": "false",
            "heads_up_tip_count": "0",
            "heads_up_tip_ids_json": "[]",
            "heads_up_tip_texts_json": "[]",
            "heads_up_tip_scores_json": "[]",
            "rate_limit_suspected": "true",
            "accepted_nonzero_exit": "false",
            "output_payload_present": "false",
            "output_preview_truncated": "false",
            "attempt_index": "2",
            "stderr_tail": "HTTP 429 Too Many Requests",
            "prompt_sha256": "prompt-a",
            "output_sha256": "",
        }
    ]
    fieldnames = sorted({key for row in rows for key in row})
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    result = runner.invoke(
        app,
        [
            "run",
            "telemetry",
            "--run-id",
            run_id,
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 2
    assert payload["filters"]["run_id"] == run_id
    assert payload["filters"]["pipeline_id"] is None
    assert payload["matched_rows"] == 1
    assert payload["summary"]["status_counts"]["failed"] == 1
    assert payload["summary"]["tokens_reasoning_total"] == 220
    assert isinstance(payload["insights"], dict)
    assert isinstance(payload["tuning_playbook"], dict)
    assert payload["terminal_errors"]["count"] == 1
    assert payload["insights"]["pass_forward_effectiveness"]["retry_context"]["rows_applied"] == 1
    recommendation_codes = {
        row["code"]
        for category in payload["recommendations"].values()
        for row in category
    }
    assert "prompt.raw_json_only_guardrail" in recommendation_codes
    assert "runtime.rate_limit_backoff" in recommendation_codes


def test_run_autotune_json_contract(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    data_dir = tmp_path / "var"
    db_path = data_dir / "codex_farm.sqlite3"
    csv_path = data_dir / "codex_exec_activity.csv"
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    sample_input = input_dir / "a.json"
    sample_input.write_text("{}", encoding="utf-8")

    pipeline_id = "demo.contract.v1"
    _write_pipeline_pack(pack, pipeline_id)

    conn = open_db(db_path)
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id=pipeline_id,
        input_dir=str(input_dir.resolve()),
        glob="**/*.json",
        output_dir=str(output_dir.resolve()),
        config={
            "farm_root": str(pack.resolve()),
            "workers": 8,
            "codex_model": "gpt-5.3-codex-spark",
            "codex_reasoning_effort": "high",
        },
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[sample_input],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=".json",
    )

    rows = [
        {
            "logged_at_utc": "2026-02-28T15:12:00.000Z",
            "status": "failed",
            "run_id": run_id,
            "pipeline_id": pipeline_id,
            "source": "worker",
            "duration_ms": "2000",
            "tokens_total": "400",
            "failure_category": "nonzero_exit_no_payload",
            "retry_context_applied": "true",
            "retry_previous_error": "Schema validation failed at recipeInstructions[0].text: bad",
            "retry_previous_error_sha256": "abc123",
            "heads_up_applied": "true",
            "heads_up_tip_count": "1",
            "heads_up_tip_ids_json": json.dumps(["tip-1"]),
            "heads_up_tip_texts_json": json.dumps(["Return only JSON."]),
            "heads_up_tip_scores_json": json.dumps([0.9]),
            "rate_limit_suspected": "true",
            "accepted_nonzero_exit": "false",
            "output_payload_present": "false",
            "output_preview_truncated": "false",
            "attempt_index": "2",
            "stderr_tail": "HTTP 429 Too Many Requests",
            "prompt_sha256": "prompt-a",
            "prompt_text": "Return JSON",
            "output_sha256": "",
            "model": "gpt-5.3-codex-spark",
            "reasoning_effort": "high",
            "input_path": str(input_dir / "a.json"),
            "codex_event_count": "0",
            "codex_event_types_json": "[]",
        },
        {
            "logged_at_utc": "2026-02-28T15:11:00.000Z",
            "status": "failed",
            "run_id": run_id,
            "pipeline_id": pipeline_id,
            "source": "worker",
            "duration_ms": "1900",
            "tokens_total": "390",
            "failure_category": "nonzero_exit_no_payload",
            "retry_context_applied": "true",
            "retry_previous_error": "Schema validation failed at recipeInstructions[0].text: bad",
            "retry_previous_error_sha256": "abc123",
            "heads_up_applied": "true",
            "heads_up_tip_count": "1",
            "heads_up_tip_ids_json": json.dumps(["tip-1"]),
            "heads_up_tip_texts_json": json.dumps(["Return only JSON."]),
            "heads_up_tip_scores_json": json.dumps([0.9]),
            "rate_limit_suspected": "true",
            "accepted_nonzero_exit": "false",
            "output_payload_present": "false",
            "output_preview_truncated": "false",
            "attempt_index": "2",
            "stderr_tail": "HTTP 429 Too Many Requests",
            "prompt_sha256": "prompt-a",
            "prompt_text": "Return JSON",
            "output_sha256": "",
            "model": "gpt-5.3-codex-spark",
            "reasoning_effort": "high",
            "input_path": str(input_dir / "a.json"),
            "codex_event_count": "0",
            "codex_event_types_json": "[]",
        },
        {
            "logged_at_utc": "2026-02-28T15:10:00.000Z",
            "status": "ok",
            "run_id": run_id,
            "pipeline_id": pipeline_id,
            "source": "worker",
            "duration_ms": "900",
            "tokens_total": "120",
            "failure_category": "",
            "retry_context_applied": "false",
            "retry_previous_error": "",
            "retry_previous_error_sha256": "",
            "heads_up_applied": "false",
            "heads_up_tip_count": "0",
            "heads_up_tip_ids_json": "[]",
            "heads_up_tip_texts_json": "[]",
            "heads_up_tip_scores_json": "[]",
            "rate_limit_suspected": "false",
            "accepted_nonzero_exit": "false",
            "output_payload_present": "true",
            "output_preview_truncated": "false",
            "attempt_index": "1",
            "stderr_tail": "",
            "prompt_sha256": "prompt-b",
            "prompt_text": "Return JSON",
            "output_sha256": "sha-ok-1",
            "model": "gpt-5.3-codex-mini",
            "reasoning_effort": "low",
            "input_path": str(input_dir / "a.json"),
            "codex_event_count": "2",
            "codex_event_types_json": json.dumps(["thread.started", "turn.completed"]),
        },
        {
            "logged_at_utc": "2026-02-28T15:09:00.000Z",
            "status": "ok",
            "run_id": run_id,
            "pipeline_id": pipeline_id,
            "source": "worker",
            "duration_ms": "950",
            "tokens_total": "130",
            "failure_category": "",
            "retry_context_applied": "false",
            "retry_previous_error": "",
            "retry_previous_error_sha256": "",
            "heads_up_applied": "false",
            "heads_up_tip_count": "0",
            "heads_up_tip_ids_json": "[]",
            "heads_up_tip_texts_json": "[]",
            "heads_up_tip_scores_json": "[]",
            "rate_limit_suspected": "false",
            "accepted_nonzero_exit": "false",
            "output_payload_present": "true",
            "output_preview_truncated": "false",
            "attempt_index": "1",
            "stderr_tail": "",
            "prompt_sha256": "prompt-b",
            "prompt_text": "Return JSON",
            "output_sha256": "sha-ok-2",
            "model": "gpt-5.3-codex-mini",
            "reasoning_effort": "low",
            "input_path": str(input_dir / "a.json"),
            "codex_event_count": "2",
            "codex_event_types_json": json.dumps(["thread.started", "turn.completed"]),
        },
    ]
    fieldnames = sorted({key for row in rows for key in row})
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    result = runner.invoke(
        app,
        [
            "run",
            "autotune",
            "--run-id",
            run_id,
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["telemetry_schema_version"] == 2
    assert payload["run_id"] == run_id
    flags = {row["flag"]: row["suggested"] for row in payload["flag_overrides"]}
    assert flags["--workers"] == "4"
    assert flags["--model"] == "gpt-5.3-codex-mini"
    assert flags["--reasoning-effort"] == "low"
    assert "--workers 4" in payload["command_preview"]
    assert "--model gpt-5.3-codex-mini" in payload["command_preview"]
    assert isinstance(payload["prompt_template_diff"], dict)
    assert "Return only JSON matching the configured output schema." in payload["prompt_template_diff"]["diff"]
    assert isinstance(payload["pipeline_config_diff"], dict)
    assert "codex_model" in payload["pipeline_config_diff"]["diff"]


def test_one_command_uses_model_override(monkeypatch, tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "out.json"
    pipeline_id = "demo.contract.v1"

    _write_pipeline_pack(pack, pipeline_id)
    input_path.write_text("{}", encoding="utf-8")

    captured_models: list[str] = []
    captured_efforts: list[str] = []
    captured_trace_paths: list[Path] = []

    def fake_run_codex_exec(**kwargs):
        captured_models.append(str(kwargs["model"]))
        captured_efforts.append(str(kwargs.get("reasoning_effort")))
        captured_trace_paths.append(Path(kwargs["trace_output_path"]))
        resolved_output_path = kwargs["output_path"]
        resolved_output_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_line = kwargs["prompt"].strip().splitlines()[-1]
        source_path = prompt_line.replace("Input file path: ", "")
        resolved_output_path.write_text(
            json.dumps({"ok": "OK", "source_path": source_path}),
            encoding="utf-8",
        )
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    monkeypatch.setattr("codex_farm.cli.run_codex_exec", fake_run_codex_exec)

    result = runner.invoke(
        app,
        [
            "one",
            "--root",
            str(pack),
            "--pipeline",
            pipeline_id,
            "--in",
            str(input_path),
            "--out",
            str(output_path),
            "--model",
            "gpt-test-override",
            "--codex-thinking-effort",
            "low",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert captured_models == ["gpt-test-override"]
    assert captured_efforts == ["low"]
    assert len(captured_trace_paths) == 1
    assert captured_trace_paths[0].parent.name == ".codex-farm-traces"
    assert captured_trace_paths[0].name.startswith("one-out-")
    assert captured_trace_paths[0].name.endswith(".trace.json")


def test_one_command_uses_output_schema_override(monkeypatch, tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "out.json"
    schema_override_path = tmp_path / "caller.schema.json"
    pipeline_id = "demo.contract.v1"

    _write_pipeline_pack(pack, pipeline_id)
    input_path.write_text("{}", encoding="utf-8")
    schema_override_path.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["ok", "source_path", "must_be_present"],
                "properties": {
                    "ok": {"type": "string"},
                    "source_path": {"type": "string"},
                    "must_be_present": {"type": "string"},
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    captured_schema_paths: list[str] = []

    def fake_run_codex_exec(**kwargs):
        captured_schema_paths.append(str(kwargs["output_schema"]))
        resolved_output_path = kwargs["output_path"]
        resolved_output_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_line = kwargs["prompt"].strip().splitlines()[-1]
        source_path = prompt_line.replace("Input file path: ", "")
        resolved_output_path.write_text(
            json.dumps(
                {
                    "ok": "OK",
                    "source_path": source_path,
                    "must_be_present": "yes",
                }
            ),
            encoding="utf-8",
        )
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    monkeypatch.setattr("codex_farm.cli.run_codex_exec", fake_run_codex_exec)

    result = runner.invoke(
        app,
        [
            "one",
            "--root",
            str(pack),
            "--pipeline",
            pipeline_id,
            "--in",
            str(input_path),
            "--out",
            str(output_path),
            "--output-schema",
            str(schema_override_path),
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert captured_schema_paths == [str(schema_override_path.resolve())]


def test_one_command_applies_heads_up_tips_when_enabled(monkeypatch, tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "out.json"
    data_dir = tmp_path / "var"
    pipeline_id = "demo.contract.v1"

    _write_pipeline_pack(pack, pipeline_id)
    input_path.write_text("{}", encoding="utf-8")

    conn = open_db(data_dir / "codex_farm.sqlite3")
    init_db(conn)
    inserted = upsert_heads_up_tips(
        conn,
        pipeline_id=pipeline_id,
        source_run_id="run-1",
        tips=[
            {
                "input_signature": "json_obj_keys:",
                "tip_text": "Return raw JSON only.",
            }
        ],
    )
    assert inserted == 1

    captured_prompts: list[str] = []
    captured_usage_contexts: list[dict[str, object]] = []

    def fake_run_codex_exec(**kwargs):
        captured_prompts.append(str(kwargs["prompt"]))
        captured_usage_contexts.append(dict(kwargs["usage_context"]))
        resolved_output_path = kwargs["output_path"]
        resolved_output_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_line = kwargs["prompt"].strip().splitlines()[0]
        source_path = prompt_line.replace("Input file path: ", "")
        resolved_output_path.write_text(
            json.dumps({"ok": "OK", "source_path": source_path}),
            encoding="utf-8",
        )
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    monkeypatch.setattr("codex_farm.cli.run_codex_exec", fake_run_codex_exec)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        [
            "one",
            "--root",
            str(pack),
            "--pipeline",
            pipeline_id,
            "--in",
            str(input_path),
            "--out",
            str(output_path),
            "--heads-up",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert len(captured_prompts) == 1
    assert "Heads up for this task:" in captured_prompts[0]
    assert "Return raw JSON only." in captured_prompts[0]
    assert captured_usage_contexts[0]["heads_up_applied"] is True
    assert captured_usage_contexts[0]["heads_up_tip_count"] == 1
    assert json.loads(str(captured_usage_contexts[0]["heads_up_tip_texts_json"])) == [
        "Return raw JSON only."
    ]
    assert captured_usage_contexts[0]["attempt_index"] == 1
    assert captured_usage_contexts[0]["retry_context_applied"] is False


def test_run_create_persists_model_override_in_run_config(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    data_dir = tmp_path / "var"
    schema_override_path = tmp_path / "caller.schema.json"
    pipeline_id = "demo.contract.v1"

    _write_pipeline_pack(pack, pipeline_id)
    input_dir.mkdir(parents=True)
    (input_dir / "one.json").write_text("{}", encoding="utf-8")
    schema_override_path.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["ok", "source_path"],
                "properties": {
                    "ok": {"type": "string"},
                    "source_path": {"type": "string"},
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "run",
            "create",
            "--root",
            str(pack),
            "--pipeline",
            pipeline_id,
            "--in",
            str(input_dir),
            "--out",
            str(output_dir),
            "--data-dir",
            str(data_dir),
            "--model",
            "gpt-test-override",
            "--reasoning-effort",
            "xhigh",
            "--output-schema",
            str(schema_override_path),
            "--heads-up",
            "--heads-up-max-tips",
            "5",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    conn = open_db(data_dir / "codex_farm.sqlite3")
    run = get_run(conn, payload["run_id"])
    config = json.loads(run["config_json"])
    assert config["codex_model"] == "gpt-test-override"
    assert config["codex_reasoning_effort"] == "xhigh"
    assert config["output_schema_path_override"] == str(schema_override_path.resolve())
    assert config["heads_up_enabled"] is True
    assert config["heads_up_max_tips"] == 5
    assert isinstance(config.get("frozen_assets"), dict)
    frozen_assets = config["frozen_assets"]
    assert frozen_assets["version"] == 1
    manifest_relpath = str(frozen_assets["manifest_relpath"])
    manifest_path = data_dir / manifest_relpath
    assert manifest_path.exists()


def test_run_create_benchmark_mode_dispatches_to_benchmark_pipeline(
    tmp_path: Path,
) -> None:
    pack = tmp_path / "pack"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    data_dir = tmp_path / "var"
    _write_pipeline_pack(pack, "demo.contract.v1")
    _write_pipeline_pack(pack, "recipeimport.benchmark.line_label.v1")
    input_dir.mkdir(parents=True)
    (input_dir / "one.json").write_text("{}", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "run",
            "create",
            "--root",
            str(pack),
            "--pipeline",
            "demo.contract.v1",
            "--in",
            str(input_dir),
            "--out",
            str(output_dir),
            "--data-dir",
            str(data_dir),
            "--recipeimport-benchmark-mode",
            "line_label_v1",
            "--recipeimport-benchmark-debug",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["pipeline_id"] == "recipeimport.benchmark.line_label.v1"
    assert payload["recipeimport_benchmark_mode"] == "line_label_v1"
    assert payload["recipeimport_benchmark_debug"] is True
    conn = open_db(data_dir / "codex_farm.sqlite3")
    run = get_run(conn, payload["run_id"])
    assert run["pipeline_id"] == "recipeimport.benchmark.line_label.v1"
    config = json.loads(run["config_json"])
    assert config["pipeline"] == "recipeimport.benchmark.line_label.v1"
    assert config["recipeimport_benchmark_mode"] == "line_label_v1"
    assert config["recipeimport_benchmark_debug"] is True


def test_process_benchmark_mode_dispatches_to_benchmark_pipeline(
    monkeypatch,
    tmp_path: Path,
) -> None:
    pack = tmp_path / "pack"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    data_dir = tmp_path / "var"
    _write_pipeline_pack(pack, "demo.contract.v1")
    _write_benchmark_pipeline_pack(pack, "recipeimport.benchmark.line_label.v1")
    input_dir.mkdir(parents=True, exist_ok=True)
    (input_dir / "one.json").write_text(
        json.dumps(
            {
                "canonical_lines": [
                    {"line_index": 0, "text": "Line zero", "expected_label": "title"},
                ]
            }
        ),
        encoding="utf-8",
    )

    def fake_run_codex_exec(**kwargs):
        output_path = kwargs["output_path"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(
                {
                    "line_predictions": [
                        {
                            "line_index": 0,
                            "label": "title",
                            "confidence": 0.95,
                            "evidence_line_indices": [0],
                            "reasoning_tags": ["header_line"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)

    result = runner.invoke(
        app,
        [
            "process",
            "--root",
            str(pack),
            "--pipeline",
            "demo.contract.v1",
            "--in",
            str(input_dir),
            "--out",
            str(output_dir),
            "--data-dir",
            str(data_dir),
            "--workers",
            "1",
            "--no-login-precheck",
            "--recipeimport-benchmark-mode",
            "line_label_v1",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["pipeline_id"] == "recipeimport.benchmark.line_label.v1"
    assert payload["status"] == "done"
    conn = open_db(data_dir / "codex_farm.sqlite3")
    run = get_run(conn, payload["run_id"])
    assert run["pipeline_id"] == "recipeimport.benchmark.line_label.v1"
    run_config = json.loads(run["config_json"])
    assert run_config["pipeline"] == "recipeimport.benchmark.line_label.v1"
    assert run_config["recipeimport_benchmark_mode"] == "line_label_v1"


def test_process_rejects_missing_output_schema(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    missing_schema = tmp_path / "missing.schema.json"
    pipeline_id = "demo.contract.v1"
    _write_pipeline_pack(pack, pipeline_id)
    input_dir.mkdir(parents=True)
    (input_dir / "one.json").write_text("{}", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "process",
            "--root",
            str(pack),
            "--pipeline",
            pipeline_id,
            "--in",
            str(input_dir),
            "--out",
            str(output_dir),
            "--output-schema",
            str(missing_schema),
        ],
    )

    assert result.exit_code == 2
    assert "--output-schema must point to an existing JSON schema file:" in result.stderr


def test_go_benchmark_mode_dispatches_to_benchmark_pipeline(
    monkeypatch,
    tmp_path: Path,
) -> None:
    pack = tmp_path / "pack"
    data_dir = tmp_path / "var"
    _write_pipeline_pack(pack, "demo.contract.v1")
    _write_benchmark_pipeline_pack(pack, "recipeimport.benchmark.line_label.v1")

    inbox = data_dir / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "one.json").write_text(
        json.dumps(
            {
                "canonical_lines": [
                    {"line_index": 0, "text": "Line zero", "expected_label": "title"},
                ]
            }
        ),
        encoding="utf-8",
    )

    def fake_run_codex_exec(**kwargs):
        output_path = kwargs["output_path"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(
                {
                    "line_predictions": [
                        {
                            "line_index": 0,
                            "label": "title",
                            "confidence": 0.95,
                            "evidence_line_indices": [0],
                            "reasoning_tags": ["header_line"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)

    result = runner.invoke(
        app,
        [
            "go",
            "--root",
            str(pack),
            "--data-dir",
            str(data_dir),
            "--recipeimport-benchmark-mode",
            "line_label_v1",
            "--no-login-precheck",
        ],
        input="1\n1\n",
    )

    assert result.exit_code == 0, result.stderr
    conn = open_db(data_dir / "codex_farm.sqlite3")
    rows = conn.execute("SELECT run_id FROM runs").fetchall()
    assert len(rows) == 1
    run_id = str(rows[0]["run_id"])
    run = get_run(conn, run_id)
    assert run["pipeline_id"] == "recipeimport.benchmark.line_label.v1"
    run_config = json.loads(run["config_json"])
    assert run_config["pipeline"] == "recipeimport.benchmark.line_label.v1"
    assert run_config["recipeimport_benchmark_mode"] == "line_label_v1"
    status = run_status(conn, run_id=run_id)
    assert status["done"] == 1


def test_process_rejects_invalid_reasoning_effort(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    pipeline_id = "demo.contract.v1"
    _write_pipeline_pack(pack, pipeline_id)
    input_dir.mkdir(parents=True)
    (input_dir / "one.json").write_text("{}", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "process",
            "--root",
            str(pack),
            "--pipeline",
            pipeline_id,
            "--in",
            str(input_dir),
            "--out",
            str(output_dir),
            "--reasoning-effort",
            "ultra",
        ],
    )

    assert result.exit_code == 2
    assert "--reasoning-effort must be one of:" in result.stderr


def test_process_rejects_invalid_recipeimport_benchmark_mode(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    pipeline_id = "demo.contract.v1"
    _write_pipeline_pack(pack, pipeline_id)
    input_dir.mkdir(parents=True)
    (input_dir / "one.json").write_text("{}", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "process",
            "--root",
            str(pack),
            "--pipeline",
            pipeline_id,
            "--in",
            str(input_dir),
            "--out",
            str(output_dir),
            "--recipeimport-benchmark-mode",
            "unknown_mode",
        ],
    )

    assert result.exit_code == 2
    assert "--recipeimport-benchmark-mode must be one of:" in result.stderr


def test_run_create_json_contract(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    data_dir = tmp_path / "var"
    pipeline_id = "demo.contract.v1"

    _write_pipeline_pack(pack, pipeline_id)
    input_dir.mkdir(parents=True)
    (input_dir / "one.json").write_text("{}", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "run",
            "create",
            "--root",
            str(pack),
            "--pipeline",
            pipeline_id,
            "--in",
            str(input_dir),
            "--out",
            str(output_dir),
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["pipeline_id"] == pipeline_id
    assert payload["total"] == 1
    assert payload["input_dir"] == str(input_dir.resolve())
    assert payload["output_dir"] == str(output_dir.resolve())
    assert payload["codex_execution_context"] == "project"
    assert payload["codex_home_path"] is None
    assert payload["codex_model"] == "gpt-5.3-codex-spark"
    assert payload["codex_reasoning_effort"] is None
    assert payload["output_schema_path"] == str(
        (pack / "schemas" / "demo_contract_v1.schema.json").resolve()
    )
    assert payload["heads_up_enabled"] is False
    assert payload["heads_up_max_tips"] == 3
    assert payload["incremental"] == {
        "enabled": False,
        "source_run_id": None,
        "reused": 0,
        "queued": 0,
        "fallback_counts": {
            "no_prior_success": 0,
            "hash_changed": 0,
            "source_output_missing": 0,
            "source_output_invalid": 0,
        },
    }

    status_result = runner.invoke(
        app,
        [
            "run",
            "status",
            "--run-id",
            payload["run_id"],
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )
    assert status_result.exit_code == 0, status_result.stderr
    status_payload = json.loads(status_result.stdout)
    assert status_payload["run_id"] == payload["run_id"]
    assert status_payload["pipeline_id"] == pipeline_id
    assert status_payload["counts"]["total"] == 1


def test_run_create_incremental_from_missing_run_is_cli_error(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    data_dir = tmp_path / "var"
    pipeline_id = "demo.contract.v1"

    _write_pipeline_pack(pack, pipeline_id)
    input_dir.mkdir(parents=True)
    (input_dir / "one.json").write_text("{}", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "run",
            "create",
            "--root",
            str(pack),
            "--pipeline",
            pipeline_id,
            "--in",
            str(input_dir),
            "--out",
            str(output_dir),
            "--data-dir",
            str(data_dir),
            "--incremental-from",
            "missing-run-id",
        ],
    )

    assert result.exit_code == 2
    assert "--incremental-from missing-run-id was not found" in result.stderr


def test_run_errors_and_run_tasks_json(tmp_path: Path) -> None:
    data_dir = tmp_path / "var"
    db_path = data_dir / "codex_farm.sqlite3"
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"

    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)

    file_a = input_dir / "a.json"
    file_b = input_dir / "b.json"
    file_a.write_text("{}", encoding="utf-8")
    file_b.write_text("{}", encoding="utf-8")

    conn = open_db(db_path)
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id="demo.contract.v1",
        input_dir=str(input_dir),
        glob="**/*.json",
        output_dir=str(output_dir),
        config={},
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[file_a, file_b],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=".json",
    )

    task_one = lease_one_task(conn, worker_id="w1", lease_seconds=30, run_id=run_id)
    task_two = lease_one_task(conn, worker_id="w2", lease_seconds=30, run_id=run_id)
    assert task_one is not None
    assert task_two is not None

    mark_task_done(conn, task_id=task_one["task_id"], output_path=str(output_dir / task_one["rel_output_path"]))
    mark_task_error(conn, task_id=task_two["task_id"], error="expected failure")

    errors_result = runner.invoke(
        app,
        [
            "run",
            "errors",
            "--run-id",
            run_id,
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )
    assert errors_result.exit_code == 0, errors_result.stderr
    errors_payload = json.loads(errors_result.stdout)
    assert len(errors_payload) == 1
    assert errors_payload[0]["error"] == "expected failure"
    assert errors_payload[0]["input_path"] == str(file_b.resolve())
    assert "task_id" in errors_payload[0]
    assert "updated_at" in errors_payload[0]
    assert errors_payload[0]["attempts"] == 1
    assert errors_payload[0]["lease_claims"] == 1
    assert errors_payload[0]["execution_attempts"] == 0
    assert errors_payload[0]["last_heartbeat_at"] is None

    done_result = runner.invoke(
        app,
        [
            "run",
            "tasks",
            "--run-id",
            run_id,
            "--status",
            "done",
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )
    assert done_result.exit_code == 0, done_result.stderr
    done_payload = json.loads(done_result.stdout)
    assert len(done_payload) == 1
    assert done_payload[0]["status"] == "done"
    assert done_payload[0]["attempts"] == 1
    assert done_payload[0]["lease_claims"] == 1
    assert done_payload[0]["execution_attempts"] == 0
    assert done_payload[0]["last_heartbeat_at"] is None


def test_run_forensics_json_contract(tmp_path: Path) -> None:
    data_dir = tmp_path / "var"
    db_path = data_dir / "codex_farm.sqlite3"
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    sample_input = input_dir / "a.json"
    sample_input.write_text("{}", encoding="utf-8")

    conn = open_db(db_path)
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id="demo.contract.v1",
        input_dir=str(input_dir.resolve()),
        glob="**/*.json",
        output_dir=str(output_dir.resolve()),
        config={},
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[sample_input],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=".json",
    )
    task = lease_one_task(conn, worker_id="w1", lease_seconds=30, run_id=run_id)
    assert task is not None
    output_path = output_dir / task["rel_output_path"]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text('{"ok":"BAD"}', encoding="utf-8")

    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["ok", "required_field"],
                "properties": {
                    "ok": {"type": "string"},
                    "required_field": {"type": "string"},
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    record = capture_failure_forensics(
        conn,
        request=FailureForensicsRequest(
            data_dir=data_dir,
            source="worker",
            run_id=run_id,
            task_id=str(task["task_id"]),
            pipeline_id="demo.contract.v1",
            attempt_index=1,
            terminal=True,
            input_path=sample_input.resolve(),
            input_hash=str(task["input_hash"]),
            rel_output_path=str(task["rel_output_path"]),
            worker_id="w1",
            failure_stage="schema_validation",
            failure_category="schema_validation",
            error_message_full="Schema validation failed at <root>: required_field is required",
            error_message_summary="Schema validation failed at <root>: required_field is required",
            prompt_text="Input file path: /tmp/in/a.json\nReturn JSON only.\n",
            schema_path=schema_path.resolve(),
            output_path=output_path.resolve(),
            stdout_tail="stdout warning",
            stderr_tail="stderr warning",
            runtime_context={"source": "test"},
        ),
    )
    assert record is not None

    result = runner.invoke(
        app,
        [
            "run",
            "forensics",
            "--run-id",
            run_id,
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert len(payload) == 1
    row = payload[0]
    for key in (
        "forensics_id",
        "run_id",
        "task_id",
        "pipeline_id",
        "attempt_index",
        "input_path",
        "failure_stage",
        "failure_category",
        "terminal",
        "bundle_dir",
        "metadata_path",
        "raw_output_path",
        "created_at",
    ):
        assert key in row
    assert row["forensics_id"] == record.forensics_id
    assert row["run_id"] == run_id
    assert row["task_id"] == str(task["task_id"])
    assert row["failure_stage"] == "schema_validation"
    assert Path(row["bundle_dir"]).exists()
    assert Path(row["metadata_path"]).exists()
    assert Path(row["raw_output_path"]).exists()


def test_one_reports_forensics_bundle_on_failure(monkeypatch, tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "out.json"
    pipeline_id = "demo.contract.v1"
    _write_pipeline_pack(pack, pipeline_id)
    input_path.write_text("{}", encoding="utf-8")

    def fake_run_codex_exec(**kwargs):
        return CodexExecResult(
            ok=False,
            exit_code=1,
            stderr_tail="simulated codex failure",
            stdout_tail="stdout context",
        )

    monkeypatch.setattr("codex_farm.cli.run_codex_exec", fake_run_codex_exec)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        [
            "one",
            "--root",
            str(pack),
            "--pipeline",
            pipeline_id,
            "--in",
            str(input_path),
            "--out",
            str(output_path),
        ],
    )

    assert result.exit_code == 1
    assert "codex exec failed (exit=1): simulated codex failure" in result.stdout
    assert "Forensics bundle:" in result.stderr

    bundle_path: Path | None = None
    for line in result.stderr.splitlines():
        if line.startswith("Forensics bundle: "):
            bundle_path = Path(line.split("Forensics bundle: ", 1)[1].strip())
            break
    assert bundle_path is not None
    assert bundle_path.exists()
    assert (bundle_path / "metadata.json").exists()


def test_one_auth_failure_reports_login_guidance_and_forensics_category(
    monkeypatch,
    tmp_path: Path,
) -> None:
    pack = tmp_path / "pack"
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "out.json"
    pipeline_id = "demo.contract.v1"
    _write_pipeline_pack(pack, pipeline_id)
    input_path.write_text("{}", encoding="utf-8")

    def fake_run_codex_exec(**kwargs):
        return CodexExecResult(
            ok=False,
            exit_code=1,
            stderr_tail=(
                "WARNING: no last agent message. "
                "WebSocket error: HTTP 403 Forbidden "
                "wss://chatgpt.com/backend-api/codex/responses"
            ),
            stdout_tail="",
        )

    monkeypatch.setattr("codex_farm.cli.run_codex_exec", fake_run_codex_exec)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        [
            "one",
            "--root",
            str(pack),
            "--pipeline",
            pipeline_id,
            "--in",
            str(input_path),
            "--out",
            str(output_path),
        ],
    )

    assert result.exit_code == 1
    assert "codex auth failed (exit=1):" in result.stdout
    assert "Run `codex` once and sign in with ChatGPT, then retry." in result.stdout
    assert "warning: codex authentication failed; run `codex` once and sign in." in result.stderr
    assert "Forensics bundle:" in result.stderr

    bundle_path: Path | None = None
    for line in result.stderr.splitlines():
        if line.startswith("Forensics bundle: "):
            bundle_path = Path(line.split("Forensics bundle: ", 1)[1].strip())
            break
    assert bundle_path is not None
    metadata = json.loads((bundle_path / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["failure_category"] == "auth_failure"


def test_run_lifecycle_commands_json_contract(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    data_dir = tmp_path / "var"
    pipeline_id = "demo.lifecycle.v1"
    _write_pipeline_pack(pack, pipeline_id)
    input_dir.mkdir(parents=True)
    (input_dir / "one.json").write_text("{}", encoding="utf-8")

    create_result = runner.invoke(
        app,
        [
            "run",
            "create",
            "--root",
            str(pack),
            "--pipeline",
            pipeline_id,
            "--in",
            str(input_dir),
            "--out",
            str(output_dir),
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )
    assert create_result.exit_code == 0, create_result.stderr
    run_id = json.loads(create_result.stdout)["run_id"]

    pause_result = runner.invoke(
        app,
        ["run", "pause", "--run-id", run_id, "--data-dir", str(data_dir), "--json"],
    )
    assert pause_result.exit_code == 0, pause_result.stderr
    pause_payload = json.loads(pause_result.stdout)
    assert pause_payload["action"] == "pause"
    assert pause_payload["status"] == "paused"
    assert pause_payload["control_state"] == "paused"
    assert pause_payload["counts"]["canceled"] == 0

    resume_result = runner.invoke(
        app,
        ["run", "resume", "--run-id", run_id, "--data-dir", str(data_dir), "--json"],
    )
    assert resume_result.exit_code == 0, resume_result.stderr
    resume_payload = json.loads(resume_result.stdout)
    assert resume_payload["action"] == "resume"
    assert resume_payload["status"] == "queued"
    assert resume_payload["control_state"] == "active"

    cancel_result = runner.invoke(
        app,
        [
            "run",
            "cancel",
            "--run-id",
            run_id,
            "--data-dir",
            str(data_dir),
            "--yes",
            "--json",
        ],
    )
    assert cancel_result.exit_code == 0, cancel_result.stderr
    cancel_payload = json.loads(cancel_result.stdout)
    assert cancel_payload["action"] == "cancel"
    assert cancel_payload["control_state"] == "canceled"
    assert cancel_payload["status"] == "canceled"
    assert cancel_payload["changed_task_count"] == 1
    assert cancel_payload["counts"]["canceled"] == 1

    canceled_tasks_result = runner.invoke(
        app,
        [
            "run",
            "tasks",
            "--run-id",
            run_id,
            "--status",
            "canceled",
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )
    assert canceled_tasks_result.exit_code == 0, canceled_tasks_result.stderr
    canceled_payload = json.loads(canceled_tasks_result.stdout)
    assert len(canceled_payload) == 1
    assert canceled_payload[0]["status"] == "canceled"


def test_run_cancel_json_requires_yes(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    data_dir = tmp_path / "var"
    pipeline_id = "demo.lifecycle.v1"
    _write_pipeline_pack(pack, pipeline_id)
    input_dir.mkdir(parents=True)
    (input_dir / "one.json").write_text("{}", encoding="utf-8")

    create_result = runner.invoke(
        app,
        [
            "run",
            "create",
            "--root",
            str(pack),
            "--pipeline",
            pipeline_id,
            "--in",
            str(input_dir),
            "--out",
            str(output_dir),
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )
    assert create_result.exit_code == 0, create_result.stderr
    run_id = json.loads(create_result.stdout)["run_id"]

    cancel_result = runner.invoke(
        app,
        [
            "run",
            "cancel",
            "--run-id",
            run_id,
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )
    assert cancel_result.exit_code == 2
    assert "--yes is required with --json" in cancel_result.stderr


def test_run_retry_errors_json_contract(tmp_path: Path) -> None:
    data_dir = tmp_path / "var"
    db_path = data_dir / "codex_farm.sqlite3"
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    (input_dir / "one.json").write_text("{}", encoding="utf-8")

    conn = open_db(db_path)
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id="demo.lifecycle.v1",
        input_dir=str(input_dir),
        glob="**/*.json",
        output_dir=str(output_dir),
        config={},
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[input_dir / "one.json"],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=".json",
    )
    task = lease_one_task(conn, worker_id="w1", lease_seconds=30, run_id=run_id)
    assert task is not None
    mark_task_error(conn, task_id=task["task_id"], error="boom")

    retry_result = runner.invoke(
        app,
        ["run", "retry-errors", "--run-id", run_id, "--data-dir", str(data_dir), "--json"],
    )
    assert retry_result.exit_code == 0, retry_result.stderr
    payload = json.loads(retry_result.stdout)
    assert payload["action"] == "retry-errors"
    assert payload["changed_task_count"] == 1
    assert payload["status"] == "queued"
    assert payload["control_state"] == "active"

    tasks_result = runner.invoke(
        app,
        ["run", "tasks", "--run-id", run_id, "--data-dir", str(data_dir), "--json"],
    )
    assert tasks_result.exit_code == 0, tasks_result.stderr
    task_rows = json.loads(tasks_result.stdout)
    assert task_rows[0]["status"] == "queued"
    assert task_rows[0]["attempts"] == 0
    assert task_rows[0]["error"] == "boom"


def test_run_status_json_includes_control_state_and_canceled_count(tmp_path: Path) -> None:
    data_dir = tmp_path / "var"
    db_path = data_dir / "codex_farm.sqlite3"
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    (input_dir / "one.json").write_text("{}", encoding="utf-8")

    conn = open_db(db_path)
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id="demo.lifecycle.v1",
        input_dir=str(input_dir),
        glob="**/*.json",
        output_dir=str(output_dir),
        config={},
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[input_dir / "one.json"],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=".json",
    )
    set_run_control_state(conn, run_id=run_id, control_state="paused")

    status_result = runner.invoke(
        app,
        ["run", "status", "--run-id", run_id, "--data-dir", str(data_dir), "--json"],
    )
    assert status_result.exit_code == 0, status_result.stderr
    payload = json.loads(status_result.stdout)
    assert payload["control_state"] == "paused"
    assert payload["counts"]["canceled"] == 0


def test_heads_up_list_and_clear_json_contract(tmp_path: Path) -> None:
    data_dir = tmp_path / "var"
    db_path = data_dir / "codex_farm.sqlite3"
    conn = open_db(db_path)
    init_db(conn)
    inserted = upsert_heads_up_tips(
        conn,
        pipeline_id="demo.contract.v1",
        source_run_id="run-1",
        tips=[
            {
                "input_signature": "json_obj_keys:a,b",
                "tip_text": "Keep output as raw JSON.",
            }
        ],
    )
    assert inserted == 1

    list_result = runner.invoke(
        app,
        [
            "heads-up",
            "list",
            "--pipeline",
            "demo.contract.v1",
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )
    assert list_result.exit_code == 0, list_result.stderr
    list_payload = json.loads(list_result.stdout)
    assert len(list_payload) == 1
    assert list_payload[0]["pipeline_id"] == "demo.contract.v1"
    assert list_payload[0]["input_signature"] == "json_obj_keys:a,b"
    assert list_payload[0]["tip_text"] == "Keep output as raw JSON."

    clear_result = runner.invoke(
        app,
        [
            "heads-up",
            "clear",
            "--pipeline",
            "demo.contract.v1",
            "--data-dir",
            str(data_dir),
            "--yes",
            "--json",
        ],
    )
    assert clear_result.exit_code == 0, clear_result.stderr
    clear_payload = json.loads(clear_result.stdout)
    assert clear_payload["pipeline_id"] == "demo.contract.v1"
    assert clear_payload["deleted"] == 1


def test_heads_up_learn_json_contract(monkeypatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "var"

    def fake_learn_heads_up_from_run(*args, **kwargs):
        return {"tips_added": 2, "warning": "distiller warning"}

    monkeypatch.setattr("codex_farm.cli.learn_heads_up_from_run", fake_learn_heads_up_from_run)

    result = runner.invoke(
        app,
        [
            "heads-up",
            "learn",
            "--run-id",
            "run-abc",
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["run_id"] == "run-abc"
    assert payload["tips_added"] == 2
    assert payload["warning"] == "distiller warning"


def test_heads_up_learn_requires_terminal_run_status(tmp_path: Path) -> None:
    data_dir = tmp_path / "var"
    db_path = data_dir / "codex_farm.sqlite3"
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    sample = input_dir / "one.json"
    sample.write_text("{}", encoding="utf-8")

    conn = open_db(db_path)
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id="demo.contract.v1",
        input_dir=str(input_dir.resolve()),
        glob="**/*.json",
        output_dir=str(output_dir.resolve()),
        config={},
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[sample],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=".json",
    )

    result = runner.invoke(
        app,
        [
            "heads-up",
            "learn",
            "--run-id",
            run_id,
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["run_id"] == run_id
    assert payload["tips_added"] == 0
    assert "terminal run status" in str(payload["warning"])


def test_process_heads_up_learning_exception_is_warning_only(
    monkeypatch,
    tmp_path: Path,
) -> None:
    pack = tmp_path / "pack"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    data_dir = tmp_path / "var"
    pipeline_id = "demo.contract.v1"
    _write_pipeline_pack(pack, pipeline_id)
    input_dir.mkdir(parents=True)
    (input_dir / "one.json").write_text("{}", encoding="utf-8")

    def fake_run_codex_exec(**kwargs):
        output_path = kwargs["output_path"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_line = kwargs["prompt"].strip().splitlines()[-1]
        source_path = prompt_line.replace("Input file path: ", "")
        output_path.write_text(
            json.dumps({"ok": "OK", "source_path": source_path}),
            encoding="utf-8",
        )
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    def explode_heads_up_learning(*args, **kwargs):
        raise RuntimeError("distiller boom")

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)
    monkeypatch.setattr("codex_farm.cli.learn_heads_up_from_run", explode_heads_up_learning)

    result = runner.invoke(
        app,
        [
            "process",
            "--root",
            str(pack),
            "--pipeline",
            pipeline_id,
            "--in",
            str(input_dir),
            "--out",
            str(output_dir),
            "--workers",
            "1",
            "--heads-up",
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "done"
    assert payload["heads_up_tips_added"] == 0
    assert payload["heads_up_tips_applied"] == 0
    assert "distiller boom" in str(payload["heads_up_warning"])


def test_go_heads_up_runs_post_run_learning(monkeypatch, tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    data_dir = tmp_path / "var"
    pipeline_id = "demo.contract.v1"
    _write_pipeline_pack(pack, pipeline_id)

    inbox = data_dir / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "one.json").write_text("{}", encoding="utf-8")

    learned_run_ids: list[str] = []

    def fake_run_codex_exec(**kwargs):
        output_path = kwargs["output_path"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_line = kwargs["prompt"].strip().splitlines()[-1]
        source_path = prompt_line.replace("Input file path: ", "")
        output_path.write_text(
            json.dumps({"ok": "OK", "source_path": source_path}),
            encoding="utf-8",
        )
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    def fake_learn_heads_up_from_run(*args, **kwargs):
        learned_run_ids.append(str(kwargs["run_id"]))
        return {"tips_added": 3, "warning": None}

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)
    monkeypatch.setattr("codex_farm.cli.learn_heads_up_from_run", fake_learn_heads_up_from_run)

    result = runner.invoke(
        app,
        [
            "go",
            "--root",
            str(pack),
            "--data-dir",
            str(data_dir),
            "--heads-up",
        ],
        input="1\n1\n",
    )

    assert result.exit_code == 0, result.stderr
    assert len(learned_run_ids) == 1
    assert learned_run_ids[0]
    assert "Heads Up tips added: 3" in result.stdout
    conn = open_db(data_dir / "codex_farm.sqlite3")
    run = get_run(conn, learned_run_ids[0])
    run_config = json.loads(run["config_json"])
    assert isinstance(run_config.get("frozen_assets"), dict)
    go_manifest_relpath = str(run_config["frozen_assets"]["manifest_relpath"])
    assert (data_dir / go_manifest_relpath).exists()
