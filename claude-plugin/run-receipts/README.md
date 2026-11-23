# Run Receipts for Claude Code

A silent session and a session that never finished its check look the same from the outside. The usual way to tell them apart is the agent's own summary, which is the agent describing itself.

Run Receipts is a small Claude Code plugin. When a session ends, including a headless `claude -p` run, it writes one receipt to a private file, separate from anything the session printed: when it ran, how many tool calls were observed, how many were reported as failed, and how the run ended. You can then ask "did that scheduled run call any tools before it said it was done?"

It is the Claude Code counterpart of [run-receipts for Hermes Agent](https://github.com/percyhendrux123-prog/hermes-run-receipts), and it keeps the same rules: it only observes, it fails open, and it records counts and names, never content.

## What a receipt tells you

Receipts are written from Claude Code's hook events, not from the model's description of what it did. A receipt counts observed tool calls and the ones Claude Code reported as failed. It proves neither that the work was correct nor that anything was delivered anywhere.

`completed` means the last turn finished normally, at least one tool call was observed, and none were reported as failed. That is not proof of a good check: an agent can run `date` once and still look at the wrong thing.

Receipts are not tamper-proof against anyone who can write to the state directory.

## Outcomes

| outcome | meaning |
|---|---|
| `completed` | The last turn finished, at least one tool call was observed, none reported as failed. |
| `completed_no_tools` | The last turn finished and no tool calls were observed. |
| `completed_with_errors` | The last turn finished and at least one tool call was reported as failed. |
| `failed` | The last turn ended in a Claude Code `StopFailure` (an API error), or finished with an empty final response. `cause` says which: an error type such as `rate_limit` or `server_error`, or `empty_response`. |
| `no_turn` | The session ended without a finished turn, for example you exited at the prompt, or an interrupt ended the only turn. `cause` is `no_turn_completed`. |
| `incomplete` | A session started and no end was ever seen (the process was killed, or the end hook did not finish). Found the next time a session starts after the log has been quiet for 12 hours. The receipt has no `ended_at`, and counts are as of the last tool call. `cause` is `no_end_seen`. |

A session can have several turns. Counts cover the whole session. The outcome follows the last turn that finished or failed.

## What it records

One JSON line per session in `receipts.jsonl`:

```json
{"v":1,"session_id":"dd0badec-57c1-4d36-98ed-de4de0096d34","source":"startup",
 "started_at":"2026-11-23T15:19:53Z","ended_at":"2026-11-23T15:19:54Z","duration_s":1.4,
 "end_reason":"other","tool_calls":2,"tool_errors":0,"tools":["Bash"],
 "tool_stats":{"Bash":{"calls":2,"errors":0}},"outcome":"completed"}
```

- Metadata only. No tool arguments, tool results, prompts, assistant text, file paths or working directories are stored. The hook receives them from Claude Code but does not use them, except that it checks whether the final response was empty and keeps only that yes or no.
- Tool names must be identifier-shaped (1 to 80 ASCII letters, digits or `_.:/-`, starting with a letter, digit or underscore). Anything else is stored as `unknown`. Do not put sensitive data in tool names.
- Failure causes come from the fixed list of `StopFailure` error types in the hooks reference. Anything else is stored as `unknown`.
- A session id is stored only if it is filename-safe (letters, digits, `_` and `-`, up to 80 characters).
- There is no option to store response text. The Hermes plugin has an opt-in snippet mode; this version does not.
- Files are 0600 inside a 0700 directory, and looser existing modes are tightened on write. Symlinked state directories, open logs and receipt files are refused. Queries never create files or change modes.
- No network, no subprocesses, no credentials, no telemetry.

## Before you install

- **Files it writes:** `run-receipts/receipts.jsonl` and a small `run-receipts/open/` log per running session, under `$CLAUDE_CONFIG_DIR` if set, otherwise `~/.claude`. Set `RUN_RECEIPTS_DIR` to use a different directory (`~` is expanded). An open log is deleted when its receipt is written.
- **Files it reads:** only its own state files, and the hook event on standard input.
- **Network, background processes, credentials, telemetry:** none. Each hook is one short Python process using only the standard library.
- **Requires:** `python3` on your PATH, macOS or Linux (it uses `fcntl` and `O_NOFOLLOW`; on Windows it will fail open and write nothing). Tested against Claude Code 2.1.325.
- **Cost to the session:** one small Python process per tool call, per turn, and at session start and end. The `SessionEnd` hook has a 1.5 second budget in Claude Code; the work it does is a single small read and append.

Optional environment variables:

| name | default | meaning |
|---|---|---|
| `RUN_RECEIPTS_DIR` | `<config dir>/run-receipts` | where receipts live |
| `RUN_RECEIPTS_KEEP_DAYS` | `30` | receipts older than this are removed whenever a new one is written |
| `RUN_RECEIPTS_STALE_HOURS` | `12` | how long an open log may be quiet before the next session start closes it as `incomplete` |

## Install

```
claude plugin marketplace add percyhendrux123-prog/hermes-run-receipts
claude plugin install axiom-run-receipts@deploy-axiom-run-receipts
```

To try it for one run without installing:

```
claude --plugin-dir ./claude-plugin/run-receipts
```

## Reading receipts back

In a session, or in `claude -p`:

```
/axiom-run-receipts:receipts last 5
/axiom-run-receipts:receipts suspicious
/axiom-run-receipts:receipts session <session-id>
/axiom-run-receipts:receipts outcome completed_no_tools
```

The command is a skill that runs one read-only script, `scripts/receipts_query.py`, and Claude reports the result. `last` and `suspicious` default to 10 rows, `session` to 50, and `n` is clamped to 1 to 200. Results are newest first by receipt order. `suspicious` lists `completed_with_errors`, `failed`, `no_turn` and `incomplete` from the last 7 days. `completed_no_tools` is not in that list, because a plain answer with no tools is normal in many sessions; ask for it with `outcome`.

For exact output with no model in between, run the script yourself:

```
python3 claude-plugin/run-receipts/scripts/receipts_query.py suspicious 20
```

It prints JSON, can't write or delete anything, and only ever returns the receipt fields listed above, even if someone edited the file. The query session is itself a session, so it leaves a receipt of its own.

## How it works

It registers command hooks for six events: `SessionStart`, `PostToolUse`, `PostToolUseFailure`, `Stop`, `StopFailure` and `SessionEnd`. At session start it opens a small event log. Each tool event adds one line (a tool name and whether it failed). Each `Stop` or `StopFailure` adds one line for the turn. At `SessionEnd` the log is turned into one receipt and removed. That is why this is hooks and scripts rather than a mod or a skill alone: the job is passive observation of events that already exist.

Hook activity from subagents is ignored, so a subagent's tool calls are not added to the main session's counts. The parent's call to the `Agent` tool is counted like any other.

Every hook catches its own exceptions, prints nothing and exits 0. It never blocks, approves, rewrites or adds output. A storage failure can leave a missing receipt or an open log that is later closed as `incomplete`. Finalizing a receipt takes a file lock, and the first complete receipt for a session wins.

## Known gaps

These are differences from the Hermes plugin and limits of what Claude Code reports. I did not test anything not listed under "Tested" below.

- **Every session gets a receipt, not only scheduled ones.** Hermes writes receipts only for cron runs and ignores interactive sessions. The hook events Claude Code sends do not say whether a session is headless, so this plugin records interactive sessions too. There is no job id; the session id is the key.
- **No "silent" outcomes.** Hermes classifies a cron run by its silent marker. Claude Code has no such marker, so the outcomes above are different: they describe how the last turn ended and whether tools were called. The `completed_no_tools` outcome is the nearest to Hermes's `silent_unchecked`.
- **`incomplete` is found by age, not by pairing.** Hermes finds an unfinished run when the same job starts again. Here, any session start closes open logs that have been quiet for 12 hours (`RUN_RECEIPTS_STALE_HOURS`). A session left idle longer than that and then resumed is closed as `incomplete`, and its later end writes nothing. A session killed with no later session start stays open (and is removed after `RUN_RECEIPTS_KEEP_DAYS`).
- **Interrupted turns.** `Stop` does not fire when you interrupt a turn, so an interrupted turn leaves no trace of its own. If an earlier turn finished, the outcome follows that one. If none did, it is `no_turn`. Tool calls made in the interrupted turn are still counted.
- **Tool errors Claude Code does not flag.** A tool error is a `PostToolUseFailure` event. A tool that reports a problem inside a successful result counts as OK. A call refused by a permission rule, or rejected before it ran (unknown tool, bad input), fires neither event and is not counted as a call. A cancelled running tool does not fire the failure hook either.
- **Plugin enabled mid-session.** A session that started before the plugin loaded has no open log, so its end writes nothing.
- **Subagent tool calls** are not counted, as noted above.
- **Managed hook policy.** If an administrator sets `allowManagedHooksOnly`, plugin hooks do not run and nothing is written. Separately, the Claude Code plugin directory refuses plugins with command hooks or scripts unless the publisher is verified, so this installs from GitHub only.
- **No response-snippet option** (Hermes `store_reason`), and no `/receipts` slash command of that exact name. The query is the namespaced skill above.
- **No transport confirmation.** A receipt says nothing about whether any notification or output reached anyone.

### Tested

Unit tests drive the real hook and query scripts with sample events built from the hooks reference. One live check ran headless `claude -p` runs against Claude Code 2.1.325 in a throwaway config directory with this plugin installed from a local checkout: `SessionStart`, `PostToolUse`, `PostToolUseFailure`, `Stop` and `SessionEnd` all fired in `-p` mode (`StopFailure` was not triggered live), and receipts were written for a run with tool calls, a run with none, and a run with a failed tool call; the skill read them back. Interactive terminal sessions, resumed sessions, cloud sessions and Windows were not run live.

## Try it with no install

```
python3 -m unittest discover -s claude-plugin/tests
```

The tests use a temporary directory and touch nothing in `~/.claude`.

## Undo

Run `claude plugin uninstall axiom-run-receipts` and, if you added the marketplace, `claude plugin marketplace remove deploy-axiom-run-receipts`. Delete `~/.claude/run-receipts/` (or your `RUN_RECEIPTS_DIR`) if you no longer want the receipts. Receipts are only removed by the plugin when they pass `RUN_RECEIPTS_KEEP_DAYS`.

## How it was made

I designed it, set the rules for what it may record, and tested it on my own system. AI agents wrote the code under my spec and review. I read Python; I don't claim to write it by hand.

Percy Keith, Deploy Axiom. MIT license.
