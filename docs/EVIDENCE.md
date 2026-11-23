# Evidence

Run on Linux, Python 3.12.3, using a read-only staged Hermes source snapshot for the optional parity test.

## Tests

```
$ HERMES_AGENT_SRC=/path/to/hermes-agent python3 -m unittest discover -s tests
$ HERMES_AGENT_SRC=/path/to/hermes-agent python3 -W error::ResourceWarning -m unittest discover -s tests
```

Both runs: 88 passed, 0 failed, 0 skipped with the staged source available. Stdlib only, no network. Test fixtures use the system temporary directory by default and clean up after each test. To choose another writable temp directory: `TMPDIR=/path/to/writable/temp python3 -m unittest discover -s tests`.

The optional parity test compares sample inputs to the staged `gateway/response_filters.py` matcher. It checks the gateway matcher, not the scheduler emitter, and is not a compatibility check across Hermes versions. The test skips if that source file is unavailable.

| class | tests | covers |
|---|---|---|
| OutcomeTests | 20 | all outcomes, max-iteration summary exception, exhausted empty reply despite a nonempty placeholder, stale recovery |
| DetectionTests | 7 | interactive/subagent sessions ignored (nothing written), cron by platform, opaque session id, job id from host identity or session id, other sessions' tool calls not counted, concurrent runs kept apart |
| CountingTests | 6 | per-tool counts, error shapes, host status wins, no arguments or results stored, odd tool names, timing |
| ReasonTests | 5 | metadata by default, on-disk opt-in, 200-character cap, no reason disclosure from either query surface, including old rows |
| StorageTests | 15 | 0600 files / 0700 dirs, write-side symlinks refused, state-dir settings, checkpoint cleanup, short writes, legacy rows missing a newline, malformed tails and idempotent finalization |
| PruneTests | 6 | prune on write at 30 days, `keep_days`, bad values, corrupt lines and invalid session IDs, mode after prune |
| FailOpenTests | 6 | hooks return `None` and never raise on garbage, unwritable dir, exception inside a hook, broken config reader, query surfaces report errors, failed write leaves no bad state |
| QueryTests | 11 | tool and slash reads, filtering, limits, 7-day window, malformed outcomes, read-only behavior and read-side symlink refusal |
| SilentRuleTests | 7 | matcher selection, fallback known answers and gateway matcher parity against staged source |
| DocumentationTests | 2 | observation and transport wording, evidence and parity scope |
| ManifestTests | 3 | registrations, opt-in manifest default, no network/subprocess imports |

## Regression coverage

Receipt storage regressions simulate an unlink failure after a complete append and a fresh recorder, real short writes, a torn tail injected after a different job starts, and a late end from another recorder. They check that complete receipts remain and that a session is not recorded twice.

Further regressions check that the exact legacy writer output missing only its newline stays readable and survives the next run, and that an expired row with an invalid session ID does not stop later receipts. Another regression checks a JSON-escaped lone surrogate in another field: recovery discards it both before a named run starts and at append if it appears during a run. The append test checks the file before pruning and covers a duplicate key that would otherwise hide the surrogate. The torn JSON and raw invalid UTF-8 controls remain.

Privacy regressions exercise default and invalid settings, the explicit local opt-in, and both registered query surfaces with new and legacy reason-bearing rows. Queries omit reasons without rewriting the local file.

Both registered query paths refuse a symlink at the state directory or receipts file; they leave a missing store absent and target contents, file lists, and modes unchanged.

`suspicious` query tests check the default, `n=1`, and the 200-row cap through both registered entry points, with newest rows first and the existing seven-day filter covered separately.

An additional simulated-hook regression reproduces the payload observed in a live scheduler smoke test: a non-empty “No reply” placeholder with `completed=True`, `failed=False`, and `turn_exit_reason="empty_response_exhausted"` yields `failed` / `empty_response`. The same text without the exit reason is not classified as a failure. The scheduler smoke itself was not run in this suite.

The documentation check preserves the distinction between observation and proof: the plugin records separately from job output, does not acknowledge transport delivery, and counts only reported or recognized tool errors. Existing behavioral tests cover bracketed/localized silence and the non-empty max-iteration exception.

Adversarial-review regressions assert that a persistent cleanup failure neither duplicates the previously committed row nor prevents later receipts, and that a non-string outcome cannot hide valid `suspicious` rows. The new tests were run with each corresponding fix reverted after commit; each failed as expected. The fixed files were restored before rerunning the tests.

## Not verified

`hermes plugins validate .` and a scheduler smoke for this revision require a Hermes host. These tests do not verify transport delivery. Script-mode (`no_agent`) crons are not covered (see README).

## Claude Code port (claude-plugin/run-receipts)

Run on macOS, Python 3 stdlib only, Claude Code 2.1.325.

```
$ python3 -W error::ResourceWarning -m unittest discover -s claude-plugin/tests
Ran 63 tests ... OK
$ python3 -m unittest discover -s tests
Ran 88 tests ... OK
$ claude plugin validate --strict claude-plugin/run-receipts   # Validation passed
$ claude plugin validate --strict .                            # Validation passed
```

The 63 new tests run the real hook and query scripts as subprocesses with sample hook events: every outcome, multi-session separation, subagent events ignored, compaction and restart handling, stale-session recovery, pruning, 0600/0700 modes, symlink refusal, garbage input and unwritable directories (fail open, empty stdout), no canary text reaching disk, read-only queries, tampered-row filtering, manifest and hook-event checks.

Live check: headless `claude -p` runs in a throwaway `CLAUDE_CONFIG_DIR`, plugin installed from this checkout. Receipts were written for a run with two Bash calls (`completed`), a run with no tools (`completed_no_tools`), and a run whose Bash call failed (`completed_with_errors`, 1 error). `claude -p "/axiom-run-receipts:receipts suspicious"` read the failed-call receipt back. `StopFailure`, interactive sessions and resumed sessions were not exercised live.
