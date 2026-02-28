import json
from pathlib import Path
import threading
import time

import pytest

from codex_farm.codex_exec import CodexExecResult
from codex_farm.db import (
    create_run,
    enqueue_tasks_for_run,
    get_run_throttle_state,
    init_db,
    lease_one_task,
    list_failure_forensics,
    list_heads_up_tips,
    list_tasks_for_run,
    mark_task_done,
    open_db,
    run_status,
    set_run_control_state,
    upsert_heads_up_tips,
)
import codex_farm.lease_heartbeat as lease_heartbeat
from codex_farm.paths import find_repo_root
from codex_farm.pipeline_spec import load_pipelines
from codex_farm.run_assets import freeze_run_assets
from codex_farm.worker import worker_loop


def _fake_recipe(name: str) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "Recipe",
        "name": name,
        "description": None,
        "recipeYield": None,
        "prepTime": None,
        "cookTime": None,
        "totalTime": None,
        "recipeIngredient": ["1 cup water"],
        "recipeInstructions": [
            {
                "@type": "HowToStep",
                "text": "Boil water."
            }
        ]
    }


def _write_demo_pack(root: Path, *, pipeline_id: str, codex_cd_mode: str) -> None:
    for folder in ("pipelines", "prompts", "schemas"):
        (root / folder).mkdir(parents=True, exist_ok=True)

    slug = pipeline_id.replace(".", "_")
    prompt_rel = Path("prompts") / f"{slug}.txt"
    schema_rel = Path("schemas") / f"{slug}.schema.json"
    pipeline_path = root / "pipelines" / f"{pipeline_id}.json"

    pipeline_payload = {
        "pipeline_id": pipeline_id,
        "description": f"Pipeline {pipeline_id}",
        "prompt_template_path": prompt_rel.as_posix(),
        "output_schema_path": schema_rel.as_posix(),
        "input_glob_default": "**/*.json",
        "output_ext": ".json",
        "codex_model": "gpt-5.3-codex-spark",
        "codex_sandbox": "read-only",
        "codex_ask_for_approval": "never",
        "codex_web_search": "disabled",
        "codex_timeout_seconds": 180,
        "codex_cd_mode": codex_cd_mode,
    }
    pipeline_path.write_text(json.dumps(pipeline_payload, indent=2) + "\n", encoding="utf-8")

    (root / prompt_rel).write_text("Input file path: {{INPUT_PATH}}\n", encoding="utf-8")
    (root / schema_rel).write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["ok", "source_path"],
                "properties": {
                    "ok": {"type": "string"},
                    "source_path": {"type": "string"},
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def test_worker_loop_processes_task_with_mocked_codex(monkeypatch, tmp_path: Path) -> None:
    repo_root = find_repo_root()
    pipelines = load_pipelines(repo_root / "pipelines")
    spec = pipelines["recipe.schemaorg.normalize.v1"]

    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    data_dir = tmp_path / "var"
    workspace_root = tmp_path / "workspace"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    workspace_root.mkdir(parents=True)

    input_path = input_dir / "r1.json"
    input_path.write_text(json.dumps({"name": "Mock Chili"}), encoding="utf-8")

    conn = open_db(data_dir / "codex_farm.sqlite3")
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id=spec.pipeline_id,
        input_dir=str(input_dir),
        glob="**/*.json",
        output_dir=str(output_dir),
        config={
            "farm_root": str(repo_root),
            "workspace_root": str(workspace_root),
            "codex_model": "gpt-test-override",
            "codex_reasoning_effort": "medium",
        },
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[input_path],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=spec.output_ext,
    )

    captured_cd_dirs: list[str] = []
    captured_models: list[str] = []
    captured_efforts: list[str] = []

    def fake_run_codex_exec(**kwargs):
        captured_cd_dirs.append(str(kwargs["cd_dir"]))
        captured_models.append(str(kwargs["model"]))
        captured_efforts.append(str(kwargs.get("reasoning_effort")))
        out = kwargs["output_path"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(_fake_recipe("Mock Chili")), encoding="utf-8")
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)

    code = worker_loop(
        data_dir=data_dir,
        worker_id="test-worker",
        run_id=run_id,
        lease_seconds=120,
        max_attempts=3,
        poll_seconds=0.01,
        once=True,
    )

    assert code == 0
    produced = output_dir / "r1.json"
    assert produced.exists()

    status = run_status(conn, run_id=run_id)
    assert status["done"] == 1
    assert status["error"] == 0
    assert captured_cd_dirs == [str(workspace_root.resolve())]
    assert captured_models == ["gpt-test-override"]
    assert captured_efforts == ["medium"]
    tasks = list_tasks_for_run(conn, run_id=run_id)
    assert tasks[0]["execution_attempts"] == 1
    assert isinstance(tasks[0]["last_heartbeat_at"], str)


