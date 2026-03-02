import json
from pathlib import Path

from codex_farm.recipeimport_benchmark_eval import (
    prepare_line_label_benchmark_artifacts,
    write_line_label_benchmark_artifacts,
)


def _write_input(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "canonical_lines": [
                    {"line_index": 0, "text": "Simple Soup", "expected_label": "title"},
                    {"line_index": 1, "text": "1 cup water", "expected_label": "ingredient"},
                    {"line_index": 2, "text": "Boil for 5 minutes", "expected_label": "instruction"},
                ]
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def test_prepare_line_label_benchmark_artifacts_applies_deterministic_calibration(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"
    _write_input(input_path)
    output_path.write_text(
        json.dumps(
            {
                "line_predictions": [
                    {
                        "line_index": 0,
                        "label": " Title ",
                        "confidence": 1.5,
                        "evidence_line_indices": [0, 99],
                        "reasoning_tags": ["Header Line"],
                    },
                    {
                        "line_index": 1,
                        "label": "ingredient",
                        "confidence": 0.7,
                        "evidence_line_indices": [1],
                        "reasoning_tags": ["quantity_found"],
                    },
                    {
                        "line_index": 1,
                        "label": "misc",
                        "confidence": 0.1,
                        "evidence_line_indices": [1],
                        "reasoning_tags": ["low_signal"],
                    },
                    {
                        "line_index": 9,
                        "label": "junk",
                        "confidence": 0.9,
                        "evidence_line_indices": [9],
                        "reasoning_tags": ["out_of_range"],
                    },
                ]
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    prepared = prepare_line_label_benchmark_artifacts(
        input_path=input_path,
        output_path=output_path,
    )

    assert len(prepared.canonical_lines) == 3
    assert len(prepared.calibrated_predictions) == 3
    assert [row.line_index for row in prepared.calibrated_predictions] == [0, 1, 2]
    assert prepared.calibrated_predictions[0].label == "title"
    assert prepared.calibrated_predictions[0].confidence == 1.0
    assert prepared.calibrated_predictions[0].evidence_line_indices == (0,)
    assert prepared.calibrated_predictions[2].label == "unlabeled"

    action_names = {row.action for row in [*prepared.alignment_actions, *prepared.calibration_actions]}
    assert "dedupe_line_index" in action_names
    assert "drop_unknown_line_index" in action_names
    assert "fill_missing_prediction" in action_names
    assert "clamp_confidence" in action_names

    stage_names = {row["stage"] for row in prepared.pass_metrics["pass_stage_scores"]}
    assert "alignment_coverage" in stage_names
    assert "calibration_stability" in stage_names
    assert "gold_label_accuracy" in stage_names
    alignment_stage = next(
        row for row in prepared.pass_metrics["pass_stage_scores"] if row["stage"] == "alignment_coverage"
    )
    assert alignment_stage["numerator"] == 2
    assert alignment_stage["denominator"] == 3
    assert alignment_stage["score"] == 0.666667


def test_write_line_label_benchmark_artifacts_writes_manifest_and_debug_files(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "out.json"
    schema_path = tmp_path / "schema.json"
    output_dir = tmp_path / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_input(input_path)
    output_path.write_text(
        json.dumps(
            {
                "line_predictions": [
                    {
                        "line_index": 0,
                        "label": "title",
                        "confidence": 0.99,
                        "evidence_line_indices": [0],
                        "reasoning_tags": ["header_line"],
                    },
                    {
                        "line_index": 1,
                        "label": "ingredient",
                        "confidence": 0.96,
                        "evidence_line_indices": [1],
                        "reasoning_tags": ["quantity_found"],
                    },
                    {
                        "line_index": 2,
                        "label": "instruction",
                        "confidence": 0.95,
                        "evidence_line_indices": [2],
                        "reasoning_tags": ["imperative_verb"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    schema_path.write_text("{}", encoding="utf-8")

    prepared = prepare_line_label_benchmark_artifacts(
        input_path=input_path,
        output_path=output_path,
    )
    manifest_path = write_line_label_benchmark_artifacts(
        run_output_dir=output_dir,
        run_id="run-1",
        task_id="task-1",
        pipeline_id="recipeimport.benchmark.line_label.v1",
        input_path=input_path,
        output_path=output_path,
        output_schema_path=schema_path,
        output_schema_logical_path=schema_path,
        selected_model="gpt-test",
        prompt_text="Prompt body",
        prepared=prepared,
        debug_enabled=True,
        raw_model_output_text='{"line_predictions":[{"line_index":0}]}',
        stdout_tail="stdout tail",
        stderr_tail="stderr tail",
    )

    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["run_id"] == "run-1"
    assert manifest["task_id"] == "task-1"
    assert manifest["model"] == "gpt-test"
    assert manifest["debug_enabled"] is True
    assert manifest["prompt_sha256"]
    assert manifest["output_schema_sha256"]
    files = manifest["files"]
    assert "predictions.calibrated.json" in files
    assert "calibration.actions.json" in files
    assert "pass.metrics.json" in files
    assert "aligned.line_mappings.json" in files
    assert "debug.request.prompt.txt" in files
    assert "debug.response.output.raw.json" in files
    assert (
        (manifest_path.parent / "debug.response.output.raw.json").read_text(encoding="utf-8")
        == '{"line_predictions":[{"line_index":0}]}'
    )
