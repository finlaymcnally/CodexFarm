"""Typed models for recipeimport line-label benchmark processing."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CanonicalLine:
    line_index: int
    text: str
    expected_label: str | None = None


@dataclass(frozen=True)
class LineLabelPrediction:
    line_index: int
    label: str
    confidence: float
    evidence_line_indices: tuple[int, ...]
    reasoning_tags: tuple[str, ...]


@dataclass(frozen=True)
class CalibrationAction:
    line_index: int
    action: str
    detail: str
    before: str | None = None
    after: str | None = None


def canonical_line_to_dict(line: CanonicalLine) -> dict[str, object]:
    return {
        "line_index": line.line_index,
        "text": line.text,
        "expected_label": line.expected_label,
    }


def line_prediction_to_dict(prediction: LineLabelPrediction) -> dict[str, object]:
    return {
        "line_index": prediction.line_index,
        "label": prediction.label,
        "confidence": prediction.confidence,
        "evidence_line_indices": list(prediction.evidence_line_indices),
        "reasoning_tags": list(prediction.reasoning_tags),
    }


def calibration_action_to_dict(action: CalibrationAction) -> dict[str, object]:
    return {
        "line_index": action.line_index,
        "action": action.action,
        "detail": action.detail,
        "before": action.before,
        "after": action.after,
    }
