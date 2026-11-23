#!/usr/bin/env python3
"""Claude Code hook for run-receipts. Observer only: it never blocks, approves or rewrites anything.

Reads one hook event on stdin and updates a small private event log, or finalizes the
session's receipt. Prints nothing and always exits 0, so a failure here can never stop
a session (a storage failure can leave a missing receipt).
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))


def main():
    import receipts_core as core

    raw = sys.stdin.buffer.read(core.MAX_STDIN)
    event = json.loads(raw.decode("utf-8", "replace"))
    if not isinstance(event, dict):
        return
    name = event.get("hook_event_name")
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return
    base = core.state_dir()
    now = time.time()

    if name == "SessionStart":
        core.record_start(base, session_id, event.get("source"), now)
        return
    if name == "SessionEnd":
        core.finalize(base, session_id, now, ended=True, end_reason=event.get("reason"))
        return
    # Everything below describes the main agent only. Subagent activity carries agent_id.
    if event.get("agent_id"):
        return
    if name == "PostToolUse":
        core.record_tool(base, session_id, event.get("tool_name"), failed=False)
    elif name == "PostToolUseFailure":
        core.record_tool(base, session_id, event.get("tool_name"), failed=True)
    elif name == "Stop":
        core.record_stop(base, session_id, event.get("last_assistant_message"), now)
    elif name == "StopFailure":
        core.record_stop_failure(base, session_id, event.get("error"), now)


if __name__ == "__main__":
    try:
        main()
    except BaseException:  # fail open, whatever went wrong
        pass
    sys.exit(0)
