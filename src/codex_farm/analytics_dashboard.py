"""Build static dashboard artifacts from codex exec telemetry CSV."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import math
from pathlib import Path


DASHBOARD_SCHEMA_VERSION = 1


@dataclass
class _Bucket:
    calls: int = 0
    ok: int = 0
    failed: int = 0
    timeout: int = 0
    other: int = 0
    duration_ms_total: int = 0
    duration_samples: list[int] = field(default_factory=list)
    tokens_total: int = 0
    output_bytes: int = 0


@dataclass(frozen=True)
class DashboardBuildResult:
    index_path: Path
    data_path: Path
    js_path: Path
    css_path: Path
    row_count: int
    warnings: tuple[str, ...]


def build_stats_dashboard(
    *,
    csv_path: Path,
    out_dir: Path,
    recent_limit: int = 250,
) -> DashboardBuildResult:
    rows, warnings = _read_csv_rows(csv_path)
    payload = _build_payload(
        rows=rows,
        csv_path=csv_path,
        warnings=warnings,
        recent_limit=recent_limit,
    )
    return _write_bundle(payload=payload, out_dir=out_dir)


def _read_csv_rows(csv_path: Path) -> tuple[list[dict[str, str]], list[str]]:
    resolved = csv_path.expanduser().resolve()
    warnings: list[str] = []
    if not resolved.exists():
        warnings.append(f"Telemetry CSV does not exist: {resolved}")
        return [], warnings
    if not resolved.is_file():
        warnings.append(f"Telemetry CSV path is not a file: {resolved}")
        return [], warnings

    try:
        with resolved.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = [dict(row) for row in reader if row]
    except OSError as exc:
        warnings.append(f"Could not read telemetry CSV ({resolved}): {exc}")
        return [], warnings

    return rows, warnings


def _build_payload(
    *,
    rows: list[dict[str, str]],
    csv_path: Path,
    warnings: list[str],
    recent_limit: int,
) -> dict[str, object]:
    normalized: list[dict[str, object]] = []
    parse_timestamp_failures = 0

    for row in rows:
        normalized_row = _normalize_row(row)
        normalized.append(normalized_row)
        raw_ts = str(normalized_row["logged_at_utc"])
        if raw_ts and normalized_row["logged_at"] is None:
            parse_timestamp_failures += 1

    status_counts = {"ok": 0, "failed": 0, "timeout": 0, "other": 0}
    duration_values: list[int] = []
    duration_ms_total = 0
    tokens_input_total = 0
    tokens_cached_input_total = 0
    tokens_output_total = 0
    tokens_reasoning_total = 0
    tokens_total = 0
    output_bytes_total = 0
    accepted_nonzero_exit_count = 0
    output_payload_rows = 0
    unique_runs: set[str] = set()
    unique_tasks: set[str] = set()
    unique_threads: set[str] = set()

    source_buckets: dict[str, _Bucket] = {}
    pipeline_buckets: dict[str, _Bucket] = {}
    model_buckets: dict[str, _Bucket] = {}
    daily_buckets: dict[str, _Bucket] = {}

    for row in normalized:
        status = str(row["status"])
        status_counts[status] = status_counts.get(status, 0) + 1

        duration_ms = int(row["duration_ms"])
        duration_ms_total += duration_ms
        duration_values.append(duration_ms)

        row_tokens_input = int(row["tokens_input"])
        row_tokens_cached_input = int(row["tokens_cached_input"])
        row_tokens_output = int(row["tokens_output"])
        row_tokens_reasoning = int(row["tokens_reasoning"])
        row_tokens_total = int(row["tokens_total"])
        row_output_bytes = int(row["output_bytes"])

        tokens_input_total += row_tokens_input
        tokens_cached_input_total += row_tokens_cached_input
        tokens_output_total += row_tokens_output
        tokens_reasoning_total += row_tokens_reasoning
        tokens_total += row_tokens_total
        output_bytes_total += row_output_bytes

        if bool(row["accepted_nonzero_exit"]):
            accepted_nonzero_exit_count += 1
        if bool(row["output_payload_present"]):
            output_payload_rows += 1

        run_id = str(row["run_id"])
        if run_id:
            unique_runs.add(run_id)
        task_id = str(row["task_id"])
        if task_id:
            unique_tasks.add(task_id)
        thread_id = str(row["thread_id"])
        if thread_id:
            unique_threads.add(thread_id)

        _update_bucket(
            source_buckets.setdefault(str(row["source"]), _Bucket()),
            status=status,
            duration_ms=duration_ms,
            tokens_total=row_tokens_total,
            output_bytes=row_output_bytes,
        )
        _update_bucket(
            pipeline_buckets.setdefault(str(row["pipeline_id"]), _Bucket()),
            status=status,
            duration_ms=duration_ms,
            tokens_total=row_tokens_total,
            output_bytes=row_output_bytes,
        )
        _update_bucket(
            model_buckets.setdefault(str(row["model"]), _Bucket()),
            status=status,
            duration_ms=duration_ms,
            tokens_total=row_tokens_total,
            output_bytes=row_output_bytes,
        )

        logged_at = row["logged_at"]
        if isinstance(logged_at, datetime):
            day_key = logged_at.astimezone(UTC).strftime("%Y-%m-%d")
            _update_bucket(
                daily_buckets.setdefault(day_key, _Bucket()),
                status=status,
                duration_ms=duration_ms,
                tokens_total=row_tokens_total,
                output_bytes=row_output_bytes,
            )

    if parse_timestamp_failures:
        warnings.append(
            f"{parse_timestamp_failures} row(s) had unparseable logged_at_utc timestamps."
        )

    total_calls = len(normalized)
    success_rate_pct = _ratio_pct(status_counts.get("ok", 0), total_calls)
    average_duration = int(round(duration_ms_total / total_calls)) if total_calls else 0
    latest_logged = _latest_logged_at(normalized)

    recent_events = _recent_events(normalized=normalized, limit=recent_limit)

    payload = {
        "schema_version": DASHBOARD_SCHEMA_VERSION,
        "generated_at_utc": _utc_ts(datetime.now(UTC)),
        "source_csv_path": str(csv_path.expanduser().resolve()),
        "row_count": total_calls,
        "latest_logged_at_utc": latest_logged,
        "warnings": warnings,
        "summary": {
            "total_calls": total_calls,
            "status_counts": status_counts,
            "success_rate_pct": success_rate_pct,
            "accepted_nonzero_exit_count": accepted_nonzero_exit_count,
            "output_payload_rows": output_payload_rows,
            "unique_runs": len(unique_runs),
            "unique_tasks": len(unique_tasks),
            "unique_threads": len(unique_threads),
        },
        "duration": {
            "total_ms": duration_ms_total,
            "avg_ms": average_duration,
            "p50_ms": _percentile(duration_values, 50),
            "p95_ms": _percentile(duration_values, 95),
            "max_ms": max(duration_values) if duration_values else None,
        },
        "tokens": {
            "input": tokens_input_total,
            "cached_input": tokens_cached_input_total,
            "output": tokens_output_total,
            "reasoning": tokens_reasoning_total,
            "total": tokens_total,
        },
        "bytes": {
            "output_total": output_bytes_total,
            "avg_output_per_call": int(round(output_bytes_total / total_calls))
            if total_calls
            else 0,
        },
        "source_breakdown": _serialize_breakdown(source_buckets, label_key="source"),
        "pipeline_breakdown": _serialize_breakdown(
            pipeline_buckets,
            label_key="pipeline_id",
        ),
        "model_breakdown": _serialize_breakdown(model_buckets, label_key="model"),
        "daily": _serialize_daily(daily_buckets),
        "recent_events": recent_events,
    }
    return payload


def _normalize_row(raw: dict[str, str]) -> dict[str, object]:
    status = _normalize_status(raw.get("status", ""))
    logged_at_utc = _clean_text(raw.get("logged_at_utc", ""))
    parsed_logged_at = _parse_ts(logged_at_utc)
    tokens_input = _as_int(raw.get("tokens_input", "")) or 0
    tokens_cached_input = _as_int(raw.get("tokens_cached_input", "")) or 0
    tokens_output = _as_int(raw.get("tokens_output", "")) or 0
    tokens_reasoning = _as_int(raw.get("tokens_reasoning", "")) or 0
    tokens_total_raw = _as_int(raw.get("tokens_total", ""))
    if tokens_total_raw is None:
        tokens_total = tokens_input + tokens_output
    else:
        tokens_total = tokens_total_raw

    return {
        "logged_at_utc": logged_at_utc,
        "logged_at": parsed_logged_at,
        "duration_ms": _as_int(raw.get("duration_ms", "")) or 0,
        "status": status,
        "exit_code": _as_int(raw.get("exit_code", "")),
        "accepted_nonzero_exit": _as_bool(raw.get("accepted_nonzero_exit", "")),
        "output_payload_present": _as_bool(raw.get("output_payload_present", "")),
        "output_bytes": _as_int(raw.get("output_bytes", "")) or 0,
        "tokens_input": tokens_input,
        "tokens_cached_input": tokens_cached_input,
        "tokens_output": tokens_output,
        "tokens_reasoning": tokens_reasoning,
        "tokens_total": tokens_total,
        "source": _label(raw.get("source", ""), fallback="(unknown)"),
        "pipeline_id": _label(raw.get("pipeline_id", ""), fallback="(none)"),
        "run_id": _clean_text(raw.get("run_id", "")),
        "task_id": _clean_text(raw.get("task_id", "")),
        "worker_id": _clean_text(raw.get("worker_id", "")),
        "model": _label(raw.get("model", ""), fallback="(unknown)"),
        "sandbox": _clean_text(raw.get("sandbox", "")),
        "thread_id": _clean_text(raw.get("thread_id", "")),
        "prompt_chars": _as_int(raw.get("prompt_chars", "")) or 0,
        "prompt_sha256": _clean_text(raw.get("prompt_sha256", "")),
        "input_path": _clean_text(raw.get("input_path", "")),
        "output_path": _clean_text(raw.get("output_path", "")),
        "stderr_tail": _trim_text(raw.get("stderr_tail", ""), limit=180),
        "stdout_tail": _trim_text(raw.get("stdout_tail", ""), limit=180),
    }


def _update_bucket(
    bucket: _Bucket,
    *,
    status: str,
    duration_ms: int,
    tokens_total: int,
    output_bytes: int,
) -> None:
    bucket.calls += 1
    if status == "ok":
        bucket.ok += 1
    elif status == "failed":
        bucket.failed += 1
    elif status == "timeout":
        bucket.timeout += 1
    else:
        bucket.other += 1
    bucket.duration_ms_total += duration_ms
    bucket.duration_samples.append(duration_ms)
    bucket.tokens_total += tokens_total
    bucket.output_bytes += output_bytes


def _serialize_breakdown(
    buckets: dict[str, _Bucket],
    *,
    label_key: str,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for label, bucket in buckets.items():
        avg_duration = int(round(bucket.duration_ms_total / bucket.calls)) if bucket.calls else 0
        rows.append(
            {
                label_key: label,
                "calls": bucket.calls,
                "ok": bucket.ok,
                "failed": bucket.failed,
                "timeout": bucket.timeout,
                "other": bucket.other,
                "success_rate_pct": _ratio_pct(bucket.ok, bucket.calls),
                "avg_duration_ms": avg_duration,
                "p95_duration_ms": _percentile(bucket.duration_samples, 95),
                "tokens_total": bucket.tokens_total,
                "output_bytes": bucket.output_bytes,
            }
        )
    return sorted(rows, key=lambda item: (-int(item["calls"]), str(item[label_key])))


def _serialize_daily(daily_buckets: dict[str, _Bucket]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for day in sorted(daily_buckets):
        bucket = daily_buckets[day]
        rows.append(
            {
                "date": day,
                "calls": bucket.calls,
                "ok": bucket.ok,
                "failed": bucket.failed,
                "timeout": bucket.timeout,
                "other": bucket.other,
                "avg_duration_ms": int(round(bucket.duration_ms_total / bucket.calls))
                if bucket.calls
                else 0,
                "tokens_total": bucket.tokens_total,
                "output_bytes": bucket.output_bytes,
            }
        )
    return rows


def _recent_events(*, normalized: list[dict[str, object]], limit: int) -> list[dict[str, object]]:
    decorated: list[tuple[float, int, dict[str, object]]] = []
    for idx, row in enumerate(normalized):
        logged_at = row["logged_at"]
        if isinstance(logged_at, datetime):
            sort_ts = logged_at.timestamp()
        else:
            sort_ts = float("-inf")
        decorated.append((sort_ts, idx, row))

    decorated.sort(key=lambda item: (item[0], item[1]), reverse=True)

    selected: list[dict[str, object]] = []
    for _sort_ts, _idx, row in decorated[: max(limit, 1)]:
        selected.append(
            {
                "logged_at_utc": row["logged_at_utc"],
                "status": row["status"],
                "source": row["source"],
                "pipeline_id": row["pipeline_id"],
                "run_id": row["run_id"],
                "task_id": row["task_id"],
                "worker_id": row["worker_id"],
                "model": row["model"],
                "sandbox": row["sandbox"],
                "duration_ms": row["duration_ms"],
                "tokens_total": row["tokens_total"],
                "tokens_reasoning": row["tokens_reasoning"],
                "output_bytes": row["output_bytes"],
                "prompt_chars": row["prompt_chars"],
                "exit_code": row["exit_code"],
                "accepted_nonzero_exit": row["accepted_nonzero_exit"],
                "output_payload_present": row["output_payload_present"],
                "thread_id": row["thread_id"],
                "prompt_sha256": row["prompt_sha256"],
                "input_path": row["input_path"],
                "output_path": row["output_path"],
                "stderr_tail": row["stderr_tail"],
                "stdout_tail": row["stdout_tail"],
            }
        )
    return selected


def _latest_logged_at(normalized: list[dict[str, object]]) -> str | None:
    latest: datetime | None = None
    for row in normalized:
        logged_at = row["logged_at"]
        if not isinstance(logged_at, datetime):
            continue
        if latest is None or logged_at > latest:
            latest = logged_at
    return _utc_ts(latest) if latest is not None else None


def _percentile(values: list[int], percentile: int) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, math.ceil((percentile / 100) * len(ordered)) - 1)
    return ordered[min(rank, len(ordered) - 1)]


def _ratio_pct(part: int, whole: int) -> float:
    if whole <= 0:
        return 0.0
    return round((part / whole) * 100, 1)


def _normalize_status(value: str) -> str:
    lowered = _clean_text(value).lower()
    if lowered in {"ok", "failed", "timeout"}:
        return lowered
    return "other"


def _parse_ts(value: str) -> datetime | None:
    raw = _clean_text(value)
    if not raw:
        return None

    normalized = raw
    if raw.endswith("Z"):
        normalized = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d_%H.%M.%S", "%Y-%m-%d-%H-%M-%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _utc_ts(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _label(value: object, *, fallback: str) -> str:
    cleaned = _clean_text(value)
    return cleaned if cleaned else fallback


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        return str(value).strip()
    return value.strip()


def _trim_text(value: object, *, limit: int) -> str:
    cleaned = _clean_text(value)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."


def _as_bool(value: object) -> bool:
    lowered = _clean_text(value).lower()
    return lowered in {"1", "true", "yes", "y"}


def _as_int(value: object) -> int | None:
    raw = _clean_text(value)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _write_bundle(*, payload: dict[str, object], out_dir: Path) -> DashboardBuildResult:
    output_dir = out_dir.expanduser().resolve()
    assets_dir = output_dir / "assets"
    output_dir.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)

    data_path = assets_dir / "dashboard_data.json"
    js_path = assets_dir / "dashboard.js"
    css_path = assets_dir / "style.css"
    index_path = output_dir / "index.html"

    data_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    js_path.write_text(_DASHBOARD_JS, encoding="utf-8")
    css_path.write_text(_DASHBOARD_CSS, encoding="utf-8")

    inline_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    html = _DASHBOARD_HTML.replace("__INLINE_DASHBOARD_DATA__", inline_json)
    index_path.write_text(html, encoding="utf-8")

    warnings = payload.get("warnings", [])
    row_count = int(payload.get("row_count", 0))
    warning_tuple = tuple(item for item in warnings if isinstance(item, str))
    return DashboardBuildResult(
        index_path=index_path,
        data_path=data_path,
        js_path=js_path,
        css_path=css_path,
        row_count=row_count,
        warnings=warning_tuple,
    )


_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>codex-farm analytics dashboard</title>
    <link rel="stylesheet" href="assets/style.css" />
  </head>
  <body>
    <div class="bg-orb orb-one"></div>
    <div class="bg-orb orb-two"></div>

    <main class="shell">
      <header class="hero">
        <p class="eyebrow">codex-farm</p>
        <h1>Codex Exec Activity Dashboard</h1>
        <p class="subhead">
          Static analytics snapshot from <code id="csv-path">-</code>.
          Generated <span id="generated-at">-</span>.
        </p>
      </header>

      <section class="cards" id="summary-cards"></section>

      <section class="panel">
        <div class="panel-head">
          <h2>Status Mix</h2>
          <p id="status-caption">-</p>
        </div>
        <div id="status-strip" class="status-strip"></div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h2>Recent Duration Trace</h2>
          <p id="trace-caption">-</p>
        </div>
        <div id="duration-trace" class="trace"></div>
      </section>

      <section class="grid grid-two">
        <article class="panel">
          <div class="panel-head">
            <h2>By Source</h2>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Source</th>
                  <th>Calls</th>
                  <th>Success</th>
                  <th>Avg ms</th>
                  <th>P95 ms</th>
                  <th>Tokens</th>
                </tr>
              </thead>
              <tbody id="source-rows"></tbody>
            </table>
          </div>
        </article>

        <article class="panel">
          <div class="panel-head">
            <h2>By Pipeline</h2>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Pipeline</th>
                  <th>Calls</th>
                  <th>Success</th>
                  <th>Avg ms</th>
                  <th>P95 ms</th>
                  <th>Tokens</th>
                </tr>
              </thead>
              <tbody id="pipeline-rows"></tbody>
            </table>
          </div>
        </article>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h2>Daily Trend</h2>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Date</th>
                <th>Calls</th>
                <th>Success</th>
                <th>Avg ms</th>
                <th>Total Tokens</th>
              </tr>
            </thead>
            <tbody id="daily-rows"></tbody>
          </table>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h2>Recent Events</h2>
          <p id="event-count-caption">-</p>
        </div>
        <div class="filters">
          <label>
            Status
            <select id="status-filter"></select>
          </label>
          <label>
            Source
            <select id="source-filter"></select>
          </label>
          <label>
            Pipeline
            <select id="pipeline-filter"></select>
          </label>
          <label>
            Search
            <input id="search-filter" type="search" placeholder="run/task/input..." />
          </label>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Status</th>
                <th>Source</th>
                <th>Pipeline</th>
                <th>Run</th>
                <th>Task</th>
                <th>ms</th>
                <th>Tokens</th>
                <th>Bytes</th>
              </tr>
            </thead>
            <tbody id="event-rows"></tbody>
          </table>
        </div>
      </section>

      <section class="panel warnings-panel" id="warnings-panel" hidden>
        <div class="panel-head">
          <h2>Warnings</h2>
        </div>
        <ul id="warning-rows"></ul>
      </section>
    </main>

    <script id="dashboard-data" type="application/json">__INLINE_DASHBOARD_DATA__</script>
    <script src="assets/dashboard.js"></script>
  </body>
</html>
"""


