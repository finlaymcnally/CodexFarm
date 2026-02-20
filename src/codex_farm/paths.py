"""Path helpers for locating repository assets and data directories."""

from __future__ import annotations

import os
from pathlib import Path


REPO_SENTINELS = ("pipelines", "prompts", "schemas")


def _looks_like_repo_root(path: Path) -> bool:
    return all((path / part).exists() for part in REPO_SENTINELS)


def find_repo_root(start: Path | None = None) -> Path:
    """Find the project root by searching upward for required asset folders."""
    env_root = os.environ.get("CODEX_FARM_ROOT")
    if env_root:
        candidate = Path(env_root).expanduser().resolve()
        if _looks_like_repo_root(candidate):
            return candidate

    candidates: list[Path] = []
    if start is not None:
        candidates.append(start.expanduser().resolve())
    candidates.append(Path.cwd().resolve())
    candidates.append(Path(__file__).resolve())

    for base in candidates:
        for parent in (base, *base.parents):
            if _looks_like_repo_root(parent):
                return parent

    raise FileNotFoundError(
        "Could not find codex-farm repository root. Set CODEX_FARM_ROOT if needed."
    )


def resolve_data_dir(data_dir: Path | str) -> Path:
    return Path(data_dir).expanduser().resolve()


def db_path_for_data_dir(data_dir: Path | str) -> Path:
    return resolve_data_dir(data_dir) / "codex_farm.sqlite3"
