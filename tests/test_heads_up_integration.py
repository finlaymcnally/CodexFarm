import json
from pathlib import Path

from typer.testing import CliRunner

from codex_farm.cli import app
from codex_farm.codex_exec import CodexExecResult


runner = CliRunner()


def _write_pack(root: Path, pipeline_id: str) -> None:
    for folder in ("pipelines", "prompts", "schemas"):
        (root / folder).mkdir(parents=True, exist_ok=True)

    slug = pipeline_id.replace(".", "_")
    prompt_rel = Path("prompts") / f"{slug}.txt"
    schema_rel = Path("schemas") / f"{slug}.schema.json"

    pipeline_payload = {
        "pipeline_id": pipeline_id,
        "description": "Heads up integration pipeline",
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

    (root / "prompts" / "heads_up_distiller_v1.txt").write_text(
        "Pipeline: {{PIPELINE_ID}}\nPrompt:\n{{PIPELINE_PROMPT}}\nObservations:\n{{OBSERVATIONS_JSON}}\n",
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
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["input_signature", "tip_text"],
                            "properties": {
                                "input_signature": {"type": "string", "minLength": 1},
                                "tip_text": {"type": "string", "minLength": 1},
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


def test_heads_up_learning_from_run_a_applies_to_run_b(monkeypatch, tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    pipeline_id = "demo.headsup.integration.v1"
    data_dir = tmp_path / "var"
    run_a_input_dir = tmp_path / "run_a_in"
    run_a_output_dir = tmp_path / "run_a_out"
    run_b_input_dir = tmp_path / "run_b_in"
    run_b_output_dir = tmp_path / "run_b_out"

    _write_pack(pack, pipeline_id)
    run_a_input_dir.mkdir(parents=True)
    run_b_input_dir.mkdir(parents=True)

    (run_a_input_dir / "a.json").write_text(
        json.dumps({"name": "A", "recipeInstructions": ["one"]}),
        encoding="utf-8",
    )
    (run_b_input_dir / "b.json").write_text(
        json.dumps({"name": "B", "recipeInstructions": ["two"]}),
        encoding="utf-8",
    )

    worker_prompts: list[str] = []
    distiller_prompts: list[str] = []

    def fake_worker_codex_exec(**kwargs):
        worker_prompts.append(str(kwargs["prompt"]))
        output_path = kwargs["output_path"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        source_path = ""
        for line in str(kwargs["prompt"]).splitlines():
            if line.startswith("Input file path: "):
                source_path = line.replace("Input file path: ", "", 1).strip()
                break
        output_path.write_text(
            json.dumps({"ok": "OK", "source_path": source_path}),
            encoding="utf-8",
        )
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    def fake_distiller_codex_exec(**kwargs):
        distiller_prompts.append(str(kwargs["prompt"]))
        output_path = kwargs["output_path"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(
                {
                    "tips": [
                        {
                            "input_signature": "json_obj_keys:name,recipeInstructions",
                            "tip_text": "Keep recipeInstructions normalized and return JSON only.",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_worker_codex_exec)
    monkeypatch.setattr("codex_farm.heads_up.run_codex_exec", fake_distiller_codex_exec)

    run_a_result = runner.invoke(
        app,
        [
            "process",
            "--root",
            str(pack),
            "--pipeline",
            pipeline_id,
            "--in",
            str(run_a_input_dir),
            "--out",
            str(run_a_output_dir),
            "--workers",
            "1",
            "--heads-up",
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )
    assert run_a_result.exit_code == 0, run_a_result.stderr
    run_a_payload = json.loads(run_a_result.stdout)
    assert run_a_payload["counts"]["done"] == 1
    assert run_a_payload["heads_up_tips_applied"] == 0
    assert run_a_payload["heads_up_tips_added"] == 1

    list_result = runner.invoke(
        app,
        [
            "heads-up",
            "list",
            "--pipeline",
            pipeline_id,
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )
    assert list_result.exit_code == 0, list_result.stderr
    rows = json.loads(list_result.stdout)
    assert len(rows) == 1

    run_b_result = runner.invoke(
        app,
        [
            "process",
            "--root",
            str(pack),
            "--pipeline",
            pipeline_id,
            "--in",
            str(run_b_input_dir),
            "--out",
            str(run_b_output_dir),
            "--workers",
            "1",
            "--heads-up",
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )
    assert run_b_result.exit_code == 0, run_b_result.stderr
    run_b_payload = json.loads(run_b_result.stdout)
    assert run_b_payload["counts"]["done"] == 1
    assert run_b_payload["heads_up_tips_applied"] == 1

    assert len(worker_prompts) == 2
    assert "Heads up for this task:" not in worker_prompts[0]
    assert "Heads up for this task:" in worker_prompts[1]
    assert "Keep recipeInstructions normalized and return JSON only." in worker_prompts[1]
    assert distiller_prompts
