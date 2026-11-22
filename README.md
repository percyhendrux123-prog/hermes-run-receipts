# run-receipts for Hermes Agent

> **Part of the seat**, a collection of Hermes plugins that work together: watch, decide, act, prove, hand off. This one covers **watch**: each scheduled job the agent runs leaves a receipt. [See the whole collection](https://github.com/percyhendrux123-prog#the-seat-a-collection-of-hermes-plugins).

A silent cron run and a cron run that never finished its check look the same from the outside. The usual way to tell them apart is the job's own output, which is the job describing itself.

run-receipts is a small plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent). When it sees an **agent-backed cron run** end, it writes a receipt separate from job output: which job ran, when, which tool calls were observed, which were flagged as errors, and how the run was classified. You can then ask "did the watcher call any tools before going silent?"

It came out of the discussion on NousResearch/hermes-agent#158289, where reviewers pointed out that "completion" is read off the job's own markers.

## What a receipt tells you

The plugin writes receipts from hook events, not from the model's description of what it did. Tool errors are those reported by Hermes or recognized by the plugin's fallback checks when Hermes supplies no status.

`silent_checked` means at least one call was observed and none were flagged as errors. That is not proof of a successful check: a watcher can call `terminal` once and still check the wrong thing or read stale data. Receipts are not tamper-proof against anyone with write access to the state directory.

## Outcomes

