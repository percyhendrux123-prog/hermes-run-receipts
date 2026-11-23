---
description: Read back run receipts, the private per-session log of outcome and tool counts that the run-receipts plugin writes. Use when the user asks whether a session or headless run actually called tools, or asks to see recent or suspicious receipts.
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/receipts_query.py *)
---

Query the receipts read-only by running exactly one command, then report what it returns.

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/receipts_query.py <action> [arguments]
```

Actions:

- `last [n]`: the most recent receipts (default 10).
- `suspicious [n]`: receipts from the last 7 days with outcome `completed_with_errors`, `failed`, `no_turn` or `incomplete` (default 10).
- `session <session-id> [n]`: receipts for one session id (default 50).
- `outcome <outcome> [n]`: receipts with one outcome (default 10). Outcomes: `completed`, `completed_no_tools`, `completed_with_errors`, `failed`, `no_turn`, `incomplete`.

`n` is clamped to 1 to 200. Output is JSON, newest first. Map the user's request to one action; with no argument, use `last`. The request was: $ARGUMENTS

When you report back:

- Say what each outcome means in plain words and keep to what the receipt shows: counts of observed tool calls and reported errors. A receipt does not show that the work was correct.
- Do not run any other command, and do not edit or delete receipt files. This skill only reads.
- If the command prints an `error`, show it and stop.
