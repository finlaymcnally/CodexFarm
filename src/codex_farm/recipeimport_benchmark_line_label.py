"""Line-label benchmark parsing and alignment helpers."""

from __future__ import annotations

import json
from pathlib import Path

from .recipeimport_benchmark_types import (
    CalibrationAction,
    CanonicalLine,
    LineLabelPrediction,
)


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None
    return None


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _as_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON for benchmark file {path}: {exc}") from exc


def _line_text_from_row(row: dict[str, object]) -> str | None:
    for key in ("text", "line", "canonical_text", "raw_text"):
        text = _as_text(row.get(key))
        if text is not None:
            return text
    return None


def _expected_label_from_row(row: dict[str, object]) -> str | None:
    for key in ("expected_label", "gold_label", "label"):
        expected = _as_text(row.get(key))
        if expected is not None:
            return expected
    return None


def load_canonical_lines(input_path: Path) -> list[CanonicalLine]:
    """Load canonical input lines from benchmark input JSON."""
    payload = _load_json(input_path)
    entries: object
    if isinstance(payload, dict):
        entries = payload.get("canonical_lines", payload.get("lines"))
    else:
        entries = payload

    if not isinstance(entries, list):
        raise ValueError(
            "Benchmark input must contain a list under 'canonical_lines' (or 'lines')."
        )

    lines: list[CanonicalLine] = []
    seen_indices: set[int] = set()
    for row_idx, entry in enumerate(entries):
        if isinstance(entry, str):
            text = entry.strip()
            if not text:
                raise ValueError(f"Benchmark canonical line at index {row_idx} is empty.")
            line_index = row_idx
            expected_label = None
        elif isinstance(entry, dict):
            raw_index = _as_int(entry.get("line_index"))
            line_index = raw_index if raw_index is not None else row_idx
            text = _line_text_from_row(entry)
            if text is None:
                raise ValueError(
                    f"Benchmark canonical line at index {row_idx} is missing text."
                )
            expected_label = _expected_label_from_row(entry)
        else:
            raise ValueError(
                f"Benchmark canonical line at index {row_idx} must be string or object."
            )

        if line_index < 0:
            raise ValueError(f"Benchmark canonical line index must be >= 0 (got {line_index}).")
        if line_index in seen_indices:
            raise ValueError(f"Duplicate canonical line index: {line_index}")
        seen_indices.add(line_index)
        lines.append(
            CanonicalLine(
                line_index=line_index,
                text=text,
                expected_label=expected_label,
            )
        )

    lines.sort(key=lambda row: row.line_index)
    return lines


def load_line_predictions(output_path: Path) -> list[LineLabelPrediction]:
    """Load model line-label predictions from benchmark output JSON."""
    payload = _load_json(output_path)
    if not isinstance(payload, dict):
        raise ValueError("Benchmark output must be an object with 'line_predictions'.")
    raw_predictions = payload.get("line_predictions")
    if not isinstance(raw_predictions, list):
        raise ValueError("Benchmark output is missing list field 'line_predictions'.")

    predictions: list[LineLabelPrediction] = []
    for row_idx, entry in enumerate(raw_predictions):
        if not isinstance(entry, dict):
            raise ValueError(f"line_predictions[{row_idx}] must be an object.")

        line_index = _as_int(entry.get("line_index"))
        if line_index is None or line_index < 0:
            raise ValueError(f"line_predictions[{row_idx}].line_index must be >= 0.")

        label = _as_text(entry.get("label"))
        if label is None:
            raise ValueError(f"line_predictions[{row_idx}].label is required.")

        confidence = _as_float(entry.get("confidence"))
        if confidence is None:
            raise ValueError(f"line_predictions[{row_idx}].confidence is required.")

        evidence_raw = entry.get("evidence_line_indices")
        if not isinstance(evidence_raw, list):
            raise ValueError(
                f"line_predictions[{row_idx}].evidence_line_indices must be a list."
            )
        evidence: list[int] = []
        for evidence_row in evidence_raw:
            index_value = _as_int(evidence_row)
            if index_value is None:
                raise ValueError(
                    f"line_predictions[{row_idx}] has non-integer evidence index."
                )
            evidence.append(index_value)

        reasoning_raw = entry.get("reasoning_tags")
        if not isinstance(reasoning_raw, list):
            raise ValueError(f"line_predictions[{row_idx}].reasoning_tags must be a list.")
        reasoning: list[str] = []
        for tag in reasoning_raw:
            tag_text = _as_text(tag)
            if tag_text is None:
                raise ValueError(
                    f"line_predictions[{row_idx}] has empty/non-string reasoning tag."
                )
            reasoning.append(tag_text)

        predictions.append(
            LineLabelPrediction(
                line_index=line_index,
                label=label,
                confidence=confidence,
                evidence_line_indices=tuple(evidence),
                reasoning_tags=tuple(reasoning),
            )
        )

    return predictions


def align_predictions_to_canonical_lines(
    *,
    canonical_lines: list[CanonicalLine],
    predictions: list[LineLabelPrediction],
) -> tuple[list[LineLabelPrediction], list[CalibrationAction]]:
    """Align potentially sparse/duplicate predictions to canonical input lines."""
    actions: list[CalibrationAction] = []
    expected_indices = {line.line_index for line in canonical_lines}
    by_index: dict[int, LineLabelPrediction] = {}

    for prediction in predictions:
        if prediction.line_index not in expected_indices:
            actions.append(
                CalibrationAction(
                    line_index=prediction.line_index,
                    action="drop_unknown_line_index",
                    detail="Prediction line index does not exist in canonical input.",
                    before=prediction.label,
                    after=None,
                )
            )
            continue

        existing = by_index.get(prediction.line_index)
        if existing is None:
            by_index[prediction.line_index] = prediction
            continue
        if prediction.confidence > existing.confidence:
            by_index[prediction.line_index] = prediction
            winner = prediction.label
        else:
            winner = existing.label
        actions.append(
            CalibrationAction(
                line_index=prediction.line_index,
                action="dedupe_line_index",
                detail="Multiple predictions for one line index; kept highest confidence.",
                before=existing.label,
                after=winner,
            )
        )

    aligned: list[LineLabelPrediction] = []
    for line in canonical_lines:
        chosen = by_index.get(line.line_index)
        if chosen is None:
            aligned.append(
                LineLabelPrediction(
                    line_index=line.line_index,
                    label="unlabeled",
                    confidence=0.0,
                    evidence_line_indices=(line.line_index,),
                    reasoning_tags=("missing_prediction",),
                )
            )
            actions.append(
                CalibrationAction(
                    line_index=line.line_index,
                    action="fill_missing_prediction",
                    detail="Synthesized fallback prediction because model omitted this line.",
                    before=None,
                    after="unlabeled",
                )
            )
            continue
        aligned.append(chosen)

    aligned.sort(key=lambda row: row.line_index)
    return aligned, actions
