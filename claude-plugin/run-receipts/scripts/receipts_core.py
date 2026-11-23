"""Shared storage and classification code for the Claude Code run-receipts plugin.

Standard library only. No network, no child processes. The hook script and the query
script both import this module. Nothing here records tool arguments, tool results,
prompts, assistant text, paths or working directories.

Layout under the state directory:
    receipts.jsonl        one JSON line per finished (or abandoned) session
    open/<session>.jsonl  small event log for a session that has started and not ended
    .lock                 writer lock used while a receipt is finalized
"""
import hashlib
import json
import os
import re
import time

try:  # macOS and Linux
    import fcntl
except ImportError:  # pragma: no cover - Windows: appends are unlocked
    fcntl = None

VERSION = 1
IDENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:/-]{0,79}$")
SESSION = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
SOURCES = ("startup", "resume", "clear", "compact", "fork")
END_REASONS = ("clear", "resume", "logout", "prompt_input_exit", "other")
# StopFailure error types from the hooks reference.
FAIL_CAUSES = ("rate_limit", "authentication_failed", "oauth_org_not_allowed",
               "account_on_hold", "billing_error", "invalid_request", "model_not_found",
               "server_error", "max_output_tokens", "cloud_credential_error", "unknown")
OUTCOMES = ("completed", "completed_no_tools", "completed_with_errors", "failed",
            "no_turn", "incomplete")
SUSPICIOUS = ("completed_with_errors", "failed", "no_turn", "incomplete")
# The only keys a query will ever return, whatever is on disk.
RECEIPT_KEYS = ("v", "session_id", "source", "started_at", "ended_at", "duration_s",
                "end_reason", "tool_calls", "tool_errors", "tools", "tool_stats",
                "outcome", "cause")

DEFAULT_KEEP_DAYS = 30
DEFAULT_STALE_HOURS = 12.0
MAX_STDIN = 1 << 20


class Refused(Exception):
    """A path was a symlink or otherwise unsafe to use."""


# ---------------------------------------------------------------- settings

def state_dir():
    explicit = os.environ.get("RUN_RECEIPTS_DIR")
    if explicit:
        return os.path.expanduser(explicit)
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(os.path.expanduser(base), "run-receipts")


def _number(name, default, minimum):
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    if value != value or value < minimum:  # NaN or too small
        return default
    return value


def keep_days():
    return _number("RUN_RECEIPTS_KEEP_DAYS", DEFAULT_KEEP_DAYS, 1)


def stale_hours():
    return _number("RUN_RECEIPTS_STALE_HOURS", DEFAULT_STALE_HOURS, 0.01)


# ---------------------------------------------------------------- small helpers

