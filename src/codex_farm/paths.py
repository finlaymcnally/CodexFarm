"""Path helpers for locating repository assets and data directories."""

from __future__ import annotations

import os
from pathlib import Path


REPO_SENTINELS = ("pipelines", "prompts", "schemas")


def _missing_repo_parts(path: Path) -> list[str]:
    return [part for part in REPO_SENTINELS if not (path / part).exists()]


def _looks_like_repo_root(path: Path) -> bool:
    return not _missing_repo_parts(path)


def _validate_repo_root(path: Path, *, source_label: str) -> Path:
    missing = _missing_repo_parts(path)
    if missing:
        joined = ", ".join(missing)
        raise FileNotFoundError(
            f"{source_label} points to '{path}', but it is missing required folders: {joined}. "
            "Expected pipelines/, prompts/, and schemas/ directly under that root."
        )
    return path


def resolve_farm_root(
    root_override: Path | str | None = None,
    *,
    start: Path | None = None,
) -> Path:
    """Resolve the codex-farm asset root with explicit override precedence."""
    if root_override is not None:
        candidate = Path(root_override).expanduser().resolve()
        return _validate_repo_root(candidate, source_label="--root")

    env_root = os.environ.get("CODEX_FARM_ROOT")
    if env_root:
        candidate = Path(env_root).expanduser().resolve()
        return _validate_repo_root(candidate, source_label="CODEX_FARM_ROOT")

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
        "Could not find codex-farm root by searching from the current directory and module path. "
        "Pass --root PATH or set CODEX_FARM_ROOT to a directory containing pipelines/, prompts/, and schemas/."
    )


def find_repo_root(start: Path | None = None) -> Path:
    """Backward-compatible root discovery helper."""
    return resolve_farm_root(start=start)


def resolve_data_dir(data_dir: Path | str) -> Path:
    return Path(data_dir).expanduser().resolve()


def db_path_for_data_dir(data_dir: Path | str) -> Path:
    return resolve_data_dir(data_dir) / "codex_farm.sqlite3"