def test_worker_heartbeat_prevents_lease_reclaim_for_long_running_task(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo_root = find_repo_root()
    pipelines = load_pipelines(repo_root / "pipelines")
    spec = pipelines["recipe.schemaorg.normalize.v1"]

    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    data_dir = tmp_path / "var"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    input_path = input_dir / "slow.json"
    input_path.write_text(json.dumps({"name": "slow"}), encoding="utf-8")

    conn = open_db(data_dir / "codex_farm.sqlite3")
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id=spec.pipeline_id,
        input_dir=str(input_dir.resolve()),
        glob="**/*.json",
        output_dir=str(output_dir.resolve()),
        config={"farm_root": str(repo_root.resolve())},
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[input_path],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=spec.output_ext,
    )

    call_worker_ids: list[str] = []
    call_lock = threading.Lock()
    codex_exec_started = threading.Event()
    release_codex_exec = threading.Event()
    heartbeat_seen = threading.Event()

    original_heartbeat = lease_heartbeat.heartbeat_task_lease

    def wrapped_heartbeat_task_lease(*args, **kwargs):
        updated = original_heartbeat(*args, **kwargs)
        if updated:
            heartbeat_seen.set()
        return updated

    monkeypatch.setattr(
        lease_heartbeat,
        "heartbeat_task_lease",
        wrapped_heartbeat_task_lease,
    )

    def fake_run_codex_exec(**kwargs):
        with call_lock:
            call_worker_ids.append(str(kwargs["usage_context"]["worker_id"]))
        codex_exec_started.set()
        release_codex_exec.wait(timeout=8.0)
        out = kwargs["output_path"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(_fake_recipe("slow")), encoding="utf-8")
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)

    results: dict[str, int] = {}

    def run_named_worker(name: str) -> None:
        results[name] = worker_loop(
            data_dir=data_dir,
            worker_id=name,
            run_id=run_id,
            lease_seconds=1,
            max_attempts=3,
            poll_seconds=0.02,
            once=True,
            farm_root=repo_root,
        )

    worker_one = threading.Thread(target=run_named_worker, args=("worker-a",), daemon=True)
    worker_two = threading.Thread(target=run_named_worker, args=("worker-b",), daemon=True)
    worker_one.start()
    if not codex_exec_started.wait(timeout=3.0):
        release_codex_exec.set()
        pytest.fail("worker-a did not start codex execution in time")
    if not heartbeat_seen.wait(timeout=5.0):
        release_codex_exec.set()
        pytest.fail("did not observe lease heartbeat before contention phase")
    worker_two.start()
    time.sleep(1.3)
    release_codex_exec.set()
    worker_one.join(timeout=8.0)
    worker_two.join(timeout=8.0)

    assert not worker_one.is_alive()
    assert not worker_two.is_alive()
    assert results["worker-a"] == 0
    assert results["worker-b"] == 0
    assert call_worker_ids == ["worker-a"]

    status = run_status(conn, run_id=run_id)
    assert status["done"] == 1
    assert status["error"] == 0
    tasks = list_tasks_for_run(conn, run_id=run_id)
    assert len(tasks) == 1
    assert tasks[0]["attempts"] == 1
    assert tasks[0]["execution_attempts"] == 1
    assert isinstance(tasks[0]["last_heartbeat_at"], str)
    assert tasks[0]["last_heartbeat_at"]


@pytest.mark.parametrize(
    ("codex_cd_mode", "expected_kind"),
    [
        ("asset_root", "asset_root"),
        ("input_dir", "input_dir"),
        ("input_file_dir", "input_file_dir"),
    ],
)
def test_worker_loop_selects_cd_dir_from_pipeline_mode(
    monkeypatch,
    tmp_path: Path,
    codex_cd_mode: str,
    expected_kind: str,
) -> None:
    pack_root = tmp_path / "pack"
    pipeline_id = f"demo.{codex_cd_mode}.v1"
    _write_demo_pack(pack_root, pipeline_id=pipeline_id, codex_cd_mode=codex_cd_mode)
    pipelines = load_pipelines(pack_root / "pipelines")
    spec = pipelines[pipeline_id]

    input_dir = tmp_path / "input_root"
    nested_dir = input_dir / "nested"
    output_dir = tmp_path / "out"
    data_dir = tmp_path / "var"
    nested_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)

    input_path = nested_dir / "a.json"
    input_path.write_text("{}", encoding="utf-8")

    conn = open_db(data_dir / "codex_farm.sqlite3")
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id=spec.pipeline_id,
        input_dir=str(input_dir.resolve()),
        glob="**/*.json",
        output_dir=str(output_dir.resolve()),
        config={
            "farm_root": str(pack_root.resolve()),
        },
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[input_path],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=spec.output_ext,
    )

    captured_cd_dirs: list[str] = []

    def fake_run_codex_exec(**kwargs):
        captured_cd_dirs.append(str(kwargs["cd_dir"]))
        out = kwargs["output_path"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps({"ok": "OK", "source_path": str(input_path.resolve())}),
            encoding="utf-8",
        )
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)

    code = worker_loop(
        data_dir=data_dir,
        worker_id="test-worker",
        run_id=run_id,
        lease_seconds=120,
        max_attempts=3,
        poll_seconds=0.01,
        once=True,
    )

    assert code == 0
    assert len(captured_cd_dirs) == 1

    if expected_kind == "asset_root":
        expected_dir = pack_root.resolve()
    elif expected_kind == "input_dir":
        expected_dir = input_dir.resolve()
    else:
        expected_dir = input_path.resolve().parent

    assert captured_cd_dirs[0] == str(expected_dir)