def ident(value):
    """Identifier-shaped strings only; anything else becomes 'unknown'."""
    if isinstance(value, str) and IDENT.match(value):
        return value
    return "unknown"


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def parse_iso(text):
    try:
        import calendar
        return float(calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return None


def file_key(session_id):
    """Safe file stem for a session; hashed when the id is not filename-safe."""
    if isinstance(session_id, str) and SESSION.match(session_id):
        return session_id
    digest = hashlib.sha256(str(session_id).encode("utf-8", "replace")).hexdigest()[:24]
    return "h-" + digest


def safe_session_id(session_id):
    return session_id if isinstance(session_id, str) and SESSION.match(session_id) else None


# ---------------------------------------------------------------- safe file access

def _check_not_symlink(path):
    if os.path.islink(path):
        raise Refused("refusing symlink: " + os.path.basename(path))


def ensure_dir(path, mode=0o700):
    _check_not_symlink(path)
    if not os.path.isdir(path):
        parent = os.path.dirname(path.rstrip(os.sep)) or os.sep
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        os.mkdir(path, mode)
    os.chmod(path, mode)


def ensure_state(base):
    ensure_dir(base)
    ensure_dir(os.path.join(base, "open"))


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def append_line(path, text, create=True):
    """Append one line (0600, no symlinks). Returns False when create=False and the file is absent."""
    flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW
    if create:
        flags |= os.O_CREAT
    try:
        fd = os.open(path, flags, 0o600)
    except FileNotFoundError:
        if create:
            raise
        return False
    try:
        os.fchmod(fd, 0o600)
        _write_all(fd, (text + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return True


def read_text(path):
    """Read a regular file without following a symlink. Returns '' if it is absent."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return ""
    with os.fdopen(fd, "rb") as handle:
        return handle.read().decode("utf-8", "replace")


class _Lock:
    def __init__(self, base):
        self.path = os.path.join(base, ".lock")
        self.fd = None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.fchmod(self.fd, 0o600)
        if fcntl is not None:
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            if fcntl is not None:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
        return False


def _json_lines(text):
    rows = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


# ---------------------------------------------------------------- recording

def _open_path(base, session_id):
    return os.path.join(base, "open", file_key(session_id) + ".jsonl")


def _emit(base, session_id, event, create=False):
    return append_line(_open_path(base, session_id), json.dumps(event, separators=(",", ":")), create=create)


def record_start(base, session_id, source, now=None):
    now = time.time() if now is None else now
    source = source if source in SOURCES else "unknown"
    ensure_state(base)
    path = _open_path(base, session_id)
    existing = os.path.lexists(path)
    if existing and source == "compact":
        return  # same session carrying on after compaction: keep its counts
    if existing:
        # A start for a session we never saw end: close the old record as incomplete.
        finalize(base, session_id, now, ended=False)
    event = {"e": "start", "t": round(now, 3), "src": source}
    sid = safe_session_id(session_id)
    if sid:
        event["sid"] = sid
    _emit(base, session_id, event, create=True)
    sweep(base, now, skip=file_key(session_id))


def record_tool(base, session_id, tool_name, failed):
    _emit(base, session_id, {"e": "tool", "n": ident(tool_name), "x": 1 if failed else 0})


def record_stop(base, session_id, last_message, now=None):
    now = time.time() if now is None else now
    empty = isinstance(last_message, str) and not last_message.strip()
    _emit(base, session_id, {"e": "stop", "t": round(now, 3), "empty": 1 if empty else 0})


def record_stop_failure(base, session_id, error, now=None):
    now = time.time() if now is None else now
    cause = error if error in FAIL_CAUSES else "unknown"
    _emit(base, session_id, {"e": "fail", "t": round(now, 3), "c": cause})


# ---------------------------------------------------------------- classification

def summarize(events, ended_at, end_reason):
    """Turn a session's event log into a receipt dict (no ended_at when ended_at is None)."""
    started = None
    source = "unknown"
    sid = None
    stats = {}
    last_turn = None
    for ev in events:
        kind = ev.get("e")
        if kind == "start" and started is None:
            if isinstance(ev.get("t"), (int, float)):
                started = float(ev["t"])
            source = ev.get("src") if ev.get("src") in SOURCES else "unknown"
            sid = safe_session_id(ev.get("sid"))
        elif kind == "tool":
            name = ident(ev.get("n"))
            slot = stats.setdefault(name, {"calls": 0, "errors": 0})
            slot["calls"] += 1
            if ev.get("x"):
                slot["errors"] += 1
        elif kind == "stop":
            last_turn = ("empty",) if ev.get("empty") else ("ok",)
        elif kind == "fail":
            cause = ev.get("c") if ev.get("c") in FAIL_CAUSES else "unknown"
            last_turn = ("fail", cause)
    calls = sum(v["calls"] for v in stats.values())
    errors = sum(v["errors"] for v in stats.values())
    receipt = {"v": VERSION, "session_id": sid, "source": source}
    receipt["started_at"] = iso(started) if started is not None else None
    cause = None
    if ended_at is None:
        outcome, cause = "incomplete", "no_end_seen"
    else:
        receipt["ended_at"] = iso(ended_at)
        if started is not None:
            receipt["duration_s"] = round(max(0.0, ended_at - started), 1)
        receipt["end_reason"] = end_reason if end_reason in END_REASONS else "other"
        if last_turn is None:
            outcome, cause = "no_turn", "no_turn_completed"
        elif last_turn[0] == "fail":
            outcome, cause = "failed", last_turn[1]
        elif last_turn[0] == "empty":
            outcome, cause = "failed", "empty_response"
        elif errors:
            outcome = "completed_with_errors"
        elif calls:
            outcome = "completed"
        else:
            outcome = "completed_no_tools"
    receipt.update({"tool_calls": calls, "tool_errors": errors,
                    "tools": sorted(stats), "tool_stats": {k: stats[k] for k in sorted(stats)},
                    "outcome": outcome})
    if cause:
        receipt["cause"] = cause
    return receipt


def finalize(base, session_id, now, ended=True, end_reason=None):
    """Write the receipt for an open session and remove its event log. Idempotent."""
    path = _open_path(base, session_id)
    ensure_state(base)
    with _Lock(base):
        text = read_text(path)
        if not text and not os.path.lexists(path):
            return None
        events = _json_lines(text)
        receipt = summarize(events, now if ended else None, end_reason)
        if receipt["session_id"] is None:
            receipt["session_id"] = safe_session_id(session_id)
        append_line(os.path.join(base, "receipts.jsonl"), json.dumps(receipt, separators=(",", ":")))
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        prune(base, now)
        return receipt


def sweep(base, now, skip=None):
    """Close open logs that have been quiet for stale_hours as 'incomplete'."""
    open_dir = os.path.join(base, "open")
    cutoff = now - stale_hours() * 3600
    try:
        names = os.listdir(open_dir)
    except OSError:
        return
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        stem = name[:-6]
        if stem == skip:
            continue
        path = os.path.join(open_dir, name)
        try:
            if os.path.islink(path) or os.lstat(path).st_mtime >= cutoff:
                continue
        except OSError:
            continue
        finalize(base, stem, now, ended=False)


def prune(base, now):
    """Drop receipts older than keep_days, and open logs abandoned for that long."""
    horizon = now - keep_days() * 86400
    path = os.path.join(base, "receipts.jsonl")
    text = read_text(path)
    if text:
        kept, dropped = [], False
        for raw in text.splitlines():
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except ValueError:
                dropped = True
                continue
            when = None
            if isinstance(row, dict):
                when = parse_iso(row.get("ended_at")) or parse_iso(row.get("started_at"))
            if when is not None and when < horizon:
                dropped = True
                continue
            kept.append(raw)
        if dropped:
            tmp = path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            try:
                os.fchmod(fd, 0o600)
                _write_all(fd, ("\n".join(kept) + ("\n" if kept else "")).encode("utf-8"))
            finally:
                os.close(fd)
            os.replace(tmp, path)
    open_dir = os.path.join(base, "open")
    try:
        names = os.listdir(open_dir)
    except OSError:
        return
    for name in names:
        full = os.path.join(open_dir, name)
        try:
            if os.lstat(full).st_mtime < horizon:
                os.unlink(full)
        except OSError:
            pass


# ---------------------------------------------------------------- reading (never writes)

def clean(row):
    """Keep only whitelisted keys, so tampered rows cannot push other text through a query."""
    out = {k: row[k] for k in RECEIPT_KEYS if k in row}
    for key in ("session_id", "source", "started_at", "ended_at", "end_reason", "outcome", "cause"):
        if key in out and not isinstance(out[key], (str, type(None))):
            del out[key]
    for key in ("tool_calls", "tool_errors", "duration_s"):
        if key in out and (isinstance(out[key], bool) or not isinstance(out[key], (int, float))):
            del out[key]
    if "tools" in out:
        tools = out["tools"]
        out["tools"] = [ident(t) for t in tools][:200] if isinstance(tools, list) else []
    if "tool_stats" in out:
        stats = out["tool_stats"]
        cleaned = {}
        if isinstance(stats, dict):
            for name, val in list(stats.items())[:200]:
                if isinstance(val, dict):
                    c, e = val.get("calls"), val.get("errors")
                    if isinstance(c, int) and isinstance(e, int) and not isinstance(c, bool):
                        cleaned[ident(name)] = {"calls": c, "errors": e}
        out["tool_stats"] = cleaned
    return out


def load_receipts(base):
    """All readable receipts in file order. Creates nothing and changes no modes."""
    path = os.path.join(base, "receipts.jsonl")
    _check_not_symlink(base)
    if not os.path.exists(path) and not os.path.islink(path):
        return []
    _check_not_symlink(path)
    return [clean(r) for r in _json_lines(read_text(path))]


def query(base, action, n=None, session_id=None, outcome=None, now=None):
    now = time.time() if now is None else now
    rows = load_receipts(base)
    if action == "suspicious":
        horizon = now - 7 * 86400
        picked = []
        for row in rows:
            if row.get("outcome") not in SUSPICIOUS:
                continue
            when = parse_iso(row.get("ended_at")) or parse_iso(row.get("started_at"))
            if when is not None and when >= horizon:
                picked.append(row)
        rows = picked
        default = 10
    elif action == "session":
        rows = [r for r in rows if session_id and r.get("session_id") == session_id]
        default = 50
    elif action == "outcome":
        rows = [r for r in rows if r.get("outcome") == outcome]
        default = 10
    elif action == "last":
        default = 10
    else:
        raise ValueError("unknown action: " + str(action))
    limit = default if n is None else max(1, min(200, int(n)))
    rows = list(reversed(rows))[:limit]
    return {"action": action, "count": len(rows), "receipts": rows}
