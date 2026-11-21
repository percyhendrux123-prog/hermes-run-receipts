"""run-receipts: one small receipt per agent-backed cron run, written outside the job's own output.

Observer only. Every hook swallows its own exceptions, never returns a value, and never changes
delivery. Interactive sessions and subagents are ignored.
"""
import json
import logging
import threading
import time

from . import core

log = logging.getLogger(__name__)

_clock = time.time  # tests replace this

TOOL_SCHEMA = {
    "name": "run_receipts_query",
    "description": (
        "Read-only. Look up run receipts the run-receipts plugin wrote for agent-backed cron runs, "
        "e.g. to see whether tool calls were observed in a silent run. action=last (recent receipts), "
        "job (receipts for one job_id) or suspicious (silent_unchecked, silent_with_errors, "
        "incomplete or failed in the last 7 days). Tool errors are reported or recognized errors, "
        "not a measure of job correctness; delivered is not a delivery acknowledgment."),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["last", "job", "suspicious"]},
            "job_id": {"type": "string", "description": "Required when action=job."},
            "n": {"type": "integer", "description": "How many receipts (1–200; default 50 for job, 10 otherwise)."},
        },
        "required": ["action"],
    },
}


class Recorder:
    """Holds the in-memory state of runs in progress and turns hook calls into receipts."""

    def __init__(self, get_config, profile="default", cron_job_id=None, clock=time.time):
        self._get_config = get_config
        self._profile = profile
        self._cron_job_id = cron_job_id or (lambda: None)
        self._clock = clock
        self._lock = threading.Lock()
        self._active = {}  # session_id -> (run dict, Store, response or None)

    def store(self):
        s = core.load_settings(self._get_config)
        return core.Store(s["state_dir"], s["keep_days"], s["store_reason"], clock=self._clock), s

    # -- hooks (each wrapped fail-open in register)
    def on_session_start(self, session_id=None, platform=None, **_):
        if not isinstance(session_id, str) or not session_id or not core.is_cron_run(platform, session_id):
            return
        with self._lock:
            if session_id in self._active:
                return
        store, _s = self.store()
        job_id = None
        try:
            job_id = core.ident(self._cron_job_id(), None)
        except Exception:
            pass
        job_id = job_id or core.job_id_from_session(session_id)
        closed = store.close_stale(job_id, session_id)
        run = core.new_run(session_id, job_id, self._profile, self._clock())
        store.save_open(run)
        with self._lock:
            for sid in closed:
                self._active.pop(sid, None)
            self._active[session_id] = [run, store, None]

    def post_tool_call(self, tool_name=None, session_id=None, status=None, result=None, **_):
        with self._lock:
            slot = self._active.get(session_id)
        if slot is None:
            return
        run, store, _resp = slot
        core.count_tool(run, tool_name, core.result_is_error(status, result))
        store.save_open(run)

    def post_llm_call(self, session_id=None, assistant_response=None, **_):
        with self._lock:
            slot = self._active.get(session_id)
            if slot is not None:
                slot[2] = assistant_response if isinstance(assistant_response, str) else None

    def on_session_end(self, session_id=None, completed=None, failed=False, interrupted=False,
                       turn_exit_reason=None, **_):
        with self._lock:
            slot = self._active.pop(session_id, None)
        if slot is None:
            return
        run, store, response = slot
        outcome, cause = core.classify(
            response, run["tool_calls"], run["tool_errors"], failed=bool(failed),
            interrupted=bool(interrupted), completed=completed, turn_exit_reason=turn_exit_reason)
        reason = core.reason_from(response, store.store_reason) if outcome.startswith("silent_") else None
        store.finalize(core.receipt_from_run(run, self._clock(), outcome, cause, reason))

    # -- read back
    def command(self, raw_args=""):
        store, _s = self.store()
        return core.run_command(store, raw_args)

    def tool(self, args, **_):
        store, _s = self.store()
        return json.dumps(core.query_tool(store, args), ensure_ascii=False)


def _fail_open(name, fn):
    def wrapper(*args, **kwargs):
        try:
            fn(*args, **kwargs)
        except Exception as exc:  # observer: never break the user's turn
            log.warning("run-receipts %s: %s", name, exc)
        return None
    wrapper.__name__ = f"run_receipts_{name}"
    return wrapper


def register(ctx):
    def cron_job_id():
        exec_fn = getattr(ctx, "current_cron_execution", None)
        cur = exec_fn() if callable(exec_fn) else None
        return getattr(cur, "job_id", None)

    rec = Recorder(
        get_config=lambda key, default=None: ctx.get_config(key, default),
        profile=core.ident(getattr(ctx, "profile_name", None), "default"),
        cron_job_id=cron_job_id,
        clock=lambda: _clock(),
    )
    for hook in ("on_session_start", "post_tool_call", "post_llm_call", "on_session_end"):
        ctx.register_hook(hook, _fail_open(hook, getattr(rec, hook)))

    def command(raw_args=""):
        try:
            return rec.command(raw_args)
        except Exception as exc:
            log.warning("run-receipts /receipts: %s", exc)
            return "receipts unavailable (see agent log)."

    def tool(args, **kw):
        try:
            return rec.tool(args, **kw)
        except Exception as exc:
            log.warning("run-receipts run_receipts_query: %s", exc)
            return json.dumps({"error": "receipts unavailable"})

    ctx.register_command("receipts", command, description="Show cron run receipts (last, job, suspicious)",
                         args_hint="last [n] | job <id> | suspicious [n]")
    ctx.register_tool(name="run_receipts_query", toolset="run_receipts", schema=TOOL_SCHEMA, handler=tool,
                      description=TOOL_SCHEMA["description"], emoji="🧾")