def test_worker_loop_requeues_rate_limit_and_recovers(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo_root = find_repo_root()
    pipelines = load_pipelines(repo_root / "pipelines")
    spec = pipelines["recipe.schemaorg.normalize.v1"]

    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    data_dir = tmp_path / "var"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)

    first_input = input_dir / "r1.json"
    second_input = input_dir / "r2.json"
    first_input.write_text(json.dumps({"name": "One"}), encoding="utf-8")
    second_input.write_text(json.dumps({"name": "Two"}), encoding="utf-8")

    conn = open_db(data_dir / "codex_farm.sqlite3")
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id=spec.pipeline_id,
        input_dir=str(input_dir),
        glob="**/*.json",
        output_dir=str(output_dir),
        config={"farm_root": str(repo_root)},
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[first_input, second_input],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=spec.output_ext,
    )

    warnings: list[str] = []

    call_count = 0
    captured_prompts: list[str] = []

    def fake_run_codex_exec(**kwargs):
        nonlocal call_count
        call_count += 1
        captured_prompts.append(str(kwargs["prompt"]))
        if call_count == 1:
            return CodexExecResult(
                ok=False,
                exit_code=1,
                stderr_tail="HTTP 429 Too Many Requests: retry after 1 seconds",
            )

        out = kwargs["output_path"]
        out.parent.mkdir(parents=True, exist_ok=True)
        name = Path(kwargs["prompt"].split("Input file path: ")[-1].strip().splitlines()[0]).stem
        out.write_text(json.dumps(_fake_recipe(name)), encoding="utf-8")
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)

    code = worker_loop(
        data_dir=data_dir,
        worker_id="test-worker",
        run_id=run_id,
        lease_seconds=120,
        max_attempts=3,
        poll_seconds=0.01,
        once=True,
        warning_callback=warnings.append,
    )

    assert code == 0
    status = run_status(conn, run_id=run_id)
    assert status["error"] == 0
    assert status["queued"] == 0
    assert status["done"] == 2
    assert call_count >= 3

    tasks = list_tasks_for_run(conn, run_id=run_id)
    retried = next(task for task in tasks if task["attempts"] > 1)
    assert retried["status"] == "done"
    assert retried["error"] is None
    assert retried["attempts"] == 2
    assert retried["execution_attempts"] == 2

    throttle = get_run_throttle_state(conn, run_id)
    assert throttle is not None
    assert throttle.last_rate_limit_error is not None
    assert "429" in throttle.last_rate_limit_error
    assert warnings
    assert any("cooling for" in warning for warning in warnings)
    assert all("Retry context:" not in prompt or "429" not in prompt for prompt in captured_prompts)


def test_worker_loop_includes_previous_error_in_retry_prompt(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo_root = find_repo_root()
    pipelines = load_pipelines(repo_root / "pipelines")
    spec = pipelines["recipe.schemaorg.normalize.v1"]

    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    data_dir = tmp_path / "var"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)

    input_path = input_dir / "r1.json"
    input_path.write_text(json.dumps({"name": "Retry me"}), encoding="utf-8")

    conn = open_db(data_dir / "codex_farm.sqlite3")
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id=spec.pipeline_id,
        input_dir=str(input_dir),
        glob="**/*.json",
        output_dir=str(output_dir),
        config={"farm_root": str(repo_root)},
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[input_path],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=spec.output_ext,
    )

    captured_prompts: list[str] = []
    captured_usage_contexts: list[dict[str, object]] = []
    call_count = 0

    def fake_run_codex_exec(**kwargs):
        nonlocal call_count
        call_count += 1
        captured_prompts.append(str(kwargs["prompt"]))
        captured_usage_contexts.append(dict(kwargs["usage_context"]))
        if call_count == 1:
            return CodexExecResult(ok=False, exit_code=1, stderr_tail="first failure from codex")

        out = kwargs["output_path"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(_fake_recipe("Retry me")), encoding="utf-8")
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)

    code = worker_loop(
        data_dir=data_dir,
        worker_id="test-worker",
        run_id=run_id,
        lease_seconds=120,
        max_attempts=2,
        poll_seconds=0.01,
        once=True,
    )

    assert code == 0
    assert call_count == 2
    assert len(captured_prompts) == 2
    assert "Retry context:" not in captured_prompts[0]
    assert "Retry context:" in captured_prompts[1]
    assert "attempt 2" in captured_prompts[1]
    assert "codex exec failed (exit=1): first failure from codex" in captured_prompts[1]
    assert captured_usage_contexts[0]["attempt_index"] == 1
    assert captured_usage_contexts[0]["lease_claim_index"] == 1
    assert captured_usage_contexts[0]["execution_attempt_index"] == 1
    assert captured_usage_contexts[0]["retry_context_applied"] is False
    assert captured_usage_contexts[0]["retry_previous_error"] is None
    assert captured_usage_contexts[1]["attempt_index"] == 2
    assert captured_usage_contexts[1]["lease_claim_index"] == 2
    assert captured_usage_contexts[1]["execution_attempt_index"] == 2
    assert captured_usage_contexts[1]["retry_context_applied"] is True
    assert "codex exec failed (exit=1): first failure from codex" in str(
        captured_usage_contexts[1]["retry_previous_error"]
    )

    status = run_status(conn, run_id=run_id)
    assert status["done"] == 1
    assert status["error"] == 0
    tasks = list_tasks_for_run(conn, run_id=run_id)
    assert tasks[0]["execution_attempts"] == 2


