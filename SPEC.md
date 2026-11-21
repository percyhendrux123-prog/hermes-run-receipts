# SPEC: hermes-run-receipts

## Why
On PR NousResearch/hermes-agent#158289 reviewers (yqecea, nickhechtel, zambodotdev) note that a silent cron run can't be told apart from a run that never finished its check: "completion" is read off the job's own markers, which are the job's claim about itself. Each run should leave a small receipt written outside the job's own output.

## What it does
1. **Detects cron runs.** Read hermes-ref/cron/scheduler.py (how a cron run builds its agent/session id and platform), agent/conversation_loop.py (on_session_start kwargs), agent/turn_finalizer.py (on_session_end kwargs). Only act inside cron runs (platform == "cron" or the cron session-id convention found in the source). Interactive sessions: do nothing.
2. **Counts inputs.** `post_tool_call` during a cron run: per tool name, count calls, count errored results (tool result contains an error shape, see hermes-ref/tools for the common error JSON), and record the set of tool names. Store no arguments or results.
3. **Writes a receipt at run end** (on_session_end / the final response via post_llm_call or transform_llm_output observer returning None): job id (if derivable), session id, started_at, ended_at, duration_s, tool_calls total, tool_errors, tools used, outcome one of:
   - `delivered` (non-silent agent response; not transport confirmation),
   - `silent_checked` (silent marker AND >=1 tool call AND 0 tool errors),
   - `silent_unchecked` (silent marker AND 0 tool calls),
   - `silent_with_errors` (silent marker AND tool errors > 0),
   - `failed` (failure marker or exception / interrupted),
   - `incomplete` (session started, no end seen — detected on the next start of the same job).
   Silent detection must reuse the upstream rule (hermes-ref/cron/scheduler.py SILENT_MARKER and its matcher); import it at runtime guarded by try/except, with a copied fallback.
   Append as one JSON line to `<state_dir>/receipts.jsonl` (0600). Only when explicitly enabled, include a short `reason` (up to 200 chars of the final response on silent_* outcomes); never return it through either query surface, including from old rows.
4. **Reads back.** Slash command `/receipts` with `last [n]`, `job <id>`, `suspicious` (silent_unchecked, silent_with_errors, incomplete, failed in the last 7 days). A tool `run_receipts_query` (read-only) so the operator agent can check "did the watcher actually check?".
5. **Config:** `state_dir`, `keep_days` (default 30, prune on write), `store_reason` (default false, local file only).
6. Fail-open; never alter delivery, never suppress or add output.

## Acceptance
- Unit tests for each outcome class with simulated hook sequences, interactive sessions ignored, incomplete detection, pruning, 0600 file, exception inside a hook does not raise, query tool returns the right rows.
- README in plain English: a receipt counts observed calls and reported or recognized errors; it proves neither job correctness nor transport delivery.
- docs/EVIDENCE.md with test counts.
