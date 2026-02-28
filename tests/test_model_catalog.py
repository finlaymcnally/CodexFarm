import json
from pathlib import Path

from codex_farm.model_catalog import DEFAULT_CODEX_MODEL, list_codex_models


def _write_models_cache(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def test_list_codex_models_reads_visible_rows_and_normalizes_efforts(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)

    _write_models_cache(
        tmp_path / ".codex" / "models_cache.json",
        {
            "models": [
                {
                    "slug": "gpt-5.3-codex",
                    "display_name": "GPT 5.3 Codex",
                    "description": "Primary model",
                    "visibility": "list",
                    "supported_reasoning_levels": [
                        "HIGH",
                        {"effort": "low"},
                        {"effort": "invalid"},
                    ],
                },
                {
                    "slug": "gpt-5.3-codex-mini",
                    "visibility": "default",
                    "supported_reasoning_levels": [{"effort": "none"}],
                },
                {
                    "slug": "internal-model",
                    "visibility": "private",
                },
            ]
        },
    )
    _write_models_cache(
        tmp_path / ".codex-alt" / "models_cache.json",
        {
            "models": [
                {
                    "slug": "gpt-5.3-codex",
                    "description": "Duplicate should be ignored",
                }
            ]
        },
    )

    models = list_codex_models()

    assert [row["slug"] for row in models] == ["gpt-5.3-codex", "gpt-5.3-codex-mini"]
    assert models[0]["display_name"] == "GPT 5.3 Codex"
    assert models[0]["supported_reasoning_efforts"] == ["high", "low"]
    assert models[1]["supported_reasoning_efforts"] == ["none"]


def test_list_codex_models_falls_back_to_default_when_cache_missing(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)

    models = list_codex_models()

    assert len(models) == 1
    assert models[0]["slug"] == DEFAULT_CODEX_MODEL
