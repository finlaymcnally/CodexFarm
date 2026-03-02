import json
import os
from pathlib import Path
import stat

from typer.testing import CliRunner

from codex_farm.cli import app
from codex_farm.paths import find_repo_root


runner = CliRunner()


def _write_fake_codex(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script_path = bin_dir / "codex"
    script_path.write_text(
        """#!/usr/bin/env python3
import json
import sys


def _value(args, flag):
    for idx, arg in enumerate(args):
        if arg == flag and idx + 1 < len(args):
            return args[idx + 1]
    return ""


args = sys.argv[1:]
output_path = _value(args, "--output-last-message")
cd_dir = _value(args, "--cd")
prompt = args[-1] if args else ""
input_path = ""

for line in prompt.splitlines():
    if line.startswith("INPUT="):
        input_path = line.split("=", 1)[1].strip()
        break

with open(output_path, "w", encoding="utf-8") as handle:
    json.dump({"ok": "OK", "cd": cd_dir, "input_path": input_path}, handle)
""",
        encoding="utf-8",
    )
    script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR)


def _env_with_fake_codex(bin_dir: Path) -> dict[str, str]:
    return {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "CODEX_FARM_SKIP_LOGIN_PRECHECK": "1",
    }


def _write_schema_failure_pack(pack_root: Path, pipeline_id: str) -> None:
    for folder in ("pipelines", "prompts", "schemas"):
        (pack_root / folder).mkdir(parents=True, exist_ok=True)

    slug = pipeline_id.replace(".", "_")
    prompt_rel = Path("prompts") / f"{slug}.txt"
    schema_rel = Path("schemas") / f"{slug}.schema.json"
    pipeline_payload = {
        "pipeline_id": pipeline_id,
        "description": "Deliberate schema-failure pack",
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
    (pack_root / "pipelines" / f"{pipeline_id}.json").write_text(
        json.dumps(pipeline_payload, indent=2) + "\n",
        encoding="utf-8",
    )
    (pack_root / prompt_rel).write_text("INPUT={{INPUT_PATH}}\n", encoding="utf-8")
    (pack_root / schema_rel).write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["ok", "cd", "input_path", "must_be_present"],
                "properties": {
                    "ok": {"type": "string"},
                    "cd": {"type": "string"},
                    "input_path": {"type": "string"},
                    "must_be_present": {"type": "string"},
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_override_schema(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["ok", "cd", "input_path", "must_be_present"],
                "properties": {
                    "ok": {"type": "string"},
                    "cd": {"type": "string"},
                    "input_path": {"type": "string"},
                    "must_be_present": {"type": "string"},
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_drift_pack(pack_root: Path, pipeline_id: str) -> None:
    for folder in ("pipelines", "prompts", "schemas"):
        (pack_root / folder).mkdir(parents=True, exist_ok=True)

    slug = pipeline_id.replace(".", "_")
    prompt_rel = Path("prompts") / f"{slug}.txt"
    schema_rel = Path("schemas") / f"{slug}.schema.json"
    pipeline_payload = {
        "pipeline_id": pipeline_id,
        "description": "Prompt drift test pack",
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
    (pack_root / "pipelines" / f"{pipeline_id}.json").write_text(
        json.dumps(pipeline_payload, indent=2) + "\n",
        encoding="utf-8",
    )
    (pack_root / prompt_rel).write_text("INPUT={{INPUT_PATH}}\n", encoding="utf-8")
    (pack_root / schema_rel).write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["ok", "cd", "input_path"],
                "properties": {
                    "ok": {"type": "string"},
                    "cd": {"type": "string"},
                    "input_path": {"type": "string"},
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def test_one_with_root_and_cd_mode(tmp_path: Path) -> None:
    repo_root = find_repo_root()
    pack_root = repo_root / "examples" / "pipeline_pack_demo"
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"
    input_path.write_text("{}", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    _write_fake_codex(fake_bin)

    result = runner.invoke(
        app,
        [
            "one",
            "--root",
            str(pack_root),
            "--pipeline",
            "demo.echo.v1",
            "--in",
            str(input_path),
            "--out",
            str(output_path),
        ],
        env=_env_with_fake_codex(fake_bin),
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["ok"] == "OK"
    assert payload["cd"] == str(input_path.resolve().parent)
    assert payload["input_path"] == str(input_path.resolve())


def test_process_with_root_and_cd_mode(tmp_path: Path) -> None:
    repo_root = find_repo_root()
    pack_root = repo_root / "examples" / "pipeline_pack_demo"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    data_dir = tmp_path / "var"
    (input_dir / "nested").mkdir(parents=True, exist_ok=True)
    (input_dir / "a.json").write_text("{}", encoding="utf-8")
    (input_dir / "nested" / "b.json").write_text("{}", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    _write_fake_codex(fake_bin)

    result = runner.invoke(
        app,
        [
            "process",
            "--root",
            str(pack_root),
            "--pipeline",
            "demo.echo.v1",
            "--in",
            str(input_dir),
            "--out",
            str(output_dir),
            "--workers",
            "2",
            "--data-dir",
            str(data_dir),
            "--json",
        ],
        env=_env_with_fake_codex(fake_bin),
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["pipeline_id"] == "demo.echo.v1"
    assert payload["counts"]["done"] == 2
    assert payload["counts"]["error"] == 0
    assert payload["counts"]["total"] == 2
    assert payload["exit_code"] == 0
    assert payload["workspace_root"] is None
    assert payload["output_schema_path"] == str(
        (pack_root / "schemas" / "demo_echo_v1.schema.json").resolve()
    )

    for rel_path in ("a.json", "nested/b.json"):
        output_path = output_dir / rel_path
        source_path = input_dir / rel_path
        assert output_path.exists()
        row = json.loads(output_path.read_text(encoding="utf-8"))
        assert row["ok"] == "OK"
        assert row["cd"] == str(input_dir.resolve())
        assert row["input_path"] == str(source_path.resolve())


def test_run_errors_json_on_schema_failure(tmp_path: Path) -> None:
    pipeline_id = "demo.schema.fail.v1"
    pack_root = tmp_path / "pack"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    data_dir = tmp_path / "var"
    input_dir.mkdir(parents=True, exist_ok=True)
    (input_dir / "bad.json").write_text("{}", encoding="utf-8")
    _write_schema_failure_pack(pack_root, pipeline_id)

    fake_bin = tmp_path / "bin"
    _write_fake_codex(fake_bin)

    process_result = runner.invoke(
        app,
        [
            "process",
            "--root",
            str(pack_root),
            "--pipeline",
            pipeline_id,
            "--in",
            str(input_dir),
            "--out",
            str(output_dir),
            "--workers",
            "1",
            "--max-attempts",
            "1",
            "--data-dir",
            str(data_dir),
            "--json",
        ],
        env=_env_with_fake_codex(fake_bin),
    )

    assert process_result.exit_code != 0
    process_payload = json.loads(process_result.stdout)
    assert process_payload["counts"]["error"] == 1
    assert process_payload["exit_code"] == 1

    errors_result = runner.invoke(
        app,
        [
            "run",
            "errors",
            "--run-id",
            process_payload["run_id"],
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )
    assert errors_result.exit_code == 0, errors_result.stderr
    errors_payload = json.loads(errors_result.stdout)
    assert len(errors_payload) == 1
    assert errors_payload[0]["input_path"] == str((input_dir / "bad.json").resolve())
    assert "Schema validation failed" in errors_payload[0]["error"]
    for key in (
        "task_id",
        "rel_output_path",
        "attempts",
        "leased_by",
        "lease_until",
        "updated_at",
    ):
        assert key in errors_payload[0]


def test_process_schema_failure_preserves_forensics_bundle(tmp_path: Path) -> None:
    pipeline_id = "demo.schema.fail.v1"
    pack_root = tmp_path / "pack"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    data_dir = tmp_path / "var"
    input_dir.mkdir(parents=True, exist_ok=True)
    (input_dir / "bad.json").write_text("{}", encoding="utf-8")
    _write_schema_failure_pack(pack_root, pipeline_id)

    fake_bin = tmp_path / "bin"
    _write_fake_codex(fake_bin)

    process_result = runner.invoke(
        app,
        [
            "process",
            "--root",
            str(pack_root),
            "--pipeline",
            pipeline_id,
            "--in",
            str(input_dir),
            "--out",
            str(output_dir),
            "--workers",
            "1",
            "--max-attempts",
            "1",
            "--data-dir",
            str(data_dir),
            "--json",
        ],
        env=_env_with_fake_codex(fake_bin),
    )

    assert process_result.exit_code != 0
    process_payload = json.loads(process_result.stdout)
    run_id = str(process_payload["run_id"])
    assert process_payload["counts"]["error"] == 1
    assert process_payload["exit_code"] == 1

    # Normal output tree remains clean after schema rejection.
    assert not (output_dir / "bad.json").exists()

    forensics_result = runner.invoke(
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
    assert forensics_result.exit_code == 0, forensics_result.stderr
    forensics_payload = json.loads(forensics_result.stdout)
    assert len(forensics_payload) == 1
    row = forensics_payload[0]
    assert row["failure_stage"] == "schema_validation"
    assert row["failure_category"] == "schema_validation"
    assert row["terminal"] is True

    bundle_dir = Path(row["bundle_dir"])
    metadata_path = Path(row["metadata_path"])
    raw_output_path = Path(row["raw_output_path"])
    assert bundle_dir.exists()
    assert metadata_path.exists()
    assert raw_output_path.exists()

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["schema_version"] == 1
    assert metadata["artifacts"]["raw_output"]["path"] == "output.raw.json"


def test_process_uses_output_schema_override_for_validation(tmp_path: Path) -> None:
    repo_root = find_repo_root()
    pack_root = repo_root / "examples" / "pipeline_pack_demo"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    data_dir = tmp_path / "var"
    override_schema = tmp_path / "caller.schema.json"
    input_dir.mkdir(parents=True, exist_ok=True)
    (input_dir / "bad.json").write_text("{}", encoding="utf-8")
    _write_override_schema(override_schema)

    fake_bin = tmp_path / "bin"
    _write_fake_codex(fake_bin)

    process_result = runner.invoke(
        app,
        [
            "process",
            "--root",
            str(pack_root),
            "--pipeline",
            "demo.echo.v1",
            "--in",
            str(input_dir),
            "--out",
            str(output_dir),
            "--workers",
            "1",
            "--max-attempts",
            "1",
            "--output-schema",
            str(override_schema),
            "--data-dir",
            str(data_dir),
            "--json",
        ],
        env=_env_with_fake_codex(fake_bin),
    )

    assert process_result.exit_code != 0
    process_payload = json.loads(process_result.stdout)
    assert process_payload["counts"]["error"] == 1
    assert process_payload["output_schema_path"] == str(override_schema.resolve())
    assert process_payload["exit_code"] == 1

    errors_result = runner.invoke(
        app,
        [
            "run",
            "errors",
            "--run-id",
            process_payload["run_id"],
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )
    assert errors_result.exit_code == 0, errors_result.stderr
    errors_payload = json.loads(errors_result.stdout)
    assert len(errors_payload) == 1
    assert "Schema validation failed" in errors_payload[0]["error"]


def test_run_create_freezes_prompt_before_worker_execution(tmp_path: Path) -> None:
    pipeline_id = "demo.prompt.freeze.v1"
    pack_root = tmp_path / "pack"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    data_dir = tmp_path / "var"
    _write_drift_pack(pack_root, pipeline_id)
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    input_path = input_dir / "one.json"
    input_path.write_text("{}", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    _write_fake_codex(fake_bin)

    create_result = runner.invoke(
        app,
        [
            "run",
            "create",
            "--root",
            str(pack_root),
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
    run_payload = json.loads(create_result.stdout)

    prompt_path = pack_root / "prompts" / "demo_prompt_freeze_v1.txt"
    prompt_path.write_text("BROKEN={{INPUT_PATH}}\n", encoding="utf-8")

    worker_result = runner.invoke(
        app,
        [
            "worker",
            "--run-id",
            run_payload["run_id"],
            "--data-dir",
            str(data_dir),
            "--once",
        ],
        env=_env_with_fake_codex(fake_bin),
    )
    assert worker_result.exit_code == 0, worker_result.stderr

    frozen_output = json.loads((output_dir / "one.json").read_text(encoding="utf-8"))
    assert frozen_output["input_path"] == str(input_path.resolve())

    second_output_dir = tmp_path / "output-second"
    second_output_dir.mkdir(parents=True, exist_ok=True)
    second_create = runner.invoke(
        app,
        [
            "run",
            "create",
            "--root",
            str(pack_root),
            "--pipeline",
            pipeline_id,
            "--in",
            str(input_dir),
            "--out",
            str(second_output_dir),
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )
    assert second_create.exit_code == 0, second_create.stderr
    second_run_payload = json.loads(second_create.stdout)
    second_worker = runner.invoke(
        app,
        [
            "worker",
            "--run-id",
            second_run_payload["run_id"],
            "--data-dir",
            str(data_dir),
            "--once",
        ],
        env=_env_with_fake_codex(fake_bin),
    )
    assert second_worker.exit_code == 0, second_worker.stderr

    live_output = json.loads((second_output_dir / "one.json").read_text(encoding="utf-8"))
    assert live_output["input_path"] == ""
