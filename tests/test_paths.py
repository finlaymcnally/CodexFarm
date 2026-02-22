from pathlib import Path

import pytest

from codex_farm.paths import resolve_farm_root


def _make_pack_root(path: Path) -> None:
    for folder in ("pipelines", "prompts", "schemas"):
        (path / folder).mkdir(parents=True, exist_ok=True)


def test_resolve_farm_root_explicit_override_wins_over_env(monkeypatch, tmp_path: Path) -> None:
    env_root = tmp_path / "env"
    explicit_root = tmp_path / "explicit"
    _make_pack_root(env_root)
    _make_pack_root(explicit_root)

    monkeypatch.setenv("CODEX_FARM_ROOT", str(env_root))

    resolved = resolve_farm_root(explicit_root)

    assert resolved == explicit_root.resolve()


def test_resolve_farm_root_rejects_invalid_override(tmp_path: Path) -> None:
    invalid_root = tmp_path / "invalid"
    invalid_root.mkdir(parents=True)

    with pytest.raises(FileNotFoundError) as excinfo:
        resolve_farm_root(invalid_root)

    message = str(excinfo.value)
    assert "missing required folders" in message
    assert "pipelines" in message


def test_resolve_farm_root_rejects_invalid_env(monkeypatch, tmp_path: Path) -> None:
    invalid_root = tmp_path / "invalid"
    invalid_root.mkdir(parents=True)
    monkeypatch.setenv("CODEX_FARM_ROOT", str(invalid_root))

    with pytest.raises(FileNotFoundError):
        resolve_farm_root(None)