_DASHBOARD_CSS = """\
:root {
  --ink: #1b1f18;
  --ink-dim: #4b5647;
  --bg: #f0f5ee;
  --panel: #f8fbf5;
  --accent: #1f6f66;
  --accent-warm: #c95f18;
  --line: #d3dece;
  --ok: #2f8f4f;
  --failed: #bf3131;
  --timeout: #a6631b;
  --other: #46536a;
}

* {
  box-sizing: border-box;
}

body {
  margin: 0;
  color: var(--ink);
  font-family: "Trebuchet MS", "Gill Sans", "Tahoma", sans-serif;
  background:
    radial-gradient(1200px 500px at -10% -20%, #cde7d8 0%, transparent 65%),
    radial-gradient(1100px 560px at 110% -15%, #f4dcc8 0%, transparent 62%),
    linear-gradient(180deg, #edf3ea 0%, #f5f8f2 100%);
  min-height: 100vh;
}

.bg-orb {
  position: fixed;
  width: 34rem;
  height: 34rem;
  border-radius: 999px;
  filter: blur(70px);
  opacity: 0.2;
  z-index: -2;
}

.orb-one {
  background: #2e8f7f;
  left: -12rem;
  top: 8rem;
}

.orb-two {
  background: #cc7336;
  right: -10rem;
  bottom: -12rem;
}

.shell {
  width: min(1200px, 94vw);
  margin: 2rem auto 3rem;
  display: grid;
  gap: 1rem;
}

.hero {
  background: linear-gradient(120deg, #e6f2e7, #f7eee7);
  border: 1px solid #d5e0d0;
  border-radius: 1.1rem;
  padding: 1.4rem 1.5rem;
}

.hero h1 {
  margin: 0.1rem 0 0.55rem;
  font-size: clamp(1.5rem, 4vw, 2.3rem);
  letter-spacing: 0.01em;
}

.eyebrow {
  margin: 0;
  text-transform: uppercase;
  letter-spacing: 0.2em;
  font-size: 0.74rem;
  color: var(--ink-dim);
}

.subhead {
  margin: 0;
  color: var(--ink-dim);
}

.cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
  gap: 0.7rem;
}

.card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 1rem;
  padding: 0.85rem 0.9rem;
}

.card-label {
  margin: 0;
  color: var(--ink-dim);
  font-size: 0.78rem;
  text-transform: uppercase;
  letter-spacing: 0.08em;
}

.card-value {
  margin: 0.35rem 0 0;
  font-size: 1.35rem;
  font-weight: 700;
}

.panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 1rem;
  padding: 0.95rem;
}

.panel-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 0.8rem;
  margin-bottom: 0.7rem;
}

.panel-head h2 {
  margin: 0;
  font-size: 1rem;
}

.panel-head p {
  margin: 0;
  color: var(--ink-dim);
  font-size: 0.86rem;
}

.status-strip {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 0.5rem;
}

.status-chip {
  border: 1px solid var(--line);
  border-left: 0.4rem solid transparent;
  border-radius: 0.65rem;
  background: #fbfdf8;
  padding: 0.45rem 0.6rem;
}

.status-chip strong {
  display: block;
  font-size: 1rem;
}

.status-chip.ok {
  border-left-color: var(--ok);
}

.status-chip.failed {
  border-left-color: var(--failed);
}

.status-chip.timeout {
  border-left-color: var(--timeout);
}

.status-chip.other {
  border-left-color: var(--other);
}

.trace {
  min-height: 140px;
}

.trace svg {
  width: 100%;
  height: 160px;
  display: block;
}

.trace .sparkline {
  fill: none;
  stroke: var(--accent);
  stroke-width: 2.5;
}

.trace .sparkline-bg {
  fill: none;
  stroke: #b8d6cc;
  stroke-width: 1.2;
  stroke-dasharray: 4 3;
}

.grid {
  display: grid;
  gap: 0.8rem;
}

.grid-two {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.table-wrap {
  overflow-x: auto;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.9rem;
}

th,
td {
  border-bottom: 1px solid #dce6d8;
  text-align: left;
  padding: 0.46rem 0.45rem;
  white-space: nowrap;
}

thead th {
  font-size: 0.76rem;
  letter-spacing: 0.07em;
  text-transform: uppercase;
  color: #5f6c5c;
}

.filters {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 0.65rem;
  margin-bottom: 0.7rem;
}

.filters label {
  display: grid;
  gap: 0.26rem;
  font-size: 0.79rem;
  color: var(--ink-dim);
}

.filters select,
.filters input {
  border: 1px solid #c8d5c3;
  border-radius: 0.5rem;
  padding: 0.44rem 0.52rem;
  background: #fcfefb;
  color: var(--ink);
  font: inherit;
}

.warnings-panel ul {
  margin: 0;
  padding-left: 1.1rem;
}

code {
  font-family: "JetBrains Mono", "Consolas", "Courier New", monospace;
  background: #edf3eb;
  padding: 0.08rem 0.2rem;
  border-radius: 0.3rem;
}

@media (max-width: 900px) {
  .grid-two {
    grid-template-columns: 1fr;
  }
}
"""


