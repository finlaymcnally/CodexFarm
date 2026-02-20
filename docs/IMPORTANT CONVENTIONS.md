# IMPORTANT CONVENTIONS

- Pipeline behavior is data-driven. Add or edit pipeline JSON in `pipelines/` and keep referenced prompt/schema paths repo-relative.
- `process` worker slots are in-process threads (`ThreadPoolExecutor`) and each worker opens its own SQLite connection; lease logic in `db.py` is the concurrency guard.
- Task outputs are written atomically via temp file + rename in `codex_exec.py`; never mark tasks done on partial writes.
- Codex CLI compatibility rule: pass approval policy as a global flag (`codex --ask-for-approval ... exec ...`), not as `codex exec --ask-for-approval ...`.
- Always include `--skip-git-repo-check` in codex worker/doctor calls; this repo may run in directories without `.git/`.
- Codex `--output-schema` currently requires `required` to include every key listed in `properties`; model optional recipe fields as nullable required fields.
- A non-zero codex exit does not always mean unusable output. If `--output-last-message` writes a non-empty file, codex-farm keeps it and relies on local schema validation.
- `recipe.schemaorg.normalize.v1` enforces one canonical instructions shape: `recipeInstructions` must be an array of `{"@type":"HowToStep","text":...}` objects.
- Always run tests in `.venv` with editable dev install: `source .venv/bin/activate && pip install -e '.[dev]' && pytest`.