def test_worker_loop_applies_heads_up_tips_and_scores_outcome(
    monkeypatch,
    tmp_path: Path,
) -> None:
    pack_root = tmp_path / "pack"
    pipeline_id = "demo.heads.up.worker.v1"
    _write_demo_pack(pack_root, pipeline_id=pipeline_id, codex_cd_mode="asset_root")
    pipelines = load_pipelines(pack_root / "pipelines")
    spec = pipelines[pipeline_id]

    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    data_dir = tmp_path / "var"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)

    input_path = input_dir / "one.json"
    input_path.write_text("{}", encoding="utf-8")

    conn = open_db(data_dir / "codex_farm.sqlite3")
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id=spec.pipeline_id,
        input_dir=str(input_dir.resolve()),
        glob="**/*.json",
        output_dir=str(output_dir.resolve()),
        config={
            "farm_root": str(pack_root.resolve()),
            "heads_up_enabled": True,
            "heads_up_max_tips": 2,
        },
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[input_path],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=spec.output_ext,
    )
    inserted = upsert_heads_up_tips(
        conn,
        pipeline_id=spec.pipeline_id,
        source_run_id=run_id,
        tips=[
            {
                "input_signature": "json_obj_keys:",
                "tip_text": "Return raw JSON only.",
            }
        ],
    )
    assert inserted == 1

    captured_prompts: list[str] = []
    captured_usage_contexts: list[dict[str, object]] = []

    def fake_run_codex_exec(**kwargs):
        captured_prompts.append(str(kwargs["prompt"]))
        captured_usage_contexts.append(dict(kwargs["usage_context"]))
        out = kwargs["output_path"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps({"ok": "OK", "source_path": str(input_path.resolve())}),
            encoding="utf-8",
        )
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)

    code = worker_loop(
        data_dir=data_dir,
        worker_id="test-worker",
        run_id=run_id,
        lease_seconds=120,
        max_attempts=3,
        poll_seconds=0.01,
        once=True,
    )

    assert code == 0
    assert len(captured_prompts) == 1
    assert "Heads up for this task:" in captured_prompts[0]
    assert "Return raw JSON only." in captured_prompts[0]
    assert captured_usage_contexts[0]["heads_up_applied"] is True
    assert captured_usage_contexts[0]["heads_up_tip_count"] == 1
    assert json.loads(str(captured_usage_contexts[0]["heads_up_tip_ids_json"]))
    assert json.loads(str(captured_usage_contexts[0]["heads_up_tip_texts_json"])) == [
        "Return raw JSON only."
    ]
    assert captured_usage_contexts[0]["retry_context_applied"] is False
    assert captured_usage_contexts[0]["attempt_index"] == 1

    rows = list_heads_up_tips(conn, pipeline_id=spec.pipeline_id)
    assert len(rows) == 1
    assert rows[0]["uses"] == 1
    assert rows[0]["wins"] == 1


def test_worker_loop_uses_output_schema_override_from_run_config(
    monkeypatch,
    tmp_path: Path,
) -> None:
    pack_root = tmp_path / "pack"
    pipeline_id = "demo.override.v1"
    _write_demo_pack(pack_root, pipeline_id=pipeline_id, codex_cd_mode="asset_root")
    pipelines = load_pipelines(pack_root / "pipelines")
    spec = pipelines[pipeline_id]

    input_dir = tmp_path / "input"
    output_dir = tmp_path / "out"
    data_dir = tmp_path / "var"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)

    input_path = input_dir / "sample.json"
    input_path.write_text("{}", encoding="utf-8")

    override_schema = tmp_path / "caller.schema.json"
    override_schema.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["ok", "source_path", "must_be_present"],
                "properties": {
                    "ok": {"type": "string"},
                    "source_path": {"type": "string"},
                    "must_be_present": {"type": "string"},
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    conn = open_db(data_dir / "codex_farm.sqlite3")
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id=spec.pipeline_id,
        input_dir=str(input_dir.resolve()),
        glob="**/*.json",
        output_dir=str(output_dir.resolve()),
        config={
            "farm_root": str(pack_root.resolve()),
            "output_schema_path_override": str(override_schema.resolve()),
        },
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[input_path],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=spec.output_ext,
    )

    captured_schema_paths: list[str] = []

    def fake_run_codex_exec(**kwargs):
        captured_schema_paths.append(str(kwargs["output_schema"]))
        out = kwargs["output_path"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "ok": "OK",
                    "source_path": str(input_path.resolve()),
                    "must_be_present": "yes",
                }
            ),
            encoding="utf-8",
        )
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)

    code = worker_loop(
        data_dir=data_dir,
        worker_id="test-worker",
        run_id=run_id,
        lease_seconds=120,
        max_attempts=3,
        poll_seconds=0.01,
        once=True,
    )

    assert code == 0
    status = run_status(conn, run_id=run_id)
    assert status["done"] == 1
    assert status["error"] == 0
    assert captured_schema_paths == [str(override_schema.resolve())]