_DASHBOARD_JS = """\
(function () {
  "use strict";

  const state = {
    payload: null,
    events: [],
    filteredEvents: [],
  };

  function setText(id, value) {
    const element = document.getElementById(id);
    if (!element) {
      return;
    }
    element.textContent = value ?? "";
  }

  function formatInt(value) {
    const numeric = Number.isFinite(value) ? value : Number(value || 0);
    return new Intl.NumberFormat("en-US").format(Math.round(numeric));
  }

  function formatPercent(value) {
    const numeric = Number.isFinite(value) ? value : Number(value || 0);
    return numeric.toFixed(1) + "%";
  }

  function readInlinePayload() {
    const node = document.getElementById("dashboard-data");
    if (!node || !node.textContent) {
      return null;
    }
    try {
      return JSON.parse(node.textContent);
    } catch (_error) {
      return null;
    }
  }

  async function loadPayload() {
    const inline = readInlinePayload();
    if (inline) {
      return inline;
    }
    try {
      const response = await fetch("assets/dashboard_data.json");
      if (!response.ok) {
        return null;
      }
      return await response.json();
    } catch (_error) {
      return null;
    }
  }

  function renderCards(payload) {
    const summary = payload.summary || {};
    const duration = payload.duration || {};
    const tokens = payload.tokens || {};

    const cards = [
      ["Calls", formatInt(summary.total_calls || 0)],
      ["Success Rate", formatPercent(summary.success_rate_pct || 0)],
      ["Total Tokens", formatInt(tokens.total || 0)],
      ["Reasoning Tokens", formatInt(tokens.reasoning || 0)],
      ["Avg Duration", formatInt(duration.avg_ms || 0) + " ms"],
      ["P95 Duration", formatInt(duration.p95_ms || 0) + " ms"],
      ["Unique Runs", formatInt(summary.unique_runs || 0)],
    ];

    const container = document.getElementById("summary-cards");
    if (!container) {
      return;
    }
    container.innerHTML = "";
    for (const [label, value] of cards) {
      const card = document.createElement("article");
      card.className = "card";
      const labelNode = document.createElement("p");
      labelNode.className = "card-label";
      labelNode.textContent = label;
      const valueNode = document.createElement("p");
      valueNode.className = "card-value";
      valueNode.textContent = value;
      card.appendChild(labelNode);
      card.appendChild(valueNode);
      container.appendChild(card);
    }
  }

  function renderStatusStrip(payload) {
    const statusCounts = (payload.summary && payload.summary.status_counts) || {};
    const total = Number(payload.row_count || 0);
    const order = ["ok", "failed", "timeout", "other"];
    const strip = document.getElementById("status-strip");
    if (!strip) {
      return;
    }
    strip.innerHTML = "";
    for (const key of order) {
      const value = Number(statusCounts[key] || 0);
      const chip = document.createElement("article");
      chip.className = "status-chip " + key;
      const label = document.createElement("span");
      label.textContent = key;
      const count = document.createElement("strong");
      const share = total > 0 ? ((value / total) * 100).toFixed(1) : "0.0";
      count.textContent = formatInt(value) + " (" + share + "%)";
      chip.appendChild(label);
      chip.appendChild(count);
      strip.appendChild(chip);
    }
    setText("status-caption", formatInt(total) + " rows in telemetry CSV");
  }

  function renderTrace(events) {
    const container = document.getElementById("duration-trace");
    if (!container) {
      return;
    }
    if (!events.length) {
      container.innerHTML = "<p>No events available yet.</p>";
      setText("trace-caption", "No duration samples.");
      return;
    }

    const points = events
      .slice(0, 120)
      .map((event) => Number(event.duration_ms || 0))
      .reverse();
    const maxValue = Math.max(...points, 1);
    const width = 960;
    const height = 160;
    const left = 10;
    const right = 10;
    const top = 10;
    const bottom = 18;
    const drawableWidth = width - left - right;
    const drawableHeight = height - top - bottom;
    const xStep = points.length > 1 ? drawableWidth / (points.length - 1) : drawableWidth;

    const pathParts = [];
    points.forEach((value, index) => {
      const x = left + index * xStep;
      const y = top + drawableHeight - (value / maxValue) * drawableHeight;
      pathParts.push((index === 0 ? "M " : "L ") + x.toFixed(2) + " " + y.toFixed(2));
    });

    const baseline = top + drawableHeight;
    container.innerHTML =
      "<svg viewBox='0 0 " + width + " " + height + "' role='img' aria-label='duration trace'>" +
      "<path class='sparkline-bg' d='M " + left + " " + baseline + " L " + (width - right) + " " + baseline + "'/>" +
      "<path class='sparkline' d='" + pathParts.join(" ") + "'/></svg>";

    setText(
      "trace-caption",
      "Last " + formatInt(points.length) + " calls, max " + formatInt(maxValue) + " ms"
    );
  }

  function renderBreakdownRows(elementId, rows, labelKey) {
    const body = document.getElementById(elementId);
    if (!body) {
      return;
    }
    body.innerHTML = "";
    if (!rows || !rows.length) {
      const row = document.createElement("tr");
      row.innerHTML = "<td colspan='6'>No data</td>";
      body.appendChild(row);
      return;
    }

    rows.forEach((entry) => {
      const tr = document.createElement("tr");
      tr.innerHTML =
        "<td>" + escapeHtml(entry[labelKey]) + "</td>" +
        "<td>" + formatInt(entry.calls || 0) + "</td>" +
        "<td>" + formatPercent(entry.success_rate_pct || 0) + "</td>" +
        "<td>" + formatInt(entry.avg_duration_ms || 0) + "</td>" +
        "<td>" + formatInt(entry.p95_duration_ms || 0) + "</td>" +
        "<td>" + formatInt(entry.tokens_total || 0) + "</td>";
      body.appendChild(tr);
    });
  }

  function renderDailyRows(rows) {
    const body = document.getElementById("daily-rows");
    if (!body) {
      return;
    }
    body.innerHTML = "";
    if (!rows || !rows.length) {
      const tr = document.createElement("tr");
      tr.innerHTML = "<td colspan='5'>No dated rows available.</td>";
      body.appendChild(tr);
      return;
    }
    rows.forEach((entry) => {
      const tr = document.createElement("tr");
      tr.innerHTML =
        "<td>" + escapeHtml(entry.date) + "</td>" +
        "<td>" + formatInt(entry.calls || 0) + "</td>" +
        "<td>" + formatPercent(entry.calls ? ((entry.ok || 0) / entry.calls) * 100 : 0) + "</td>" +
        "<td>" + formatInt(entry.avg_duration_ms || 0) + "</td>" +
        "<td>" + formatInt(entry.tokens_total || 0) + "</td>";
      body.appendChild(tr);
    });
  }

  function uniqueValues(events, key) {
    const values = new Set();
    events.forEach((event) => {
      const value = String(event[key] || "");
      if (value) {
        values.add(value);
      }
    });
    return [...values].sort();
  }

  function populateFilter(selectId, options) {
    const select = document.getElementById(selectId);
    if (!select) {
      return;
    }
    select.innerHTML = "";
    const any = document.createElement("option");
    any.value = "";
    any.textContent = "All";
    select.appendChild(any);
    options.forEach((value) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      select.appendChild(option);
    });
  }

  function applyEventFilters() {
    const statusFilter = (document.getElementById("status-filter") || {}).value || "";
    const sourceFilter = (document.getElementById("source-filter") || {}).value || "";
    const pipelineFilter = (document.getElementById("pipeline-filter") || {}).value || "";
    const searchFilter = ((document.getElementById("search-filter") || {}).value || "")
      .trim()
      .toLowerCase();

    state.filteredEvents = state.events.filter((event) => {
      if (statusFilter && event.status !== statusFilter) {
        return false;
      }
      if (sourceFilter && event.source !== sourceFilter) {
        return false;
      }
      if (pipelineFilter && event.pipeline_id !== pipelineFilter) {
        return false;
      }
      if (searchFilter) {
        const haystack = [
          event.run_id,
          event.task_id,
          event.worker_id,
          event.input_path,
          event.output_path,
        ]
          .join(" ")
          .toLowerCase();
        if (!haystack.includes(searchFilter)) {
          return false;
        }
      }
      return true;
    });

    renderEventRows();
  }

  function renderEventRows() {
    const body = document.getElementById("event-rows");
    if (!body) {
      return;
    }
    const events = state.filteredEvents;
    body.innerHTML = "";
    if (!events.length) {
      const tr = document.createElement("tr");
      tr.innerHTML = "<td colspan='9'>No events match filters.</td>";
      body.appendChild(tr);
      setText("event-count-caption", "0 matching rows");
      return;
    }

    events.slice(0, 250).forEach((event) => {
      const tr = document.createElement("tr");
      tr.innerHTML =
        "<td>" + escapeHtml(event.logged_at_utc || "-") + "</td>" +
        "<td>" + escapeHtml(event.status || "-") + "</td>" +
        "<td>" + escapeHtml(event.source || "-") + "</td>" +
        "<td>" + escapeHtml(event.pipeline_id || "-") + "</td>" +
        "<td>" + escapeHtml(event.run_id || "-") + "</td>" +
        "<td>" + escapeHtml(event.task_id || "-") + "</td>" +
        "<td>" + formatInt(event.duration_ms || 0) + "</td>" +
        "<td>" + formatInt(event.tokens_total || 0) + "</td>" +
        "<td>" + formatInt(event.output_bytes || 0) + "</td>";
      body.appendChild(tr);
    });

    setText(
      "event-count-caption",
      formatInt(events.length) + " matching rows (showing first 250)"
    );
  }

  function renderWarnings(warnings) {
    const panel = document.getElementById("warnings-panel");
    const list = document.getElementById("warning-rows");
    if (!panel || !list) {
      return;
    }
    list.innerHTML = "";
    if (!warnings || !warnings.length) {
      panel.hidden = true;
      return;
    }
    panel.hidden = false;
    warnings.forEach((warning) => {
      const li = document.createElement("li");
      li.textContent = warning;
      list.appendChild(li);
    });
  }

  function wireFilters() {
    ["status-filter", "source-filter", "pipeline-filter", "search-filter"].forEach((id) => {
      const input = document.getElementById(id);
      if (!input) {
        return;
      }
      input.addEventListener("change", applyEventFilters);
      input.addEventListener("input", applyEventFilters);
    });
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function render(payload) {
    state.payload = payload;
    state.events = Array.isArray(payload.recent_events) ? payload.recent_events : [];
    state.filteredEvents = state.events.slice();

    setText("csv-path", payload.source_csv_path || "-");
    setText("generated-at", payload.generated_at_utc || "-");

    renderCards(payload);
    renderStatusStrip(payload);
    renderTrace(state.events);
    renderBreakdownRows("source-rows", payload.source_breakdown || [], "source");
    renderBreakdownRows("pipeline-rows", payload.pipeline_breakdown || [], "pipeline_id");
    renderDailyRows(payload.daily || []);
    renderWarnings(payload.warnings || []);

    populateFilter("status-filter", uniqueValues(state.events, "status"));
    populateFilter("source-filter", uniqueValues(state.events, "source"));
    populateFilter("pipeline-filter", uniqueValues(state.events, "pipeline_id"));
    wireFilters();
    applyEventFilters();
  }

  loadPayload().then((payload) => {
    if (!payload) {
      setText("generated-at", "failed to load dashboard data");
      return;
    }
    render(payload);
  });
})();
"""