| outcome | meaning |
|---|---|
| `delivered` | A non-silent agent response, not verified transport delivery. |
| `silent_checked` | Silent response, at least one observed tool call, no flagged tool errors. |
| `silent_unchecked` | Silent response, no observed tool calls. |
| `silent_with_errors` | Silent response, and at least one observed tool call was flagged as an error. |
| `failed` | The agent reported failure or was interrupted, the run did not complete, the response starts with the `[CRON_FAILURE]` line, or the final response was empty (including Hermes's `empty_response_exhausted` exit, even if it supplied placeholder text). A short `cause` says which: `agent_failed`, `interrupted`, `not_completed`, `failure_marker`, `empty_response`. |
| `incomplete` | A run started and no end was ever seen. Found the next time the same job starts; the receipt has no `ended_at`, and the counts are as of the last tool call. `cause` is `no_end_seen`. |

"Silent" uses Hermes's matcher when available, otherwise a local copy. It recognizes a marker as the whole reply or on its own first or last nonblank line, including localized markers such as `静默`. A bracketed prefix such as `[SILENT] no changes` also counts; a marker buried mid-sentence does not.

A nonblank max-iteration summary is an exception to the `not_completed` rule: the plugin classifies the response normally instead of treating `completed=false` alone as failure. Explicit failure or interruption still takes precedence.

## What it records

One JSON line per run in `receipts.jsonl`:

```json
{"v":1,"job_id":"daily-watch","session_id":"cron_daily-watch_20261121_090000","profile":"default",
 "started_at":"2026-11-21T15:00:00Z","ended_at":"2026-11-21T15:00:12Z","duration_s":12.4,
 "tool_calls":3,"tool_errors":0,"tools":["terminal","web_search"],
 "tool_stats":{"terminal":{"calls":2,"errors":0},"web_search":{"calls":1,"errors":0}},
 "outcome":"silent_checked"}
```

- Metadata only by default. No tool arguments, tool results or prompts are recorded. Tool names that aren't identifier-shaped are stored as `unknown`.
- `store_reason: true` opts in to keeping up to 200 characters of the final response on `silent_*` outcomes, with whitespace collapsed. This is potentially sensitive response text, not a redaction. It stays in the local receipt file: neither `/receipts` nor `run_receipts_query` displays it, even for rows from older releases. Turning the option off does not scrub old rows; they remain on disk until removed or pruned.
- Writes use 0600 files inside a 0700 directory and tighten existing looser modes. Reads and writes refuse symlinked state directories or receipt files. Queries never create files or change modes.
- No network, no subprocesses, no credentials, no telemetry.

## Install and configure

```sh
hermes plugins install <this repo>     # or copy this directory to ~/.hermes/plugins/run-receipts
hermes plugins enable run-receipts
```

Settings (under `plugins.entries.run-receipts.settings` in config.yaml, all optional):

| key | default | meaning |
|---|---|---|
| `state_dir` | `<HERMES_HOME>/run-receipts` | where `receipts.jsonl` lives (per profile by default) |
| `keep_days` | `30` | receipts older than this are removed whenever a new one is written |
| `store_reason` | `false` | opt in to storing silent response snippets in the local file only |

Requires Hermes >= 0.22.5 (checked against source at 0.23.3). Uses `fcntl`, so macOS and Linux only.

## Reading receipts back

In a session: `/receipts last [n]` and `/receipts suspicious [n]` default to 10 rows and clamp `n` to 1–200. `/receipts job <id>` shows up to 50 rows. Results are newest first by receipt order.

`suspicious` lists `silent_unchecked`, `silent_with_errors`, `incomplete` and `failed` from the last 7 days. A malformed outcome does not hide valid matches.

Where the toolset is enabled, the read-only `run_receipts_query` returns receipt metadata as JSON (`action`: `last`, `job` with `job_id`, or `suspicious`). All actions accept optional `n` (1–200, default 50 for `job` and 10 otherwise). The tool can't write or delete anything. The plugin does not authenticate callers as operators.

## How it works

It registers four observer hooks: `on_session_start`, `post_tool_call`, `post_llm_call` and `on_session_end`. A session counts as a cron run when its platform is `cron` (that is how `cron/scheduler.py` builds the agent). Interactive sessions and delegated subagents (`platform="subagent"`) are ignored and nothing is written for them. The job id comes from Hermes's cron execution identity (`ctx.current_cron_execution()`), else from the `cron_<job>_<timestamp>` session id.

Every hook catches its own exceptions, logs a warning and returns `None`. It never changes what is delivered and never adds or removes output. A storage failure can leave a missing receipt or a checkpoint to recover, but does not stop the job.

Finalization and checkpoint cleanup share a writer lock. While the receipt is retained, the first complete receipt for a session wins. If cleanup fails after append, later runs continue and retry cleanup without emitting a second receipt. An already-final receipt can outlive `keep_days` until its leftover checkpoint can be removed. A complete receipt missing only its final newline is readable and kept; the next append or cleanup restores the newline. A torn fragment or a row that cannot be written as UTF-8 is discarded. Short writes are completed before the checkpoint is removed.

## Not covered

- **Script-mode (`no_agent`) cron jobs.** `cron/scheduler.py` runs these as a bare subprocess (`_run_no_agent_job`): no agent, no session, so none of the session or tool hooks fire. Hermes exposes no plugin hook or event for them (`ctx.current_cron_execution()` is only a getter, and the `hermes:` event namespace has no cron events in the source I read). Many crons run this way, so most of them get no receipt. If Hermes later adds a cron-run hook, this plugin can use it.
- **The script step of an agent job.** A job that has both a `script` and an agent run is covered for the agent part. The pre-run script's own exit status isn't recorded, and a `wakeAgent=false` skip never starts an agent, so it leaves no receipt.
- **Runs skipped before an agent starts**: monitor-gate "unchanged" ticks, prompt-scanner blocks, config or preflight failures. No session starts, so nothing is written.
- **Tools run by delegated subagents.** Their calls are not added to the parent's counts.
- **Tool errors Hermes doesn't flag.** Error counting uses the status Hermes derives from each result, plus the result's top-level JSON shape if no status is given. A tool that reports a problem in plain text, or one that fails inside an otherwise successful payload, counts as OK.
- **Jobs with no derivable id.** Receipts are still written (job id is `null`), but `incomplete` can only be found by pairing a start with the next start of the *same* job, so those runs can't be flagged.
- **Overlapping runs of the same job.** If a job starts while an earlier run of it is still going, the earlier run is marked `incomplete` and its late end writes nothing. Hermes normally prevents overlapping runs of one job.
- **Jobs that never run again.** An unfinished run only becomes `incomplete` when the same job next starts. Its open-run record is dropped after `keep_days`.
- **Sessions in a different state dir.** Profiles with different `state_dir` settings keep separate files; `/receipts` reads only the current profile's.
- **No transport confirmation.** This revision's tests simulate hook events; they do not verify receipt of a real cron notification.

## Tests

```sh
python3 -m unittest discover -s tests
```

Stdlib only, no network, no Hermes install. Set `HERMES_AGENT_SRC` to a source checkout to compare the fallback against its gateway matcher. That optional test does not exercise the scheduler emitter. See `docs/EVIDENCE.md` for a `TMPDIR` example and the test scope.

## License

MIT