def test_run_scoped_worker_waits_through_pause_and_continues_after_resume(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo_root = find_repo_root()
    pipelines = load_pipelines(repo_root / "pipelines")
    spec = pipelines["recipe.schemaorg.normalize.v1"]

    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    data_dir = tmp_path / "var"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    file_a = input_dir / "a.json"
    file_b = input_dir / "b.json"
    file_a.write_text(json.dumps({"name": "A"}), encoding="utf-8")
    file_b.write_text(json.dumps({"name": "B"}), encoding="utf-8")

    conn = open_db(data_dir / "codex_farm.sqlite3")
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id=spec.pipeline_id,
        input_dir=str(input_dir.resolve()),
        glob="**/*.json",
        output_dir=str(output_dir.resolve()),
        config={"farm_root": str(repo_root.resolve())},
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[file_a, file_b],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=spec.output_ext,
    )

    first_started = threading.Event()
    allow_first_to_finish = threading.Event()
    call_count = {"value": 0}

    def fake_run_codex_exec(**kwargs):
        call_count["value"] += 1
        if call_count["value"] == 1:
            first_started.set()
            allow_first_to_finish.wait(timeout=2.0)
        out = kwargs["output_path"]
        out.parent.mkdir(parents=True, exist_ok=True)
        name = Path(kwargs["prompt"].split("Input file path: ")[-1].strip().splitlines()[0]).stem
        out.write_text(json.dumps(_fake_recipe(name)), encoding="utf-8")
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)

    result_holder: dict[str, int] = {}

    def run_worker() -> None:
        result_holder["code"] = worker_loop(
            data_dir=data_dir,
            worker_id="pause-resume-worker",
            run_id=run_id,
            lease_seconds=120,
            max_attempts=3,
            poll_seconds=0.02,
            once=False,
            farm_root=repo_root,
        )

    thread = threading.Thread(target=run_worker, daemon=True)
    thread.start()

    assert first_started.wait(timeout=2.0), "first task never started"
    set_run_control_state(conn, run_id=run_id, control_state="paused")
    allow_first_to_finish.set()

    deadline = time.time() + 2.0
    while time.time() < deadline:
        status = run_status(conn, run_id=run_id)
        if status["status"] == "paused":
            break
        time.sleep(0.02)
    else:
        raise AssertionError(f"expected paused status; got {status}")

    assert call_count["value"] == 1
    time.sleep(0.2)
    assert call_count["value"] == 1, "paused run should not lease additional work"

    set_run_control_state(conn, run_id=run_id, control_state="active")
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if call_count["value"] >= 2:
            break
        time.sleep(0.02)
    assert call_count["value"] >= 2

    thread.join(timeout=3.0)
    assert not thread.is_alive(), "run-scoped worker should exit after terminal status"
    assert result_holder["code"] == 0

    final = run_status(conn, run_id=run_id)
    assert final["status"] == "done"
    assert final["done"] == 2


def test_worker_marks_retryable_failure_as_canceled_during_cancel_drain(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo_root = find_repo_root()
    pipelines = load_pipelines(repo_root / "pipelines")
    spec = pipelines["recipe.schemaorg.normalize.v1"]

    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    data_dir = tmp_path / "var"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    (input_dir / "one.json").write_text(json.dumps({"name": "one"}), encoding="utf-8")

    conn = open_db(data_dir / "codex_farm.sqlite3")
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id=spec.pipeline_id,
        input_dir=str(input_dir.resolve()),
        glob="**/*.json",
        output_dir=str(output_dir.resolve()),
        config={"farm_root": str(repo_root.resolve())},
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[input_dir / "one.json"],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=spec.output_ext,
    )

    def fake_run_codex_exec(**kwargs):
        control_conn = open_db(data_dir / "codex_farm.sqlite3")
        init_db(control_conn)
        set_run_control_state(control_conn, run_id=run_id, control_state="cancel_requested")
        return CodexExecResult(ok=False, exit_code=1, stderr_tail="retryable boom")

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)

    code = worker_loop(
        data_dir=data_dir,
        worker_id="cancel-drain-worker",
        run_id=run_id,
        lease_seconds=120,
        max_attempts=3,
        poll_seconds=0.01,
        once=True,
        farm_root=repo_root,
    )
    assert code == 0

    tasks = list_tasks_for_run(conn, run_id=run_id)
    assert len(tasks) == 1
    assert tasks[0]["status"] == "canceled"
    assert "retryable boom" in str(tasks[0]["error"])


def test_worker_stale_owner_does_not_delete_winner_output(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo_root = find_repo_root()
    pipelines = load_pipelines(repo_root / "pipelines")
    spec = pipelines["recipe.schemaorg.normalize.v1"]

    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    data_dir = tmp_path / "var"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    (input_dir / "one.json").write_text(json.dumps({"name": "stale"}), encoding="utf-8")

    conn = open_db(data_dir / "codex_farm.sqlite3")
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id=spec.pipeline_id,
        input_dir=str(input_dir.resolve()),
        glob="**/*.json",
        output_dir=str(output_dir.resolve()),
        config={"farm_root": str(repo_root.resolve())},
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[input_dir / "one.json"],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=spec.output_ext,
    )

    output_paths: list[Path] = []
    winner_output: Path | None = None

    def fake_run_codex_exec(**kwargs):
        nonlocal winner_output
        out = kwargs["output_path"]
        output_paths.append(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(_fake_recipe("stale")), encoding="utf-8")

        race_conn = open_db(data_dir / "codex_farm.sqlite3")
        init_db(race_conn)
        running = race_conn.execute(
            "SELECT task_id FROM tasks WHERE run_id = ? AND status = 'running'",
            (run_id,),
        ).fetchone()
        assert running is not None
        race_conn.execute(
            "UPDATE tasks SET lease_until = ? WHERE task_id = ?",
            (time.time() - 1, running["task_id"]),
        )
        race_conn.commit()
        reclaimed = lease_one_task(
            race_conn,
            worker_id="other-worker",
            lease_seconds=120,
            run_id=run_id,
        )
        assert reclaimed is not None
        winner_output = output_dir / str(reclaimed["rel_output_path"])
        winner_output.parent.mkdir(parents=True, exist_ok=True)
        winner_output.write_text(json.dumps(_fake_recipe("winner")), encoding="utf-8")
        assert mark_task_done(
            race_conn,
            task_id=str(reclaimed["task_id"]),
            lease_token=str(reclaimed["lease_token"]),
            output_path=str(winner_output),
        )
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)

    code = worker_loop(
        data_dir=data_dir,
        worker_id="stale-worker",
        run_id=run_id,
        lease_seconds=120,
        max_attempts=3,
        poll_seconds=0.01,
        once=True,
        farm_root=repo_root,
    )
    assert code in {0, 1}
    assert output_paths
    assert output_paths[0].exists() is False

    status = run_status(conn, run_id=run_id)
    assert status["done"] == 1
    assert winner_output is not None
    assert winner_output.exists()
    payload = json.loads(winner_output.read_text(encoding="utf-8"))
    assert payload["name"] == "winner"


