"""Codex model-discovery helpers used by caller-facing model pickers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


CODEX_REASONING_EFFORT_VALUES = ("none", "minimal", "low", "medium", "high", "xhigh")
DEFAULT_CODEX_MODEL = "gpt-5.3-codex-spark"


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve(strict=False)
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def _codex_home_roots() -> list[Path]:
    roots: list[Path] = []
    env_root = (os.environ.get("CODEX_HOME") or "").strip()
    if env_root:
        roots.append(Path(env_root).expanduser())

    # Primary Codex home first, then historical/alternate homes.
    roots.extend([Path.home() / ".codex", Path.home() / ".codex-alt"])
    for path in sorted(Path.home().glob(".codex*")):
        if path.is_dir():
            roots.append(path)
    return _dedupe_paths(roots)


def _codex_models_cache_paths() -> list[Path]:
    return [root / "models_cache.json" for root in _codex_home_roots()]


def _supported_reasoning_efforts_from_row(row: dict[str, Any]) -> list[str]:
    raw_levels = row.get("supported_reasoning_levels")
    if not isinstance(raw_levels, list):
        return []

    efforts: list[str] = []
    seen: set[str] = set()
    for level in raw_levels:
        candidate: Any = level
        if isinstance(level, dict):
            candidate = level.get("effort")
        if not isinstance(candidate, str):
            continue

        normalized = candidate.strip().lower()
        if not normalized or normalized not in CODEX_REASONING_EFFORT_VALUES:
            continue
        if normalized in seen:
            continue
        efforts.append(normalized)
        seen.add(normalized)
    return efforts


def list_codex_models() -> list[dict[str, Any]]:
    """Return visible model rows from local Codex cache files."""
    models: list[dict[str, Any]] = []
    seen: set[str] = set()

    for cache_path in _codex_models_cache_paths():
        if not cache_path.exists():
            continue
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        rows = payload.get("models")
        if not isinstance(rows, list):
            continue

        for row in rows:
            if not isinstance(row, dict):
                continue

            slug = str(row.get("slug") or "").strip()
            if not slug or slug in seen:
                continue

            visibility = str(row.get("visibility") or "").strip().lower()
            if visibility and visibility not in {"list", "default"}:
                continue

            description = str(row.get("description") or "").strip()
            display_name = str(row.get("display_name") or slug).strip() or slug
            entry: dict[str, Any] = {
                "slug": slug,
                "display_name": display_name,
                "description": description,
            }

            supported_efforts = _supported_reasoning_efforts_from_row(row)
            if supported_efforts:
                entry["supported_reasoning_efforts"] = supported_efforts

            models.append(entry)
            seen.add(slug)

    if models:
        return models

    return [
        {
            "slug": DEFAULT_CODEX_MODEL,
            "display_name": DEFAULT_CODEX_MODEL,
            "description": "fallback default (no local models_cache.json found)",
        }
    ]
