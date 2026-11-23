#!/usr/bin/env python3
"""Read-only query over run-receipts. Prints JSON. Never creates files or changes modes.

    receipts_query.py last [n]
    receipts_query.py suspicious [n]
    receipts_query.py session <session-id> [n]
    receipts_query.py outcome <outcome> [n]

n is clamped to 1-200 (default 50 for `session`, 10 otherwise). Newest first.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import receipts_core as core  # noqa: E402

USAGE = ("usage: receipts_query.py last [n] | suspicious [n] | session <session-id> [n] "
         "| outcome <" + "|".join(core.OUTCOMES) + "> [n]")


def fail(message):
    print(json.dumps({"error": message}))
    return 2


def parse_n(token):
    if token is None:
        return None
    if not re.fullmatch(r"[0-9]{1,6}", token):
        raise ValueError("n must be a whole number")
    return int(token)


def main(argv):
    if not argv:
        return fail(USAGE)
    action, rest = argv[0], argv[1:]
    try:
        if action in ("last", "suspicious"):
            if len(rest) > 1:
                return fail(USAGE)
            result = core.query(core.state_dir(), action, n=parse_n(rest[0] if rest else None))
        elif action == "session":
            if not 1 <= len(rest) <= 2 or not core.SESSION.match(rest[0]):
                return fail(USAGE)
            result = core.query(core.state_dir(), "session", session_id=rest[0],
                                n=parse_n(rest[1] if len(rest) > 1 else None))
        elif action == "outcome":
            if not 1 <= len(rest) <= 2 or rest[0] not in core.OUTCOMES:
                return fail(USAGE)
            result = core.query(core.state_dir(), "outcome", outcome=rest[0],
                                n=parse_n(rest[1] if len(rest) > 1 else None))
        else:
            return fail(USAGE)
    except (ValueError, core.Refused, OSError) as exc:
        return fail(str(exc) if isinstance(exc, (ValueError, core.Refused)) else "could not read receipts")
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