def test_worker_preflight_failures_do_not_increment_execution_attempts(
    tmp_path: Path,
) -> None:
    repo_root = find_repo_root()
    pipelines = load_pipelines(repo_root / "pipelines")
    spec = pipelines["recipe.schemaorg.normalize.v1"]

    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    data_dir = tmp_path / "var"
    missing_workspace = tmp_path / "missing_workspace"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    input_path = input_dir / "one.json"
    input_path.write_text("{}", encoding="utf-8")

    conn = open_db(data_dir / "codex_farm.sqlite3")
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id=spec.pipeline_id,
        input_dir=str(input_dir.resolve()),
        glob="**/*.json",
        output_dir=str(output_dir.resolve()),
        config={
            "farm_root": str(repo_root.resolve()),
            "workspace_root": str(missing_workspace.resolve()),
        },
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[input_path],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=spec.output_ext,
    )

    code = worker_loop(
        data_dir=data_dir,
        worker_id="preflight-worker",
        run_id=run_id,
        lease_seconds=30,
        max_attempts=3,
        poll_seconds=0.01,
        once=True,
        farm_root=repo_root,
    )

    assert code == 1
    tasks = list_tasks_for_run(conn, run_id=run_id)
    assert len(tasks) == 1
    assert tasks[0]["status"] == "error"
    assert tasks[0]["attempts"] == 1
    assert tasks[0]["execution_attempts"] == 0


def test_worker_uses_frozen_prompt_after_template_edit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    pack_root = tmp_path / "pack"
    pipeline_id = "demo.frozen.prompt.v1"
    _write_demo_pack(pack_root, pipeline_id=pipeline_id, codex_cd_mode="asset_root")
    slug = pipeline_id.replace(".", "_")
    prompt_path = pack_root / "prompts" / f"{slug}.txt"
    prompt_path.write_text("ORIGINAL PROMPT {{INPUT_PATH}}\n", encoding="utf-8")
    spec = load_pipelines(pack_root / "pipelines")[pipeline_id]

    input_dir = tmp_path / "input"
    output_dir = tmp_path / "out"
    data_dir = tmp_path / "var"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    input_path = input_dir / "one.json"
    input_path.write_text("{}", encoding="utf-8")

    run_id = "frozen-prompt-run"
    frozen_assets = freeze_run_assets(
        run_id=run_id,
        data_dir=data_dir,
        pipeline=spec,
        resolved_model=spec.codex_model,
        resolved_reasoning_effort=spec.codex_reasoning_effort,
        resolved_output_schema_path=spec.output_schema_path,
    )

    conn = open_db(data_dir / "codex_farm.sqlite3")
    init_db(conn)
    create_run(
        conn,
        run_id=run_id,
        pipeline_id=spec.pipeline_id,
        input_dir=str(input_dir.resolve()),
        glob="**/*.json",
        output_dir=str(output_dir.resolve()),
        config={
            "farm_root": str(pack_root.resolve()),
            "frozen_assets": frozen_assets,
        },
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[input_path],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=spec.output_ext,
    )

    prompt_path.write_text("EDITED PROMPT {{INPUT_PATH}}\n", encoding="utf-8")
    captured_prompts: list[str] = []

    def fake_run_codex_exec(**kwargs):
        captured_prompts.append(str(kwargs["prompt"]))
        out = kwargs["output_path"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps({"ok": "OK", "source_path": str(input_path.resolve())}),
            encoding="utf-8",
        )
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)

    code = worker_loop(
        data_dir=data_dir,
        worker_id="worker-frozen-prompt",
        run_id=run_id,
        lease_seconds=120,
        max_attempts=3,
        poll_seconds=0.01,
        once=True,
    )

    assert code == 0
    assert len(captured_prompts) == 1
    assert "ORIGINAL PROMPT" in captured_prompts[0]
    assert "EDITED PROMPT" not in captured_prompts[0]


