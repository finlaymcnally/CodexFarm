"""Deterministic calibration rules for benchmark line-label outputs."""

from __future__ import annotations

import re

from .recipeimport_benchmark_types import CalibrationAction, CanonicalLine, LineLabelPrediction


_LABEL_SANITIZE_PATTERN = re.compile(r"[^a-z0-9_]+")
_MULTI_UNDERSCORE_PATTERN = re.compile(r"_+")


def _normalize_label(value: str) -> str:
    lowered = value.strip().lower().replace("-", "_").replace(" ", "_")
    lowered = _LABEL_SANITIZE_PATTERN.sub("_", lowered)
    lowered = _MULTI_UNDERSCORE_PATTERN.sub("_", lowered).strip("_")
    return lowered


def _normalize_tag(value: str) -> str:
    cleaned = value.strip().lower().replace("-", "_").replace(" ", "_")
    cleaned = _LABEL_SANITIZE_PATTERN.sub("_", cleaned)
    return _MULTI_UNDERSCORE_PATTERN.sub("_", cleaned).strip("_")


def calibrate_line_label_predictions(
    *,
    canonical_lines: list[CanonicalLine],
    predictions: list[LineLabelPrediction],
    low_confidence_floor: float = 0.2,
) -> tuple[list[LineLabelPrediction], list[CalibrationAction]]:
    """Normalize labels/confidence/evidence/tags into deterministic benchmark outputs."""
    actions: list[CalibrationAction] = []
    valid_indices = {line.line_index for line in canonical_lines}

    calibrated: list[LineLabelPrediction] = []
    for prediction in predictions:
        line_index = prediction.line_index

        confidence = prediction.confidence
        clamped_confidence = max(0.0, min(1.0, confidence))
        if clamped_confidence != confidence:
            actions.append(
                CalibrationAction(
                    line_index=line_index,
                    action="clamp_confidence",
                    detail="Confidence was clamped to [0, 1].",
                    before=str(confidence),
                    after=str(clamped_confidence),
                )
            )
        confidence = clamped_confidence

        normalized_label = _normalize_label(prediction.label)
        if not normalized_label:
            normalized_label = "unlabeled"
        if normalized_label != prediction.label:
            actions.append(
                CalibrationAction(
                    line_index=line_index,
                    action="normalize_label",
                    detail="Label was normalized to lowercase underscore format.",
                    before=prediction.label,
                    after=normalized_label,
                )
            )

        if confidence < low_confidence_floor and normalized_label != "unlabeled":
            actions.append(
                CalibrationAction(
                    line_index=line_index,
                    action="low_confidence_relabel",
                    detail=(
                        "Confidence below threshold; relabeled to 'unlabeled' for deterministic"
                        " benchmark scoring."
                    ),
                    before=normalized_label,
                    after="unlabeled",
                )
            )
            normalized_label = "unlabeled"

        filtered_evidence: list[int] = []
        for raw_index in prediction.evidence_line_indices:
            if raw_index in valid_indices:
                filtered_evidence.append(raw_index)
        filtered_evidence = sorted(set(filtered_evidence))
        if not filtered_evidence:
            filtered_evidence = [line_index]
            actions.append(
                CalibrationAction(
                    line_index=line_index,
                    action="fill_evidence_indices",
                    detail="No valid evidence indices found; defaulted to the current line index.",
                    before=str(list(prediction.evidence_line_indices)),
                    after=str(filtered_evidence),
                )
            )
        elif tuple(filtered_evidence) != tuple(prediction.evidence_line_indices):
            actions.append(
                CalibrationAction(
                    line_index=line_index,
                    action="normalize_evidence_indices",
                    detail="Evidence indices were deduplicated and restricted to known lines.",
                    before=str(list(prediction.evidence_line_indices)),
                    after=str(filtered_evidence),
                )
            )

        normalized_tags: list[str] = []
        for tag in prediction.reasoning_tags:
            normalized = _normalize_tag(tag)
            if normalized:
                normalized_tags.append(normalized)
        normalized_tags = sorted(set(normalized_tags))
        if not normalized_tags:
            normalized_tags = ["unspecified"]
            actions.append(
                CalibrationAction(
                    line_index=line_index,
                    action="fill_reasoning_tags",
                    detail="Reasoning tags were empty after normalization.",
                    before=str(list(prediction.reasoning_tags)),
                    after=str(normalized_tags),
                )
            )
        elif tuple(normalized_tags) != tuple(prediction.reasoning_tags):
            actions.append(
                CalibrationAction(
                    line_index=line_index,
                    action="normalize_reasoning_tags",
                    detail="Reasoning tags were normalized and deduplicated.",
                    before=str(list(prediction.reasoning_tags)),
                    after=str(normalized_tags),
                )
            )

        calibrated.append(
            LineLabelPrediction(
                line_index=line_index,
                label=normalized_label,
                confidence=confidence,
                evidence_line_indices=tuple(filtered_evidence),
                reasoning_tags=tuple(normalized_tags),
            )
        )

    calibrated.sort(key=lambda row: row.line_index)
    return calibrated, actions
