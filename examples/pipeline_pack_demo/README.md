Tiny external pipeline pack demo for `--root` and pipeline-driven `codex_cd_mode`.

`demo.echo.v1` sets `"codex_cd_mode": "input_dir"` and uses `INPUT={{INPUT_PATH}}` in
the prompt so fake-codex integration tests can assert prompt substitution and `--cd`.
