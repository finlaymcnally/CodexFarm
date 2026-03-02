"""Evaluation and artifact writing for recipeimport benchmark mode."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil

from .recipeimport_benchmark_calibration import calibrate_line_label_predictions
from .recipeimport_benchmark_line_label import (
    align_predictions_to_canonical_lines,
    load_canonical_lines,
    load_line_predictions,
)
from .recipeimport_benchmark_types import (
    CalibrationAction,
    CanonicalLine,
    LineLabelPrediction,
    calibration_action_to_dict,
    canonical_line_to_dict,
    line_prediction_to_dict,
)


RECIPEIMPORT_BENCHMARK_MODE = "line_label_v1"
RECIPEIMPORT_BENCHMARK_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PreparedLineLabelBenchmarkArtifacts:
    canonical_lines: list[CanonicalLine]
    raw_predictions: list[LineLabelPrediction]
    aligned_predictions: list[LineLabelPrediction]
    alignment_actions: list[CalibrationAction]
    calibrated_predictions: list[LineLabelPrediction]
    calibration_actions: list[CalibrationAction]
    pass_metrics: dict[str, object]


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_expected_label(label: str | None) -> str | None:
    if label is None:
        return None
    cleaned = label.strip().lower().replace(" ", "_").replace("-", "_")
    return cleaned or None


def _float_summary(values: list[float]) -> dict[str, float]:
    if not values:
        return {"min": 0.0, "max": 0.0, "avg": 0.0}
    return {
        "min": min(values),
        "max": max(values),
        "avg": round(sum(values) / len(values), 6),
    }


def _pass_stage_scores(
    *,
    total_lines: int,
    observed_lines: int,
    calibration_action_count: int,
    gold_total: int,
    gold_matches: int,
) -> list[dict[str, object]]:
    denominator = max(total_lines, 1)
    alignment_score = round(observed_lines / denominator, 6)
    calibration_score = round(max(0.0, 1.0 - (calibration_action_count / denominator)), 6)
    stages = [
        {
            "stage": "alignment_coverage",
            "score": alignment_score,
            "numerator": observed_lines,
            "denominator": total_lines,
        },
        {
            "stage": "calibration_stability",
            "score": calibration_score,
            "numerator": max(0, total_lines - calibration_action_count),
            "denominator": total_lines,
        },
    ]
    if gold_total > 0:
        stages.append(
            {
                "stage": "gold_label_accuracy",
                "score": round(gold_matches / gold_total, 6),
                "numerator": gold_matches,
                "denominator": gold_total,
            }
        )
    return stages


def build_line_label_pass_metrics(
    *,
    canonical_lines: list[CanonicalLine],
    raw_predictions: list[LineLabelPrediction],
    calibrated_predictions: list[LineLabelPrediction],
    all_actions: list[CalibrationAction],
) -> dict[str, object]:
    label_counts: dict[str, int] = {}
    confidences: list[float] = []
    prediction_by_index = {row.line_index: row for row in calibrated_predictions}

    for prediction in calibrated_predictions:
        label_counts[prediction.label] = label_counts.get(prediction.label, 0) + 1
        confidences.append(prediction.confidence)

    gold_total = 0
    gold_matches = 0
    for line in canonical_lines:
        expected = _normalize_expected_label(line.expected_label)
        if expected is None:
            continue
        gold_total += 1
        predicted = prediction_by_index.get(line.line_index)
        if predicted is not None and predicted.label == expected:
            gold_matches += 1

    action_counts: dict[str, int] = {}
    for action in all_actions:
        action_counts[action.action] = action_counts.get(action.action, 0) + 1

    total_lines = len(canonical_lines)
    expected_indices = {line.line_index for line in canonical_lines}
    observed_lines = len(
        {
            row.line_index
            for row in raw_predictions
            if row.line_index in expected_indices
        }
    )
    aligned_lines = len(calibrated_predictions)
    return {
        "schema_version": RECIPEIMPORT_BENCHMARK_SCHEMA_VERSION,
        "mode": RECIPEIMPORT_BENCHMARK_MODE,
        "total_lines": total_lines,
        "observed_lines": observed_lines,
        "aligned_lines": aligned_lines,
        "label_distribution": dict(sorted(label_counts.items())),
        "confidence_summary": _float_summary(confidences),
        "calibration_action_counts": dict(sorted(action_counts.items())),
        "gold_label_summary": {
            "gold_label_lines": gold_total,
            "exact_matches": gold_matches,
            "accuracy": round(gold_matches / gold_total, 6) if gold_total > 0 else None,
        },
        "pass_stage_scores": _pass_stage_scores(
            total_lines=total_lines,
            observed_lines=observed_lines,
            calibration_action_count=len(all_actions),
            gold_total=gold_total,
            gold_matches=gold_matches,
        ),
    }


def prepare_line_label_benchmark_artifacts(
    *,
    input_path: Path,
    output_path: Path,
) -> PreparedLineLabelBenchmarkArtifacts:
    """Parse and calibrate line-label benchmark artifacts before task finalization."""
    canonical_lines = load_canonical_lines(input_path)
    raw_predictions = load_line_predictions(output_path)
    aligned_predictions, alignment_actions = align_predictions_to_canonical_lines(
        canonical_lines=canonical_lines,
        predictions=raw_predictions,
    )
    calibrated_predictions, calibration_actions = calibrate_line_label_predictions(
        canonical_lines=canonical_lines,
        predictions=aligned_predictions,
    )
    pass_metrics = build_line_label_pass_metrics(
        canonical_lines=canonical_lines,
        raw_predictions=raw_predictions,
        calibrated_predictions=calibrated_predictions,
        all_actions=[*alignment_actions, *calibration_actions],
    )
    return PreparedLineLabelBenchmarkArtifacts(
        canonical_lines=canonical_lines,
        raw_predictions=raw_predictions,
        aligned_predictions=aligned_predictions,
        alignment_actions=alignment_actions,
        calibrated_predictions=calibrated_predictions,
        calibration_actions=calibration_actions,
        pass_metrics=pass_metrics,
    )


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_write(path: Path, payload: dict[str, object] | list[object]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_stage_json(
    *,
    root: Path,
    relpath: str,
    payload: dict[str, object] | list[object],
    files_manifest: dict[str, dict[str, object]],
) -> None:
    target = root / relpath
    _json_write(target, payload)
    files_manifest[relpath] = {
        "bytes": target.stat().st_size,
        "sha256": _sha256_file(target),
    }


def _write_stage_text(
    *,
    root: Path,
    relpath: str,
    text: str,
    files_manifest: dict[str, dict[str, object]],
) -> None:
    target = root / relpath
    target.write_text(text, encoding="utf-8")
    files_manifest[relpath] = {
        "bytes": target.stat().st_size,
        "sha256": _sha256_file(target),
    }


def write_line_label_benchmark_artifacts(
    *,
    run_output_dir: Path,
    run_id: str,
    task_id: str,
    pipeline_id: str,
    input_path: Path,
    output_path: Path,
    output_schema_path: Path,
    output_schema_logical_path: Path,
    selected_model: str,
    prompt_text: str,
    prepared: PreparedLineLabelBenchmarkArtifacts,
    debug_enabled: bool,
    raw_model_output_text: str | None,
    stdout_tail: str | None,
    stderr_tail: str | None,
) -> Path:
    """Write benchmark diagnostics to a stable per-task artifact folder."""
    artifacts_root = run_output_dir.resolve() / ".recipeimport-benchmark"
    artifacts_root.mkdir(parents=True, exist_ok=True)

    final_task_dir = artifacts_root / task_id
    stage_dir = artifacts_root / f".{task_id}.stage-{hashlib.sha256(_utc_now().encode()).hexdigest()[:8]}"
    stage_dir.mkdir(parents=True, exist_ok=False)
    files_manifest: dict[str, dict[str, object]] = {}

    try:
        _write_stage_json(
            root=stage_dir,
            relpath="canonical.lines.json",
            payload=[canonical_line_to_dict(row) for row in prepared.canonical_lines],
            files_manifest=files_manifest,
        )
        _write_stage_json(
            root=stage_dir,
            relpath="predictions.raw.json",
            payload=[line_prediction_to_dict(row) for row in prepared.raw_predictions],
            files_manifest=files_manifest,
        )
        _write_stage_json(
            root=stage_dir,
            relpath="predictions.aligned.json",
            payload=[line_prediction_to_dict(row) for row in prepared.aligned_predictions],
            files_manifest=files_manifest,
        )
        _write_stage_json(
            root=stage_dir,
            relpath="predictions.calibrated.json",
            payload=[line_prediction_to_dict(row) for row in prepared.calibrated_predictions],
            files_manifest=files_manifest,
        )
        _write_stage_json(
            root=stage_dir,
            relpath="calibration.actions.json",
            payload=[
                calibration_action_to_dict(row)
                for row in [*prepared.alignment_actions, *prepared.calibration_actions]
            ],
            files_manifest=files_manifest,
        )
        _write_stage_json(
            root=stage_dir,
            relpath="pass.metrics.json",
            payload=prepared.pass_metrics,
            files_manifest=files_manifest,
        )

        prediction_by_index = {row.line_index: row for row in prepared.calibrated_predictions}
        line_mappings: list[dict[str, object]] = []
        for line in prepared.canonical_lines:
            prediction = prediction_by_index.get(line.line_index)
            line_mappings.append(
                {
                    "line_index": line.line_index,
                    "line_text": line.text,
                    "expected_label": line.expected_label,
                    "prediction": line_prediction_to_dict(prediction)
                    if prediction is not None
                    else None,
                }
            )
        _write_stage_json(
            root=stage_dir,
            relpath="aligned.line_mappings.json",
            payload=line_mappings,
            files_manifest=files_manifest,
        )

        if debug_enabled:
            _write_stage_text(
                root=stage_dir,
                relpath="debug.request.prompt.txt",
                text=prompt_text,
                files_manifest=files_manifest,
            )
            if raw_model_output_text is not None:
                _write_stage_text(
                    root=stage_dir,
                    relpath="debug.response.output.raw.json",
                    text=raw_model_output_text,
                    files_manifest=files_manifest,
                )
            else:
                copied_output = stage_dir / "debug.response.output.raw.json"
                shutil.copyfile(output_path, copied_output)
                files_manifest["debug.response.output.raw.json"] = {
                    "bytes": copied_output.stat().st_size,
                    "sha256": _sha256_file(copied_output),
                }
            _write_stage_text(
                root=stage_dir,
                relpath="debug.response.stdout_tail.txt",
                text=stdout_tail or "",
                files_manifest=files_manifest,
            )
            _write_stage_text(
                root=stage_dir,
                relpath="debug.response.stderr_tail.txt",
                text=stderr_tail or "",
                files_manifest=files_manifest,
            )

        manifest = {
            "schema_version": RECIPEIMPORT_BENCHMARK_SCHEMA_VERSION,
            "mode": RECIPEIMPORT_BENCHMARK_MODE,
            "created_at": _utc_now(),
            "run_id": run_id,
            "task_id": task_id,
            "pipeline_id": pipeline_id,
            "input_path": str(input_path.resolve()),
            "output_path": str(output_path.resolve()),
            "model": selected_model,
            "output_schema_execution_path": str(output_schema_path.resolve()),
            "output_schema_logical_path": str(output_schema_logical_path.resolve()),
            "prompt_sha256": _sha256_text(prompt_text),
            "output_schema_sha256": _sha256_file(output_schema_path.resolve()),
            "pass_metrics": prepared.pass_metrics,
            "debug_enabled": bool(debug_enabled),
            "files": dict(sorted(files_manifest.items())),
        }
        _write_stage_json(
            root=stage_dir,
            relpath="debug.manifest.json",
            payload=manifest,
            files_manifest=files_manifest,
        )

        if final_task_dir.exists():
            shutil.rmtree(final_task_dir)
        stage_dir.replace(final_task_dir)
        return final_task_dir / "debug.manifest.json"
    except Exception:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise
