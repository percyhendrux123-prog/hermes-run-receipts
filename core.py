"""Run receipts core: pure logic, no hard Hermes imports.

One small receipt per agent-backed cron run, written outside the job's own
output. It records metadata, never tool arguments or tool results. Silent
response snippets are written to disk only if explicitly enabled, and neither
query surface returns them.
"""
import fcntl
import hashlib
import json
import logging
import os
import re
import stat
import sys
import tempfile
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
RECEIPTS_FILE = "receipts.jsonl"
OPEN_DIR = "open"
LOCK_FILE = ".lock"

OUTCOMES = ("delivered", "silent_checked", "silent_unchecked", "silent_with_errors", "failed", "incomplete")
SUSPICIOUS = frozenset({"silent_unchecked", "silent_with_errors", "incomplete", "failed"})
SUSPICIOUS_DAYS = 7
REASON_MAX = 200
MAX_TOOLS = 200
DEFAULT_KEEP_DAYS = 30

_IDENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:/-]{0,79}$")
# Fallback session-id convention from cron/scheduler.py: f"cron_{job_id}_{YYYYmmdd_HHMMSS}".
_CRON_SID = re.compile(r"^cron_(?P<job>.+)_\d{8}_\d{6}$")

# --- upstream silence / failure rules ---------------------------------------
# Copied from cron/scheduler.py + gateway/response_filters.py (Hermes 2026-11).
# Used only when the host modules cannot be imported at runtime.
SILENT_MARKER = "[SILENT]"
CRON_FAILURE_MARKER = "[CRON_FAILURE]"
_SILENT_MARKERS = frozenset({"[SILENT]", "SILENT", "NO_REPLY", "NO REPLY", "[静默]", "静默", "[沉默]", "沉默"})
_BRACKETED = tuple(sorted(m for m in _SILENT_MARKERS if m.startswith("[")))
_MARKER_LENGTH_CAP = 64


def _canon(text):
    return " ".join(text.strip().upper().split())


def _edge_punct(ch):
    return ch not in "[]" and unicodedata.category(ch).startswith("P")


def _strip_edges(text):
    start, end = 0, len(text)
    while start < end and _edge_punct(text[start]):
        start += 1
    while end > start and _edge_punct(text[end - 1]):
        end -= 1
    return text[start:end].strip()


def _candidates(text):
    stripped = text.strip() if isinstance(text, str) else ""
    if not 0 < len(stripped) <= _MARKER_LENGTH_CAP:
        return ()
    dep = _strip_edges(stripped)
    forms = (stripped,) if dep == stripped else (stripped, dep)
    return tuple(_canon(f) for f in forms)


def _fallback_is_intentional(response):
    return any(c in _SILENT_MARKERS for c in _candidates(response))


def _fallback_is_silent(response):
    """Copy of gateway.response_filters.is_autonomous_silence_response."""
    stripped = response.strip() if isinstance(response, str) else ""
    if not stripped:
        return False
    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    return stripped.upper().startswith(_BRACKETED) or any(
        _fallback_is_intentional(c) for c in (stripped, lines[0], lines[-1]))


def _fallback_failure(text):
    """Copy of cron.scheduler._cron_failure_marker_error."""
    lines = (text or "").splitlines()
    if not lines or lines[0].rstrip() != CRON_FAILURE_MARKER:
        return None
    return "\n".join(lines[1:]).strip() or "Cron agent reported failure."


def _upstream(module, name):
    """Return ``module.name`` only if the host module is already loaded or cheap to import."""
    try:
        mod = sys.modules.get(module)
        if mod is None:
            if module == "cron.scheduler":  # heavy; a cron run has already imported it
                return None
            import importlib
            mod = importlib.import_module(module)
        fn = getattr(mod, name, None)
        return fn if callable(fn) else None
    except Exception:  # fail open to the copied rule
        return None


def is_silent(response):
    """Upstream cron silence rule, imported at runtime; copied fallback."""
    fn = _upstream("cron.scheduler", "_is_cron_silence_response") or \
        _upstream("gateway.response_filters", "is_autonomous_silence_response")
    if fn is not None:
        try:
            return bool(fn(response))
        except Exception:
            pass
    return _fallback_is_silent(response)


def failure_marker(response):
    """Upstream strict first-line [CRON_FAILURE] rule; copied fallback. True/False."""
    fn = _upstream("cron.scheduler", "_cron_failure_marker_error")
    if fn is not None:
        try:
            return fn(response) is not None
        except Exception:
            pass
    return _fallback_failure(response) is not None


# --- small helpers ------------------------------------------------------------
def ident(value, default: "str | None" = "unknown"):
    """Identifier-shaped strings only; anything else is not stored."""
    value = value if isinstance(value, str) else ""
    return value if _IDENT.match(value) else default


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def job_id_from_session(session_id):
    m = _CRON_SID.match(session_id or "")
    return ident(m.group("job"), None) if m else None


def is_cron_run(platform, session_id=None):
    """Cron runs: AIAgent(platform="cron"). Subagents are platform="subagent"; interactive
    sessions are cli/telegram/etc., so they never match. The session-id convention is only
    consulted when the platform is missing."""
    plat = (platform or "").strip().lower()
    if plat:
        return plat == "cron"
    return bool(_CRON_SID.match(session_id or ""))


def result_is_error(status, result):
    """Whether a post_tool_call result is an error. The host derives ``status`` ("ok"/"error")
    from the result (model_tools._tool_result_observer_fields); trust it, and only inspect the
    result's top-level JSON shape when the host gave no status."""
    if status == "error":
        return True
    if status == "ok":
        return False
    try:
        data = json.loads(result) if isinstance(result, str) else result
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    if data.get("error"):
        return True
    if data.get("success") is False:
        return True
    code = data.get("exit_code")
    return isinstance(code, int) and not isinstance(code, bool) and code != 0


def classify(response, tool_calls, tool_errors, *, failed=False, interrupted=False, completed=None,
             turn_exit_reason=None):
    """Return (outcome, cause). ``cause`` is a fixed vocabulary word or None."""
    reason = turn_exit_reason if isinstance(turn_exit_reason, str) else ""
    text = response if isinstance(response, str) else ""
    if interrupted:
        return "failed", "interrupted"
    if failed:
        return "failed", "agent_failed"
    if completed is False and not (reason.startswith("max_iterations_reached(") and text.strip()):
        return "failed", "not_completed"
    if text and failure_marker(text):
        return "failed", "failure_marker"
    if reason == "empty_response_exhausted" or not text.strip() or text.strip() == "(No response generated)":
        return "failed", "empty_response"  # cron treats an empty final response as a soft failure
    if is_silent(text):
        if tool_errors > 0:
            return "silent_with_errors", None
        if tool_calls == 0:
            return "silent_unchecked", None
        return "silent_checked", None
    return "delivered", None


# --- settings -------------------------------------------------------------------
def default_state_dir():
    home = None
    try:
        from hermes_constants import get_hermes_home  # type: ignore
        home = get_hermes_home()
    except Exception:
        pass
    if not home:
        home = os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")
    return Path(home) / "run-receipts"


def _truthy(value, default):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off"):
            return False
    return default


def load_settings(get):
    """``get(key, default)`` reads plugins.entries.run-receipts.settings. Bad values -> defaults."""
    def read(key, default=None):
        try:
            return get(key, default)
        except Exception:
            return default

    raw_dir = read("state_dir")
    state_dir = Path(os.path.expanduser(raw_dir)) if isinstance(raw_dir, str) and raw_dir.strip() \
        else default_state_dir()
    keep = read("keep_days", DEFAULT_KEEP_DAYS)
    if isinstance(keep, bool) or not isinstance(keep, (int, float)) or keep <= 0:
        keep = DEFAULT_KEEP_DAYS
    return {"state_dir": state_dir, "keep_days": float(keep),
            "store_reason": _truthy(read("store_reason", False), False)}


# --- store ----------------------------------------------------------------------
class Store:
    """Append-only JSONL receipts plus one small file per still-open run. Everything 0600
    inside a 0700 directory; symlinks are refused; writers serialise on an flock."""

    _tlock = threading.RLock()

    def __init__(self, state_dir, keep_days=DEFAULT_KEEP_DAYS, store_reason=False, clock=time.time):
        self.dir = Path(state_dir)
        self.keep_days = keep_days
        self.store_reason = store_reason
        self.clock = clock

    @property
    def receipts_path(self):
        return self.dir / RECEIPTS_FILE

    @property
    def open_dir(self):
        return self.dir / OPEN_DIR

    # -- filesystem plumbing
    def _ensure(self):
        for d in (self.dir, self.open_dir):
            if d.is_symlink():
                raise OSError(f"refusing symlinked state directory: {d}")
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)
        if self.receipts_path.is_symlink():
            raise OSError(f"refusing symlinked receipts file: {self.receipts_path}")

    def _lock(self):
        class _L:
            def __enter__(s):
                Store._tlock.acquire()
                try:
                    self._ensure()
                    s.fd = os.open(self.dir / LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                    os.fchmod(s.fd, 0o600)
                    fcntl.flock(s.fd, fcntl.LOCK_EX)
                except BaseException:
                    Store._tlock.release()
                    raise
                return s

            def __exit__(s, *exc):
                try:
                    fcntl.flock(s.fd, fcntl.LOCK_UN)
                    os.close(s.fd)
                finally:
                    Store._tlock.release()
        return _L()

    @staticmethod
    def _atomic_write(path, text):
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def _append_separator(fd):
        # Scan backwards only as far as the last newline. Work in bytes:
        # an interrupted write can end inside a UTF-8 character.
        end = os.lseek(fd, 0, os.SEEK_END)
        pos = end
        while pos:
            size = min(pos, 8192)
            pos -= size
            chunk = os.pread(fd, size, pos)
            if len(chunk) != size:
                raise OSError("short read while checking receipt tail")
            last = chunk.rfind(b"\n")
            if last >= 0:
                pos += last + 1
                break
        if pos != end:
            tail = os.pread(fd, end - pos, pos)
            if len(tail) != end - pos:
                raise OSError("short read while checking receipt tail")
            if Store._parse_receipt(tail) is not None:
                # The legacy writer could leave a complete row missing only its newline.
                return b"\n"
            os.ftruncate(fd, pos)
        return b""

    def _append_line(self, line):
        fd = os.open(self.receipts_path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            separator = self._append_separator(fd)
            remaining = memoryview(separator + (line + "\n").encode("utf-8"))
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("receipt append made no progress")
                remaining = remaining[written:]
        finally:
            os.close(fd)

    # -- receipts
    def read_all(self):
        """All complete, parseable receipts in file order."""
        return self._scan_receipts()[0]

    def _open_receipts_readonly(self):
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        # Open the directory itself without following links, then bind the leaf open
        # to that descriptor; do not rely on a check followed by a path-based open.
        dir_fd = os.open(self.dir, flags)
        try:
            if not stat.S_ISDIR(os.fstat(dir_fd).st_mode):
                raise NotADirectoryError(str(self.dir))
            return open(RECEIPTS_FILE, "rb", opener=lambda path, _: os.open(path, flags, dir_fd=dir_fd))
        finally:
            os.close(dir_fd)

    @staticmethod
    def _receipt_json(rec):
        return json.dumps(rec, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _receipt_object(pairs):
        obj = dict(pairs)
        if len(obj) != len(pairs):
            # Do not let a later key hide a value that cannot be rewritten.
            raise ValueError("duplicate receipt object key")
        return obj

    @staticmethod
    def _parse_receipt(line):
        try:
            # json.loads(bytes) can accept UTF-8 sequences for surrogate code points.
            rec = json.loads(line.decode("utf-8"), object_pairs_hook=Store._receipt_object)
            if not isinstance(rec, dict):
                return None
            # JSON escapes can introduce lone surrogates anywhere in a row. Reject it
            # before append or prune recovery treats it as a complete receipt.
            Store._receipt_json(rec).encode("utf-8")
            return rec
        except (ValueError, RecursionError):
            return None

    def _scan_receipts(self):
        out, needs_rewrite = [], False
        try:
            with self._open_receipts_readonly() as fh:
                if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                    raise OSError("receipts file is not a regular file")
                for line in fh:
                    needs_rewrite |= not line.endswith(b"\n")
                    if not line.strip():
                        continue
                    rec = self._parse_receipt(line)
                    if rec is not None:
                        out.append(rec)
                    else:
                        needs_rewrite = True
        except FileNotFoundError:
            pass
        return out, needs_rewrite

    def _stamp(self, rec):
        return parse_iso(rec.get("ended_at")) or parse_iso(rec.get("started_at")) or 0.0

    def _prune_locked(self):
        cutoff = self.clock() - self.keep_days * 86400
        recs, needs_rewrite = self._scan_receipts()
        keep = []
        for rec in recs:
            if self._stamp(rec) >= cutoff:
                keep.append(rec)
            elif isinstance(rec.get("session_id"), str):
                # Never forget a committed ID while its checkpoint can still be recovered.
                if not self._drop_open_locked(rec["session_id"]):
                    keep.append(rec)
        if len(keep) != len(recs) or needs_rewrite:
            self._atomic_write(self.receipts_path,
                               "".join(self._receipt_json(r) + "\n" for r in keep))
        # abandoned open-run files past retention are removed, not turned into receipts
        try:
            for p in self.open_dir.glob("*.json"):
                if p.stat().st_mtime < cutoff:
                    p.unlink()
        except OSError:
            pass

    def finalize(self, rec):
        with self._lock():
            self._finalize_locked(rec, self._finalized_ids())
            self._prune_locked()

    def _finalized_ids(self):
        return {r["session_id"] for r in self.read_all() if isinstance(r.get("session_id"), str)}

    def _finalize_locked(self, rec, finalized):
        sid = rec["session_id"]
        if sid not in finalized:
            self._append_line(self._receipt_json(rec))
            finalized.add(sid)
        self._drop_open_locked(sid)

    # -- open runs
    def _open_path(self, session_id):
        return self.open_dir / (hashlib.sha1(session_id.encode("utf-8")).hexdigest()[:24] + ".json")

    def save_open(self, run):
        with self._lock():
            self._atomic_write(self._open_path(run["session_id"]), json.dumps(run, sort_keys=True))

    def _drop_open_locked(self, session_id):
        try:
            self._open_path(session_id).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            # The receipt already exists. Leave it authoritative and let later runs proceed.
            log.warning("run-receipts checkpoint cleanup: %s", exc)
            return False
        return True

    def list_open(self):
        out = []
        try:
            for p in sorted(self.open_dir.glob("*.json")):
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(data, dict) and isinstance(data.get("session_id"), str):
                    out.append(data)
        except OSError:
            pass
        return out

    def close_stale(self, job_id, except_session):
        """Close earlier checkpoints for the job; record ``incomplete`` only if not finalized."""
        closed = []
        if not job_id:
            return closed
        with self._lock():
            finalized = self._finalized_ids()
            for run in self.list_open():
                if run.get("job_id") != job_id or run["session_id"] == except_session:
                    continue
                rec = receipt_from_run(run, ended=None, outcome="incomplete", cause="no_end_seen")
                self._finalize_locked(rec, finalized)
                closed.append(run["session_id"])
            self._prune_locked()
        return closed

    # -- reading back
    def last(self, n=10):
        n = clamp_n(n)
        return list(reversed(self.read_all()[-n:]))

    def for_job(self, job_id, n=50):
        n = clamp_n(n, default=50)
        rows = [r for r in self.read_all() if r.get("job_id") == job_id]
        return list(reversed(rows[-n:]))

    def suspicious(self, days=SUSPICIOUS_DAYS, *, n=10):
        n = clamp_n(n)
        cutoff = self.clock() - days * 86400
        rows = [r for r in self.read_all()
                if isinstance(r.get("outcome"), str) and r["outcome"] in SUSPICIOUS and self._stamp(r) >= cutoff]
        return list(reversed(rows[-n:]))


def clamp_n(n, default=10, maximum=200):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, maximum))


# --- run state -> receipt -----------------------------------------------------------
def new_run(session_id, job_id, profile, now):
    return {"session_id": session_id, "job_id": job_id, "profile": profile,
            "started_at": iso(now), "started_epoch": now,
            "tool_calls": 0, "tool_errors": 0, "tool_stats": {}}


def count_tool(run, tool_name, errored):
    name = ident(tool_name)
    stats = run["tool_stats"]
    if name not in stats and len(stats) >= MAX_TOOLS:
        name = "other"
    entry = stats.setdefault(name, {"calls": 0, "errors": 0})
    entry["calls"] += 1
    run["tool_calls"] += 1
    if errored:
        entry["errors"] += 1
        run["tool_errors"] += 1


def receipt_from_run(run, ended, outcome, cause=None, reason=None):
    started = run.get("started_epoch")
    rec = {
        "v": SCHEMA_VERSION,
        "job_id": run.get("job_id"),
        "session_id": run.get("session_id"),
        "profile": run.get("profile"),
        "started_at": run.get("started_at"),
        "ended_at": iso(ended) if ended is not None else None,
        "duration_s": round(ended - started, 3) if ended is not None and started is not None else None,
        "tool_calls": run.get("tool_calls", 0),
        "tool_errors": run.get("tool_errors", 0),
        "tools": sorted(run.get("tool_stats", {})),
        "tool_stats": run.get("tool_stats", {}),
        "outcome": outcome,
    }
    if cause:
        rec["cause"] = cause
    if reason:
        rec["reason"] = reason
    return rec


def reason_from(response, enabled):
    if not enabled or not isinstance(response, str):
        return None
    text = " ".join(response.split())[:REASON_MAX]
    return text or None


# --- formatting for /receipts -------------------------------------------------------
def format_row(rec):
    dur = rec.get("duration_s")
    parts = [
        rec.get("ended_at") or rec.get("started_at") or "?",
        f"job={rec.get('job_id') or '?'}",
        rec.get("outcome", "?"),
        f"calls={rec.get('tool_calls', 0)}",
        f"errors={rec.get('tool_errors', 0)}",
        f"{dur}s" if dur is not None else "no-end",
    ]
    if rec.get("cause"):
        parts.append(f"cause={rec['cause']}")
    return "  ".join(parts)


HELP = ("/receipts last [n]       - most recent receipts (default 10, max 200)\n"
        "/receipts job <id>       - up to 50 receipts for one cron job\n"
        "/receipts suspicious [n] - silent_unchecked, silent_with_errors, incomplete, failed "
        "(last 7 days; default 10, max 200)")


def run_command(store, raw_args):
    parts = (raw_args or "").split()
    if not parts or parts[0] in ("help", "-h", "--help"):
        return HELP
    cmd = parts[0].lower()
    if cmd == "last":
        rows = store.last(parts[1] if len(parts) > 1 else 10)
    elif cmd == "job":
        if len(parts) < 2:
            return "usage: /receipts job <id>"
        rows = store.for_job(parts[1])
    elif cmd == "suspicious":
        rows = store.suspicious(n=parts[1] if len(parts) > 1 else 10)
    else:
        return HELP
    if not rows:
        return "No receipts."
    return "\n".join(format_row(r) for r in rows)


def query_tool(store, args):
    """run_receipts_query: read-only. Returns a JSON-serialisable dict."""
    args = args if isinstance(args, dict) else {}
    action = args.get("action") or "last"
    if action == "last":
        rows = store.last(args.get("n", 10))
    elif action == "job":
        job_id = args.get("job_id")
        if not isinstance(job_id, str) or not job_id.strip():
            return {"error": "job_id is required for action=job"}
        rows = store.for_job(job_id.strip(), args.get("n", 50))
    elif action == "suspicious":
        rows = store.suspicious(n=args.get("n", 10))
    else:
        return {"error": f"unknown action {str(action)[:40]!r}; use last, job or suspicious"}
    # The toolset is not an operator-only access boundary. Do not return response text,
    # including reasons already on disk from older releases or explicit local opt-in.
    metadata = [{key: value for key, value in row.items() if key != "reason"} for row in rows]
    return {"action": action, "count": len(metadata), "receipts": metadata}