def test_worker_uses_frozen_schema_after_override_edit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    pack_root = tmp_path / "pack"
    pipeline_id = "demo.frozen.schema.v1"
    _write_demo_pack(pack_root, pipeline_id=pipeline_id, codex_cd_mode="asset_root")
    spec = load_pipelines(pack_root / "pipelines")[pipeline_id]

    input_dir = tmp_path / "input"
    output_dir = tmp_path / "out"
    data_dir = tmp_path / "var"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    input_path = input_dir / "one.json"
    input_path.write_text("{}", encoding="utf-8")

    override_schema = tmp_path / "override.schema.json"
    override_schema.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["ok", "source_path"],
                "properties": {
                    "ok": {"type": "string"},
                    "source_path": {"type": "string"},
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    run_id = "frozen-schema-run"
    frozen_assets = freeze_run_assets(
        run_id=run_id,
        data_dir=data_dir,
        pipeline=spec,
        resolved_model=spec.codex_model,
        resolved_reasoning_effort=spec.codex_reasoning_effort,
        resolved_output_schema_path=override_schema,
    )

    conn = open_db(data_dir / "codex_farm.sqlite3")
    init_db(conn)
    create_run(
        conn,
        run_id=run_id,
        pipeline_id=spec.pipeline_id,
        input_dir=str(input_dir.resolve()),
        glob="**/*.json",
        output_dir=str(output_dir.resolve()),
        config={
            "farm_root": str(pack_root.resolve()),
            "output_schema_path_override": str(override_schema.resolve()),
            "frozen_assets": frozen_assets,
        },
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[input_path],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=spec.output_ext,
    )

    override_schema.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["ok", "source_path", "must_be_present"],
                "properties": {
                    "ok": {"type": "string"},
                    "source_path": {"type": "string"},
                    "must_be_present": {"type": "string"},
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    captured_schema_paths: list[str] = []
    captured_logical_schema_paths: list[str] = []

    def fake_run_codex_exec(**kwargs):
        captured_schema_paths.append(str(kwargs["output_schema"]))
        captured_logical_schema_paths.append(str(kwargs["output_schema_logical_path"]))
        out = kwargs["output_path"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps({"ok": "OK", "source_path": str(input_path.resolve())}),
            encoding="utf-8",
        )
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)

    code = worker_loop(
        data_dir=data_dir,
        worker_id="worker-frozen-schema",
        run_id=run_id,
        lease_seconds=120,
        max_attempts=3,
        poll_seconds=0.01,
        once=True,
    )

    assert code == 0
    status = run_status(conn, run_id=run_id)
    assert status["done"] == 1
    assert status["error"] == 0
    assert len(captured_schema_paths) == 1
    assert captured_schema_paths[0] != str(override_schema.resolve())
    assert captured_logical_schema_paths == [str(override_schema.resolve())]


def test_worker_uses_frozen_pipeline_settings_after_pipeline_json_edit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    pack_root = tmp_path / "pack"
    pipeline_id = "demo.frozen.pipeline.v1"
    _write_demo_pack(pack_root, pipeline_id=pipeline_id, codex_cd_mode="asset_root")
    spec = load_pipelines(pack_root / "pipelines")[pipeline_id]

    input_dir = tmp_path / "input"
    output_dir = tmp_path / "out"
    data_dir = tmp_path / "var"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    input_path = input_dir / "one.json"
    input_path.write_text("{}", encoding="utf-8")

    run_id = "frozen-pipeline-run"
    frozen_assets = freeze_run_assets(
        run_id=run_id,
        data_dir=data_dir,
        pipeline=spec,
        resolved_model=spec.codex_model,
        resolved_reasoning_effort=spec.codex_reasoning_effort,
        resolved_output_schema_path=spec.output_schema_path,
    )

    conn = open_db(data_dir / "codex_farm.sqlite3")
    init_db(conn)
    create_run(
        conn,
        run_id=run_id,
        pipeline_id=spec.pipeline_id,
        input_dir=str(input_dir.resolve()),
        glob="**/*.json",
        output_dir=str(output_dir.resolve()),
        config={
            "farm_root": str(pack_root.resolve()),
            "frozen_assets": frozen_assets,
        },
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[input_path],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=spec.output_ext,
    )

    live_pipeline_path = pack_root / "pipelines" / f"{pipeline_id}.json"
    payload = json.loads(live_pipeline_path.read_text(encoding="utf-8"))
    payload["codex_model"] = "gpt-live-edited"
    payload["codex_cd_mode"] = "input_dir"
    live_pipeline_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    captured_models: list[str] = []
    captured_cd_dirs: list[str] = []

    def fake_run_codex_exec(**kwargs):
        captured_models.append(str(kwargs["model"]))
        captured_cd_dirs.append(str(kwargs["cd_dir"]))
        out = kwargs["output_path"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps({"ok": "OK", "source_path": str(input_path.resolve())}),
            encoding="utf-8",
        )
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)

    code = worker_loop(
        data_dir=data_dir,
        worker_id="worker-frozen-pipeline",
        run_id=run_id,
        lease_seconds=120,
        max_attempts=3,
        poll_seconds=0.01,
        once=True,
    )

    assert code == 0
    assert captured_models == [spec.codex_model]
    assert captured_cd_dirs == [str(pack_root.resolve())]


