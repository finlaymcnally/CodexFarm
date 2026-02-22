---
summary: "Project-level hidden rules and runtime conventions."
read_when:
  - "When changing pipeline behavior, schema contracts, or worker/runtime semantics"
---

# IMPORTANT CONVENTIONS

- Pipeline behavior is data-driven. Add or edit pipeline JSON in `pipelines/` and keep referenced prompt/schema paths repo-relative.
- Pipelines may set `codex_cd_mode` (`asset_root`, `input_dir`, `input_file_dir`) to choose Codex `--cd` without code changes.
- Asset-root resolution precedence is `--root` flag, then `CODEX_FARM_ROOT`, then upward auto-discovery; the chosen root must contain `pipelines/`, `prompts/`, and `schemas/`.
- Run metadata in `runs.config_json` stores absolute `farm_root` and optional explicit `workspace_root`; workers prefer those persisted paths so resumed runs keep the same pipeline pack and Codex `--cd` behavior.
- `--workspace-root` is an explicit override; when absent, workers resolve Codex `--cd` from pipeline `codex_cd_mode`.
- `process --json` is a machine contract: stdout must be JSON only. Progress lines go to stderr.
- `process` worker slots are in-process threads (`ThreadPoolExecutor`) and each worker opens its own SQLite connection; lease logic in `db.py` is the concurrency guard.
- Task outputs are written atomically via temp file + rename in `codex_exec.py`; never mark tasks done on partial writes.
- `run tasks --json` and `run errors --json` are the supported way to inspect per-task failures programmatically without reading SQLite directly.
- Codex CLI compatibility rule: pass approval policy as a global flag (`codex --ask-for-approval ... exec ...`), not as `codex exec --ask-for-approval ...`.
- Always include `--skip-git-repo-check` in codex worker/doctor calls; this repo may run in directories without `.git/`.
- Codex `--output-schema` currently requires `required` to include every key listed in `properties`; model optional recipe fields as nullable required fields.
- A non-zero codex exit does not always mean unusable output. If `--output-last-message` writes a non-empty file, codex-farm keeps it and relies on local schema validation.
- `recipe.schemaorg.normalize.v1` enforces one canonical instructions shape: `recipeInstructions` must be an array of `{"@type":"HowToStep","text":...}` objects.
- `recipeimport_intermediate_fullshape_v1.schema.json` and `recipeimport_final_fullshape_v1.schema.json` are inbound validation contracts and must validate both sparse real samples and platonic full-shape samples under `examples/recipeimport_*`.
- Always run tests in `.venv` with editable dev install: `source .venv/bin/activate && pip install -e '.[dev]' && pytest`.
