I read the report. I think it is directionally right about one thing and too pessimistic about another.

Your report correctly established that historical local artifacts under `CODEX_HOME/sessions/...` can contain explicit reasoning items with human-readable summaries plus encrypted content, while your authenticated standalone probe produced no reasoning on stdout and ended with repeated recorder failures on stderr. 

What the web search changes is the implied contract. Current Codex docs and the official repo README say `codex exec` persists rollout files by default, and that `--ephemeral` is the mode that runs **without** persisting session rollout files. The CLI reference also documents `codex exec resume [SESSION_ID]`, and an OpenAI maintainer explicitly tells users to inspect rollout JSONL files under `~/.codex/sessions` to see what a session pulled into context. That makes rollout persistence the intended baseline for non-ephemeral `exec`, not an accidental side effect. ([OpenAI Developers][1])

So the most likely fix is **not** “find a hidden stdout reasoning flag.” The most likely fix is: treat your current `~/.codex-alt` as unhealthy. In your own probe, Codex logged multiple `Permission denied` errors under that home before it started emitting `failed to record rollout items: failed to queue rollout items: channel closed`. Given the documented default persistence behavior, that pattern looks much more like a broken or unwritable `CODEX_HOME` / recorder path than proof that standalone `codex exec` generally does not persist rollout artifacts.  ([OpenAI Developers][1])

There is a second, separate problem: `exec --json` itself is a weak reasoning surface. The official config reference says `hide_agent_reasoning` suppresses reasoning events in `codex exec`, while `model_reasoning_summary`, `model_supports_reasoning_summaries`, and `show_raw_agent_reasoning` control whether reasoning metadata is requested or surfaced. The Responses API docs also explicitly document `reasoning.encrypted_content`, so the richer reasoning payload is expected to be encrypted rather than plainly harvestable text. ([OpenAI Developers][2])

That leads to a cleaner diagnosis:

1. **Rollout/session persistence is supposed to exist** for normal `codex exec` runs.
2. **Stdout JSON is not a trustworthy reasoning export surface**.
3. **Your local home or recorder path appears broken**, which is why you failed to observe the expected persisted artifact in that specific probe.
4. **Full reasoning will still not be plain text**, even when the artifact exists, because the richer payload is designed around encrypted reasoning content. ([OpenAI Developers][1]) 

What I would change in CodexFarm:

* **Use rollout files as the primary rich trace surface; treat stdout as a status stream.** An official repo issue notes that `exec --json` lacks data that is present in the session file, including reasoning token counts, and that request was closed as not planned. ([GitHub][3])

* **Search the filesystem directly by `thread_id` before trusting SQLite.** Another official issue shows `codex exec resume` can report `state db missing rollout path` even while the rollout JSONL file is present on disk. So CodexFarm should parse `thread.started.thread_id`, then glob `CODEX_HOME/sessions/**/rollout-*<thread_id>*.jsonl`; only use SQLite as a secondary hint. ([GitHub][4])

* **Force reasoning-related config explicitly on the command line.** Because Codex loads project-scoped `.codex/config.toml` files from the repo root down to the working directory, your `--cd` flow can silently inherit overrides from the target repo. I would add explicit overrides such as `hide_agent_reasoning=false`, `model_reasoning_summary="concise"` or `"detailed"`, and `model_supports_reasoning_summaries=true` on diagnostic runs. Keep `show_raw_agent_reasoning=true` only for a separate debug mode, because if it works it may expose more than you want to persist. ([OpenAI Developers][5])

* **Classify absence instead of guessing.** The result states should distinguish at least: `ephemeral`, `config_suppressed`, `rollout_recorder_failed`, `rollout_missing`, `rollout_present_summary_only`, `rollout_present_encrypted_only`, and `stdout_only`. That keeps CodexFarm truthful instead of fabricating reasoning counts or implying the model “didn’t reason.” The gap between stdout and rollout data is real and appears intentional enough that I would not expect it to disappear soon. ([GitHub][3])

One more thing to test: auth mode. The official README documents both ChatGPT-sign-in and API-key setups, and an open Codex issue reports that some API-key-auth configurations emit no reasoning items in `--json` even when other users on the same CLI version see them. I would treat that as a plausible explanation for missing stdout reasoning, but not for missing rollout files. ([GitHub][6])

So the report’s final conclusion is the part I would rewrite. I would replace it with this:

> Current `codex exec` is **intended** to persist rollout JSONL for non-ephemeral runs, but `exec --json` is not a reliable reasoning export surface. In my environment, missing rollout artifacts are likely caused by local `CODEX_HOME` / recorder failures rather than by the absence of any upstream session surface. CodexFarm should therefore harvest reasoning summaries opportunistically from rollout files, search the filesystem directly by session/thread id, and classify missing reasoning as config/auth/recorder failure instead of concluding that standalone `exec` has no harvestable reasoning surface. ([OpenAI Developers][1]) 

If you want, I can turn that into a concrete patch plan for `src/codex_farm/codex_exec.py`.

[1]: https://developers.openai.com/codex/cli/reference/ "Command line options"
[2]: https://developers.openai.com/codex/config-reference/ "https://developers.openai.com/codex/config-reference/"
[3]: https://github.com/openai/codex/issues/5276 "Add reasoning token usage on json output · Issue #5276 · openai/codex · GitHub"
[4]: https://github.com/openai/codex/issues/11634 "Error: \"state db missing rollout path\" received when running codex exec resume <SESSION_ID> \"...\" · Issue #11634 · openai/codex · GitHub"
[5]: https://developers.openai.com/codex/config-advanced/ "https://developers.openai.com/codex/config-advanced/"
[6]: https://github.com/openai/codex "https://github.com/openai/codex"