def test_worker_rejects_corrupt_frozen_assets_without_live_fallback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    pack_root = tmp_path / "pack"
    pipeline_id = "demo.frozen.corrupt.v1"
    _write_demo_pack(pack_root, pipeline_id=pipeline_id, codex_cd_mode="asset_root")
    spec = load_pipelines(pack_root / "pipelines")[pipeline_id]

    input_dir = tmp_path / "input"
    output_dir = tmp_path / "out"
    data_dir = tmp_path / "var"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    input_path = input_dir / "one.json"
    input_path.write_text("{}", encoding="utf-8")

    run_id = "frozen-corrupt-run"
    frozen_assets = freeze_run_assets(
        run_id=run_id,
        data_dir=data_dir,
        pipeline=spec,
        resolved_model=spec.codex_model,
        resolved_reasoning_effort=spec.codex_reasoning_effort,
        resolved_output_schema_path=spec.output_schema_path,
    )
    frozen_prompt = data_dir / "run_assets" / run_id / "prompt.template.txt"
    frozen_prompt.write_text("tampered prompt", encoding="utf-8")

    conn = open_db(data_dir / "codex_farm.sqlite3")
    init_db(conn)
    create_run(
        conn,
        run_id=run_id,
        pipeline_id=spec.pipeline_id,
        input_dir=str(input_dir.resolve()),
        glob="**/*.json",
        output_dir=str(output_dir.resolve()),
        config={
            "farm_root": str(pack_root.resolve()),
            "frozen_assets": frozen_assets,
        },
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[input_path],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=spec.output_ext,
    )

    codex_calls = 0

    def fake_run_codex_exec(**kwargs):
        nonlocal codex_calls
        codex_calls += 1
        raise AssertionError("worker should not call codex when frozen assets are invalid")

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)

    code = worker_loop(
        data_dir=data_dir,
        worker_id="worker-frozen-corrupt",
        run_id=run_id,
        lease_seconds=120,
        max_attempts=3,
        poll_seconds=0.01,
        once=True,
    )

    assert code == 1
    assert codex_calls == 0
    tasks = list_tasks_for_run(conn, run_id=run_id)
    assert len(tasks) == 1
    assert tasks[0]["status"] == "error"
    assert "requires frozen assets and cannot fall back to live pipeline files" in str(
        tasks[0]["error"]
    )


def test_older_run_without_frozen_assets_still_uses_live_pipeline(
    monkeypatch,
    tmp_path: Path,
) -> None:
    pack_root = tmp_path / "pack"
    pipeline_id = "demo.live.fallback.v1"
    _write_demo_pack(pack_root, pipeline_id=pipeline_id, codex_cd_mode="asset_root")
    slug = pipeline_id.replace(".", "_")
    prompt_path = pack_root / "prompts" / f"{slug}.txt"
    prompt_path.write_text("BEFORE {{INPUT_PATH}}\n", encoding="utf-8")
    spec = load_pipelines(pack_root / "pipelines")[pipeline_id]

    input_dir = tmp_path / "input"
    output_dir = tmp_path / "out"
    data_dir = tmp_path / "var"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    input_path = input_dir / "one.json"
    input_path.write_text("{}", encoding="utf-8")

    conn = open_db(data_dir / "codex_farm.sqlite3")
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id=spec.pipeline_id,
        input_dir=str(input_dir.resolve()),
        glob="**/*.json",
        output_dir=str(output_dir.resolve()),
        config={"farm_root": str(pack_root.resolve())},
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[input_path],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=spec.output_ext,
    )

    prompt_path.write_text("AFTER {{INPUT_PATH}}\n", encoding="utf-8")
    captured_prompts: list[str] = []

    def fake_run_codex_exec(**kwargs):
        captured_prompts.append(str(kwargs["prompt"]))
        out = kwargs["output_path"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps({"ok": "OK", "source_path": str(input_path.resolve())}),
            encoding="utf-8",
        )
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)

    code = worker_loop(
        data_dir=data_dir,
        worker_id="worker-live-fallback",
        run_id=run_id,
        lease_seconds=120,
        max_attempts=3,
        poll_seconds=0.01,
        once=True,
    )

    assert code == 0
    assert len(captured_prompts) == 1
    assert "AFTER" in captured_prompts[0]


def test_worker_loop_captures_forensics_before_cleanup_on_schema_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    pack_root = tmp_path / "pack"
    pipeline_id = "demo.forensics.worker.v1"
    _write_demo_pack(pack_root, pipeline_id=pipeline_id, codex_cd_mode="asset_root")
    pipelines = load_pipelines(pack_root / "pipelines")
    spec = pipelines[pipeline_id]

    input_dir = tmp_path / "input"
    output_dir = tmp_path / "out"
    data_dir = tmp_path / "var"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    input_path = input_dir / "bad.json"
    input_path.write_text("{}", encoding="utf-8")

    conn = open_db(data_dir / "codex_farm.sqlite3")
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id=spec.pipeline_id,
        input_dir=str(input_dir.resolve()),
        glob="**/*.json",
        output_dir=str(output_dir.resolve()),
        config={"farm_root": str(pack_root.resolve())},
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[input_path],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=spec.output_ext,
    )

    def fake_run_codex_exec(**kwargs):
        out = kwargs["output_path"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"ok": "OK"}), encoding="utf-8")
        return CodexExecResult(
            ok=True,
            exit_code=0,
            stderr_tail="schema mismatch",
            stdout_tail="stdout trace",
        )

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)

    code = worker_loop(
        data_dir=data_dir,
        worker_id="worker-forensics",
        run_id=run_id,
        lease_seconds=120,
        max_attempts=1,
        poll_seconds=0.01,
        once=True,
    )

    assert code == 1
    assert not (output_dir / "bad.json").exists()

    rows = list_failure_forensics(conn, run_id=run_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["task_id"]
    assert row["attempt_index"] == 1
    assert row["failure_stage"] == "schema_validation"
    assert row["failure_category"] == "schema_validation"
    assert row["terminal"] is True

    metadata_path = Path(str(row["metadata_path"]))
    raw_output_path = Path(str(row["raw_output_path"]))
    assert metadata_path.exists()
    assert raw_output_path.exists()
    assert json.loads(raw_output_path.read_text(encoding="utf-8")) == {"ok": "OK"}
