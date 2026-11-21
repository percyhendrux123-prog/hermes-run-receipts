"""Unit tests: simulated hook sequences, no Hermes install, no network.

Run from the repo root: python3 -m unittest discover -s tests
"""
import importlib
import importlib.util
import json
import os
import shutil
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = os.environ.get("TMPDIR") or tempfile.gettempdir()
PKG = "hermes_run_receipts"


def load_package():
    if PKG in sys.modules:
        return sys.modules[PKG]
    spec = importlib.util.spec_from_file_location(PKG, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = mod
    spec.loader.exec_module(mod)
    return mod


plugin = load_package()
core = importlib.import_module(PKG + ".core")

T0 = 1_800_000_000.0  # fixed clock origin
DAY = 86400.0


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


class FakeCtx:
    profile_name = "tester"

    def __init__(self, settings=None, job_id=None):
        self.settings = settings or {}
        self.hooks, self.commands, self.tools = {}, {}, {}
        self.job_id = job_id

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def current_cron_execution(self):
        return types.SimpleNamespace(job_id=self.job_id) if self.job_id else None

    def register_hook(self, name, cb, **kw):
        self.hooks[name] = cb

    def register_command(self, name, handler, description="", args_hint="", **kw):
        self.commands[name] = handler

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = (schema, handler)


class Base(unittest.TestCase):
    def setUp(self):
        os.makedirs(SCRATCH, exist_ok=True)
        self.tmp = os.path.realpath(tempfile.mkdtemp(dir=SCRATCH, prefix="rr-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = os.path.join(self.tmp, "state")
        self.clock = Clock()
        patcher = mock.patch.object(plugin, "_clock", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ctx = self.make_ctx()

    def make_ctx(self, job_id=None, **settings):
        ctx = FakeCtx(dict({"state_dir": self.state}, **settings), job_id=job_id)
        plugin.register(ctx)
        return ctx

    # simulated hook sequence for one cron run
    def run_cron(self, ctx=None, sid="cron_watch_20261121_090000", platform="cron", tools=(), response="done",
                 end=True, failed=False, interrupted=False, completed=True, duration=5.0, turn_exit_reason=None,
                 skip_llm=False):
        ctx = ctx or self.ctx
        h = ctx.hooks
        h["on_session_start"](session_id=sid, model="m", platform=platform)
        for name, status in tools:
            h["post_tool_call"](tool_name=name, args={"secret": "ARG-SECRET"}, result='{"x": "RESULT-SECRET"}',
                                session_id=sid, status=status)
        if not skip_llm:
            h["post_llm_call"](session_id=sid, assistant_response=response, platform=platform)
        self.clock.now += duration
        if end:
            h["on_session_end"](session_id=sid, completed=completed, failed=failed, interrupted=interrupted,
                                turn_exit_reason=turn_exit_reason, platform=platform)

    def receipts(self):
        path = os.path.join(self.state, "receipts.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def store(self, **kw):
        return core.Store(self.state, clock=self.clock, **kw)


class OutcomeTests(Base):
    def test_delivered(self):
        self.run_cron(tools=[("terminal", "ok")], response="Report: 3 new leads.")
        (r,) = self.receipts()
        self.assertEqual(r["outcome"], "delivered")
        self.assertEqual((r["tool_calls"], r["tool_errors"]), (1, 0))
        self.assertNotIn("reason", r)

    def test_silent_checked(self):
        self.run_cron(tools=[("web_search", "ok"), ("terminal", "ok")], response="[SILENT]")
        (r,) = self.receipts()
        self.assertEqual(r["outcome"], "silent_checked")
        self.assertEqual(r["tools"], ["terminal", "web_search"])

    def test_silent_checked_with_trailing_marker_line(self):
        self.run_cron(tools=[("terminal", "ok")], response="Nothing changed.\n[SILENT]")
        self.assertEqual(self.receipts()[0]["outcome"], "silent_checked")

    def test_silent_unchecked(self):
        self.run_cron(tools=[], response="[SILENT]")
        (r,) = self.receipts()
        self.assertEqual(r["outcome"], "silent_unchecked")
        self.assertEqual(r["tool_calls"], 0)

    def test_silent_with_errors(self):
        self.run_cron(tools=[("terminal", "ok"), ("terminal", "error")], response="[SILENT]")
        (r,) = self.receipts()
        self.assertEqual(r["outcome"], "silent_with_errors")
        self.assertEqual((r["tool_calls"], r["tool_errors"]), (2, 1))

    def test_silent_with_errors_wins_over_zero_ok_calls(self):
        self.run_cron(tools=[("terminal", "error")], response="NO_REPLY")
        self.assertEqual(self.receipts()[0]["outcome"], "silent_with_errors")

    def test_mid_sentence_marker_is_delivered(self):
        self.run_cron(tools=[("terminal", "ok")], response="I will not say [SILENT] today, but here is news.")
        self.assertEqual(self.receipts()[0]["outcome"], "delivered")

    def test_failed_flag(self):
        self.run_cron(tools=[("terminal", "ok")], response="boom", failed=True, completed=False)
        r = self.receipts()[0]
        self.assertEqual((r["outcome"], r["cause"]), ("failed", "agent_failed"))

    def test_failed_interrupted(self):
        self.run_cron(response="partial", interrupted=True, completed=False)
        r = self.receipts()[0]
        self.assertEqual((r["outcome"], r["cause"]), ("failed", "interrupted"))

    def test_failed_not_completed_without_llm_call(self):
        self.run_cron(skip_llm=True, completed=False)  # exception path: no post_llm_call fired
        r = self.receipts()[0]
        self.assertEqual((r["outcome"], r["cause"]), ("failed", "not_completed"))

    def test_max_iteration_handoff_with_summary_is_not_failed(self):
        self.run_cron(tools=[("terminal", "ok")], response="Ran out of steps; summary: ...", completed=False,
                      turn_exit_reason="max_iterations_reached(30/30)")
        self.assertEqual(self.receipts()[0]["outcome"], "delivered")

    def test_failure_marker_first_line(self):
        self.run_cron(tools=[("terminal", "ok")], response="[CRON_FAILURE]\nsource unreachable")
        r = self.receipts()[0]
        self.assertEqual((r["outcome"], r["cause"]), ("failed", "failure_marker"))

    def test_quoted_failure_marker_is_not_failure(self):
        self.run_cron(tools=[("terminal", "ok")], response="Report mentions [CRON_FAILURE] in a log line.")
        self.assertEqual(self.receipts()[0]["outcome"], "delivered")

    def test_empty_response_is_failed(self):
        self.run_cron(tools=[("terminal", "ok")], response="   ")
        r = self.receipts()[0]
        self.assertEqual((r["outcome"], r["cause"]), ("failed", "empty_response"))

    def test_empty_response_exhausted_with_host_placeholder_is_failed(self):
        placeholder = "No reply: the model didn't produce a reply"
        self.run_cron(response=placeholder, completed=True, failed=False, interrupted=False,
                      turn_exit_reason="empty_response_exhausted")
        self.run_cron(sid="cron_watch_20261121_100000", response=placeholder, completed=True, failed=False)
        exhausted, normal = self.receipts()
        self.assertEqual((exhausted["outcome"], exhausted.get("cause")), ("failed", "empty_response"))
        self.assertEqual(normal["outcome"], "delivered")  # the text alone isn't a failure marker

    def test_incomplete_detected_on_next_start_of_same_job(self):
        self.run_cron(sid="cron_watch_20261121_090000", tools=[("terminal", "ok"), ("terminal", "error")],
                      end=False, skip_llm=True)
        self.assertEqual(self.receipts(), [])  # nothing yet: run may still be going
        self.clock.now += 3600
        self.run_cron(sid="cron_watch_20261121_100000", tools=[("terminal", "ok")], response="[SILENT]")
        first, second = self.receipts()
        self.assertEqual(first["outcome"], "incomplete")
        self.assertEqual(first["session_id"], "cron_watch_20261121_090000")
        self.assertEqual(first["cause"], "no_end_seen")
        self.assertIsNone(first["ended_at"])
        self.assertIsNone(first["duration_s"])
        self.assertEqual((first["tool_calls"], first["tool_errors"]), (2, 1))
        self.assertEqual(second["outcome"], "silent_checked")

    def test_incomplete_survives_process_restart(self):
        self.run_cron(tools=[("terminal", "ok")], end=False, skip_llm=True)
        fresh = self.make_ctx()  # new plugin instance, empty memory, same state dir
        self.clock.now += 600
        self.run_cron(ctx=fresh, sid="cron_watch_20261121_091000", response="ok report", tools=[("terminal", "ok")])
        self.assertEqual([r["outcome"] for r in self.receipts()], ["incomplete", "delivered"])

    def test_incomplete_is_per_job(self):
        self.run_cron(sid="cron_other_20261121_090000", end=False, skip_llm=True)
        self.run_cron(sid="cron_watch_20261121_091000", response="hello", tools=[("terminal", "ok")])
        self.assertEqual([r["outcome"] for r in self.receipts()], ["delivered"])

    def test_incomplete_not_reported_twice(self):
        self.run_cron(sid="cron_watch_20261121_090000", end=False, skip_llm=True)
        self.run_cron(sid="cron_watch_20261121_100000", response="hi", tools=[("terminal", "ok")])
        self.run_cron(sid="cron_watch_20261121_110000", response="hi", tools=[("terminal", "ok")])
        self.assertEqual([r["outcome"] for r in self.receipts()], ["incomplete", "delivered", "delivered"])

    def test_late_end_after_incomplete_adds_nothing(self):
        self.ctx.hooks["on_session_start"](session_id="cron_watch_20261121_090000", platform="cron")
        self.run_cron(sid="cron_watch_20261121_100000", response="hi", tools=[("terminal", "ok")])
        self.ctx.hooks["on_session_end"](session_id="cron_watch_20261121_090000", completed=True)
        self.assertEqual([r["outcome"] for r in self.receipts()], ["incomplete", "delivered"])


class DetectionTests(Base):
    def test_interactive_sessions_are_ignored(self):
        for platform, sid in (("cli", "20261121_090000_abc123"), ("telegram", "s2"), ("subagent", "sub1"),
                              ("discord", "cron_looks_like_20261121_090000")):
            self.run_cron(sid=sid, platform=platform, tools=[("terminal", "error")], response="[SILENT]")
        self.assertEqual(self.receipts(), [])
        self.assertFalse(os.path.exists(os.path.join(self.state, "open")) and
                         os.listdir(os.path.join(self.state, "open")))

    def test_platform_cron_with_opaque_session_id(self):
        self.run_cron(sid="local-5f3a9c", platform="cron", tools=[("terminal", "ok")], response="[SILENT]")
        (r,) = self.receipts()
        self.assertEqual(r["session_id"], "local-5f3a9c")
        self.assertIsNone(r["job_id"])

    def test_missing_platform_falls_back_to_session_id_convention(self):
        self.run_cron(sid="cron_abc123_20261121_090000", platform="", tools=[("terminal", "ok")], response="hi")
        self.assertEqual(self.receipts()[0]["job_id"], "abc123")

    def test_job_id_prefers_host_execution_identity(self):
        ctx = self.make_ctx(job_id="job-from-host")
        self.run_cron(ctx=ctx, sid="local-1", tools=[("terminal", "ok")], response="hi")
        self.assertEqual(self.receipts()[0]["job_id"], "job-from-host")

    def test_job_id_parsed_from_session_id(self):
        self.run_cron(sid="cron_daily-brief_20261121_090000", tools=[("terminal", "ok")], response="hi")
        self.assertEqual(self.receipts()[0]["job_id"], "daily-brief")

    def test_tool_calls_from_other_sessions_are_not_counted(self):
        h = self.ctx.hooks
        h["on_session_start"](session_id="cron_watch_20261121_090000", platform="cron")
        h["post_tool_call"](tool_name="terminal", session_id="some-subagent", status="error", result="{}")
        h["post_tool_call"](tool_name="terminal", session_id="", status="error", result="{}")
        h["post_llm_call"](session_id="cron_watch_20261121_090000", assistant_response="[SILENT]")
        h["on_session_end"](session_id="cron_watch_20261121_090000", completed=True)
        self.assertEqual(self.receipts()[0]["outcome"], "silent_unchecked")

    def test_two_concurrent_cron_runs_do_not_mix(self):
        h = self.ctx.hooks
        a, b = "cron_a_20261121_090000", "cron_b_20261121_090000"
        h["on_session_start"](session_id=a, platform="cron")
        h["on_session_start"](session_id=b, platform="cron")
        h["post_tool_call"](tool_name="terminal", session_id=a, status="ok", result="{}")
        h["post_tool_call"](tool_name="web_search", session_id=b, status="error", result="{}")
        for sid in (a, b):
            h["post_llm_call"](session_id=sid, assistant_response="[SILENT]")
            h["on_session_end"](session_id=sid, completed=True)
        by_job = {r["job_id"]: r for r in self.receipts()}
        self.assertEqual(by_job["a"]["outcome"], "silent_checked")
        self.assertEqual(by_job["b"]["outcome"], "silent_with_errors")


class CountingTests(Base):
    def test_per_tool_counts_and_names(self):
        self.run_cron(tools=[("terminal", "ok"), ("terminal", "error"), ("web_search", "ok")], response="hi")
        r = self.receipts()[0]
        self.assertEqual(r["tool_stats"], {"terminal": {"calls": 2, "errors": 1}, "web_search": {"calls": 1, "errors": 0}})
        self.assertEqual(r["tools"], ["terminal", "web_search"])
        self.assertEqual((r["tool_calls"], r["tool_errors"]), (3, 1))

    def test_error_shape_used_when_host_gives_no_status(self):
        h = self.ctx.hooks
        sid = "cron_watch_20261121_090000"
        h["on_session_start"](session_id=sid, platform="cron")
        for result in ('{"error": "boom"}', '{"success": false}', '{"exit_code": 2, "output": ""}',
                       '{"output": "fine", "exit_code": 0}', "plain text", None):
            h["post_tool_call"](tool_name="t", session_id=sid, result=result)
        h["post_llm_call"](session_id=sid, assistant_response="[SILENT]")
        h["on_session_end"](session_id=sid, completed=True)
        r = self.receipts()[0]
        self.assertEqual((r["tool_calls"], r["tool_errors"]), (6, 3))

    def test_host_status_wins_over_result_shape(self):
        self.assertFalse(core.result_is_error("ok", '{"error": "guardrail refusal"}'))
        self.assertTrue(core.result_is_error("error", "not json"))

    def test_arguments_and_results_never_stored(self):
        self.run_cron(tools=[("terminal", "ok")], response="[SILENT]")
        for dirpath, _d, files in os.walk(self.state):
            for name in files:
                text = Path(dirpath, name).read_text(errors="replace")
                self.assertNotIn("ARG-SECRET", text)
                self.assertNotIn("RESULT-SECRET", text)

    def test_odd_tool_names_are_not_stored_verbatim(self):
        self.run_cron(tools=[("rm -rf / ; secret=abc", "ok")], response="hi")
        self.assertEqual(self.receipts()[0]["tools"], ["unknown"])

    def test_timing_fields(self):
        self.run_cron(tools=[("terminal", "ok")], response="hi", duration=12.5)
        r = self.receipts()[0]
        self.assertEqual(r["started_at"], core.iso(T0))
        self.assertEqual(r["ended_at"], core.iso(T0 + 12.5))
        self.assertAlmostEqual(r["duration_s"], 12.5)


class ReasonTests(Base):
    def test_default_and_invalid_reason_config_store_metadata_only(self):
        for index, settings in enumerate(({}, {"store_reason": "not a boolean"})):
            self.run_cron(ctx=self.make_ctx(**settings), sid=f"cron_watch_20261121_09000{index}",
                          response="[SILENT] sensitive fixture text")
        self.assertEqual([r["outcome"] for r in self.receipts()], ["silent_unchecked"] * 2)
        self.assertNotIn("sensitive fixture text", self.store().receipts_path.read_text())
        self.assertFalse(any("reason" in r for r in self.receipts()))

    def test_reason_never_exposed_by_registered_queries_even_for_legacy_rows(self):
        ctx = self.make_ctx(store_reason=True)
        self.run_cron(ctx=ctx, response="[SILENT] opt-in fixture text")
        legacy = dict(self.receipts()[0], session_id="legacy", reason="legacy fixture text")
        with self.store().receipts_path.open("a") as fh:
            fh.write(json.dumps(legacy) + "\n")

        for reader in (self.ctx, ctx):
            for action in ("last", "job", "suspicious"):
                with self.subTest(opt_in=reader is ctx, action=action):
                    args = {"action": action, "job_id": "watch"}
                    out = json.loads(reader.tools["run_receipts_query"][1](args))
                    self.assertEqual(out["count"], 2)
                    self.assertFalse(any("reason" in r for r in out["receipts"]))
                    cmd = action + (" watch" if action == "job" else "")
                    self.assertNotIn("fixture text", reader.commands["receipts"](cmd))
        self.assertEqual([r["reason"] for r in self.receipts()],
                         ["[SILENT] opt-in fixture text", "legacy fixture text"])

    def test_reason_stored_on_silent_only_and_truncated(self):
        long = "[SILENT] " + "x" * 500
        ctx = self.make_ctx(store_reason=True)
        self.run_cron(ctx=ctx, tools=[("terminal", "ok")], response=long)
        self.run_cron(ctx=ctx, sid="cron_watch_20261121_100000", tools=[("terminal", "ok")],
                      response="Real report " + "y" * 300)
        silent, delivered = self.receipts()
        self.assertEqual(len(silent["reason"]), 200)
        self.assertTrue(silent["reason"].startswith("[SILENT] xxx"))
        self.assertNotIn("reason", delivered)

    def test_reason_not_stored_on_failed(self):
        self.run_cron(ctx=self.make_ctx(store_reason=True), tools=[("terminal", "ok")],
                      response="[CRON_FAILURE]\nprivate detail")
        self.assertNotIn("reason", self.receipts()[0])

    def test_store_reason_off(self):
        ctx = self.make_ctx(store_reason=False)
        self.run_cron(ctx=ctx, tools=[("terminal", "ok")], response="[SILENT] nothing new")
        r = self.receipts()[0]
        self.assertEqual(r["outcome"], "silent_checked")
        self.assertNotIn("reason", r)
        self.assertNotIn("nothing new", Path(self.state, "receipts.jsonl").read_text())


class StorageTests(Base):
    # The bytes are UTF-8, but the JSON escape decodes to a lone surrogate.
    _UNWRITABLE_TAIL = (b'{"ended_at": "2027-01-15T08:00:05Z", "outcome": "delivered", '
                        b'"session_id": "broken", "profile": "\\ud800"}')

    def test_cleanup_failure_after_append_does_not_duplicate_on_restart(self):
        sid = "cron_watch_20261121_090000"
        checkpoint = self.store()._open_path(sid)
        unlink = Path.unlink

        def fail_checkpoint(path, *args, **kwargs):
            if path == checkpoint:
                raise OSError("checkpoint cleanup failed")
            return unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", fail_checkpoint):
            with self.assertLogs(plugin.log, level="WARNING"):
                self.run_cron(sid=sid)
        self.assertTrue(checkpoint.exists())
        self.assertEqual([r["outcome"] for r in self.receipts()], ["delivered"])

        self.run_cron(ctx=self.make_ctx(), sid="cron_watch_20261121_100000")
        self.assertEqual([(r["session_id"], r["outcome"]) for r in self.receipts()],
                         [(sid, "delivered"), ("cron_watch_20261121_100000", "delivered")])
        self.assertFalse(checkpoint.exists())

    def test_persistent_checkpoint_cleanup_failure_does_not_drop_other_receipts(self):
        a, b, c = "cron_a_20261121_090000", "cron_a_20261121_100000", "cron_b_20261121_090000"
        checkpoint = self.store()._open_path(a)
        unlink = Path.unlink

        def fail_checkpoint(path, *args, **kwargs):
            if path == checkpoint:
                raise OSError("checkpoint cleanup failed")
            return unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", fail_checkpoint):
            with self.assertLogs(plugin.log, level="WARNING"):
                self.run_cron(sid=a)
                self.run_cron(ctx=self.make_ctx(), sid=b)
                self.assertEqual([r["session_id"] for r in self.receipts()], [a, b])
                self.clock.now += 31 * DAY
                self.run_cron(sid=c)
        self.assertEqual([(r["session_id"], r["outcome"]) for r in self.receipts()],
                         [(a, "delivered"), (c, "delivered")])
        self.assertTrue(checkpoint.exists())
        self.run_cron(ctx=self.make_ctx(), sid="cron_a_20261121_110000")
        self.assertEqual([r["session_id"] for r in self.receipts()], [c, "cron_a_20261121_110000"])
        self.assertFalse(checkpoint.exists())

    def test_short_write_is_completed_before_removing_checkpoint(self):
        sid = "cron_watch_20261121_090000"
        write = os.write
        write_sizes = []

        def write_in_chunks(fd, data):
            count = write(fd, data[:31])
            write_sizes.append((count, len(data)))
            return count

        with mock.patch.object(core.os, "write", write_in_chunks):
            self.run_cron(sid=sid)
        self.assertLess(*write_sizes[0])  # the first real write was short
        self.assertEqual([(r["session_id"], r["outcome"]) for r in self.store().read_all()],
                         [(sid, "delivered")])
        self.assertFalse(self.store()._open_path(sid).exists())

    def test_complete_legacy_receipt_without_newline_survives_next_run(self):
        # Exact bytes captured from 4e5377e with a one-byte short os.write.
        legacy = (b'{"duration_s": 5.0, "ended_at": "2027-01-15T08:00:05Z", "job_id": "watch", '
                  b'"outcome": "delivered", "profile": "tester", "session_id": "cron_watch_20261121_090000", '
                  b'"started_at": "2027-01-15T08:00:00Z", "tool_calls": 0, "tool_errors": 0, '
                  b'"tool_stats": {}, "tools": [], "v": 1}')
        row = json.loads(legacy)
        # A named job prunes on start; an opaque ID without a job first repairs the tail on append.
        for sid in ("cron_watch_20261121_100000", "opaque-next-session"):
            with self.subTest(sid=sid):
                self.clock.now = T0 + 5
                path = self.store().receipts_path
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(legacy)
                ctx = self.make_ctx()
                with self.subTest(phase="read before write"):
                    query = ctx.tools["run_receipts_query"][1]
                    self.assertEqual(json.loads(query({"action": "last"}))["receipts"], [row])
                    self.assertEqual(path.read_bytes(), legacy)
                with self.subTest(phase="next run"):
                    self.run_cron(ctx=ctx, sid=sid)
                    lines = path.read_bytes().splitlines(keepends=True)
                    self.assertEqual(len(lines), 2)
                    self.assertTrue(all(line.endswith(b"\n") for line in lines))
                    previous, latest = map(json.loads, lines)
                    self.assertEqual(previous, row)
                    self.assertEqual((latest["session_id"], latest["outcome"]), (sid, "delivered"))

    def test_torn_tail_during_another_run_does_not_erase_its_receipt(self):
        a, b = "cron_a_20261121_090000", "cron_b_20261121_090000"
        tails = {
            "truncated-json": b'{"outcome": "torn"',
            "invalid-utf8": (b'{"ended_at": "2027-01-15T08:00:05Z", "outcome": "delivered", '
                             b'"session_id": "broken", "profile": "\xed\xa0\x80"}'),
        }
        for name, tail in tails.items():
            with self.subTest(tail=name):
                self.state = os.path.join(self.tmp, name)
                ctx = self.make_ctx()
                self.run_cron(ctx=ctx, sid=a)
                self.run_cron(ctx=ctx, sid=b, end=False)
                path = self.store().receipts_path
                with path.open("ab") as fh:
                    fh.write(tail)
                ctx.hooks["on_session_end"](session_id=b, completed=True)
                self.assertNotIn(tail, path.read_bytes())
                self.assertEqual([(r["session_id"], r["outcome"]) for r in self.receipts()],
                                 [(a, "delivered"), (b, "delivered")])
                self.assertFalse(self.store()._open_path(b).exists())

    def test_escaped_surrogate_tail_before_run_does_not_block_named_receipts(self):
        a, b, c = ("cron_watch_20261121_090000", "cron_watch_20261121_100000",
                   "cron_watch_20261121_110000")
        self.run_cron(sid=a)
        path = self.store().receipts_path
        with path.open("ab") as fh:
            fh.write(self._UNWRITABLE_TAIL)

        self.run_cron(ctx=self.make_ctx(), sid=b)
        self.run_cron(ctx=self.make_ctx(), sid=c)
        self.assertEqual([(r["session_id"], r["outcome"]) for r in self.receipts()],
                         [(a, "delivered"), (b, "delivered"), (c, "delivered")])
        self.assertNotIn(self._UNWRITABLE_TAIL, path.read_bytes())

    def test_escaped_surrogate_tail_during_run_is_discarded_on_append(self):
        a, b = "cron_watch_20261121_090000", "cron_watch_20261121_100000"
        # A later key must not hide malformed content and claim an active session's receipt.
        shadowed = (self._UNWRITABLE_TAIL.replace(b"broken", b.encode("ascii"))[:-1]
                    + b', "profile": "tester"}')
        for name, tail in (("lone-surrogate", self._UNWRITABLE_TAIL), ("shadowed-surrogate", shadowed)):
            with self.subTest(tail=name):
                self.state = os.path.join(self.tmp, name)
                self.clock.now = T0
                ctx = self.make_ctx()
                self.run_cron(ctx=ctx, sid=a)
                self.run_cron(ctx=ctx, sid=b, end=False)
                path = self.store().receipts_path
                with path.open("ab") as fh:
                    fh.write(tail)

                before_prune = []
                prune = core.Store._prune_locked

                def observe_prune(store):
                    before_prune.append(path.read_bytes())
                    return prune(store)

                with mock.patch.object(core.Store, "_prune_locked", observe_prune):
                    ctx.hooks["on_session_end"](session_id=b, completed=True)

                # Pruning also discards malformed rows, so check the append result first.
                self.assertEqual(len(before_prune), 1)
                self.assertNotIn(tail, before_prune[0])
                rows = map(json.loads, before_prune[0].splitlines())
                self.assertEqual([(r["session_id"], r["outcome"]) for r in rows],
                                 [(a, "delivered"), (b, "delivered")])
                self.assertEqual(path.read_bytes(), before_prune[0])
                self.assertFalse(self.store()._open_path(b).exists())

    def test_late_end_in_another_recorder_cannot_duplicate_stale_receipt(self):
        a, b = "cron_watch_20261121_090000", "cron_watch_20261121_100000"
        self.run_cron(sid=a, end=False)
        self.run_cron(ctx=self.make_ctx(), sid=b)
        self.ctx.hooks["on_session_end"](session_id=a, completed=True)
        self.assertEqual([(r["session_id"], r["outcome"]) for r in self.receipts()],
                         [(a, "incomplete"), (b, "delivered")])

    def test_files_are_0600_and_dirs_0700(self):
        self.run_cron(tools=[("terminal", "ok")], response="hi", end=False, skip_llm=True)
        self.run_cron(sid="cron_watch_20261121_100000", tools=[("terminal", "ok")], response="hi")
        modes = {}
        for dirpath, dirs, files in os.walk(self.state):
            for n in files:
                modes[os.path.join(dirpath, n)] = stat.S_IMODE(os.stat(os.path.join(dirpath, n)).st_mode)
            for n in dirs:
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(dirpath, n)).st_mode), 0o700)
        self.assertTrue(modes)
        self.assertIn(os.path.join(self.state, "receipts.jsonl"), modes)
        for path, mode in modes.items():
            self.assertEqual(mode, 0o600, path)
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode), 0o700)

    def test_loose_existing_modes_are_tightened(self):
        os.makedirs(self.state)
        path = os.path.join(self.state, "receipts.jsonl")
        Path(path).write_text("")
        os.chmod(path, 0o644)
        os.chmod(self.state, 0o755)
        self.run_cron(tools=[("terminal", "ok")], response="hi")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode), 0o700)

    def test_state_dir_setting_is_honoured(self):
        other = os.path.join(self.tmp, "elsewhere")
        ctx = self.make_ctx(state_dir=other)
        self.run_cron(ctx=ctx, tools=[("terminal", "ok")], response="hi")
        self.assertTrue(os.path.exists(os.path.join(other, "receipts.jsonl")))
        self.assertEqual(os.path.realpath(os.path.join(other, "receipts.jsonl")),
                         os.path.join(self.tmp, "elsewhere", "receipts.jsonl"))

    def test_default_state_dir_follows_hermes_home(self):
        with mock.patch.dict(os.environ, {"HERMES_HOME": self.tmp}):
            with mock.patch.dict(sys.modules, {"hermes_constants": None}):
                d = core.default_state_dir()
        self.assertEqual(os.path.realpath(str(d)), os.path.join(self.tmp, "run-receipts"))

    def test_symlinked_receipts_file_is_refused(self):
        os.makedirs(self.state)
        target = os.path.join(self.tmp, "victim.txt")
        Path(target).write_text("keep")
        os.symlink(target, os.path.join(self.state, "receipts.jsonl"))
        with self.assertLogs(plugin.log, level="WARNING"):
            self.run_cron(tools=[("terminal", "ok")], response="hi")  # fail-open: must not raise
        self.assertEqual(Path(target).read_text(), "keep")

    def test_symlinked_state_dir_is_refused(self):
        real = os.path.join(self.tmp, "real")
        os.makedirs(real)
        os.symlink(real, self.state)
        with self.assertLogs(plugin.log, level="WARNING"):
            self.run_cron(tools=[("terminal", "ok")], response="hi")
        self.assertEqual(os.listdir(real), [])

    def test_open_file_removed_after_clean_end(self):
        self.run_cron(tools=[("terminal", "ok")], response="hi")
        self.assertEqual(os.listdir(os.path.join(self.state, "open")), [])


class PruneTests(Base):
    def seed(self, days_ago, outcome="delivered", job="old"):
        t = T0 - days_ago * DAY
        rec = {"v": 1, "job_id": job, "session_id": f"s-{days_ago}", "started_at": core.iso(t - 5),
               "ended_at": core.iso(t), "duration_s": 5.0, "tool_calls": 1, "tool_errors": 0, "tools": ["t"],
               "tool_stats": {}, "outcome": outcome}
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "receipts.jsonl"), "a") as fh:
            fh.write(json.dumps(rec) + "\n")

    def test_prune_on_write_default_30_days(self):
        self.seed(45)
        self.seed(31)
        self.seed(29)
        self.run_cron(tools=[("terminal", "ok")], response="hi")
        self.assertEqual([r["session_id"] for r in self.receipts()], ["s-29", "cron_watch_20261121_090000"])

    def test_expired_receipt_with_invalid_session_id_does_not_stop_new_runs(self):
        self.seed(1)
        ended = T0 - 31 * DAY
        bad = core.receipt_from_run(core.new_run("\ud800", "old", "tester", ended - 5), ended, "delivered")
        # Escaping the invalid ID makes the row parseable JSON, but the ID cannot be hashed as UTF-8.
        with self.store().receipts_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(bad, ensure_ascii=True) + "\n")

        first, second = "cron_watch_20261121_090000", "cron_watch_20261121_100000"
        self.run_cron(sid=first)
        self.run_cron(ctx=self.make_ctx(), sid=second)
        self.assertEqual([(r["session_id"], r["outcome"]) for r in self.receipts()],
                         [("s-1", "delivered"), (first, "delivered"), (second, "delivered")])

    def test_keep_days_configurable(self):
        ctx = self.make_ctx(keep_days=2)
        self.seed(3)
        self.seed(1)
        self.run_cron(ctx=ctx, tools=[("terminal", "ok")], response="hi")
        self.assertEqual([r["session_id"] for r in self.receipts()], ["s-1", "cron_watch_20261121_090000"])

    def test_bad_keep_days_falls_back_to_default(self):
        for bad in (0, -5, "soon", True, None):
            s = core.load_settings(lambda k, d=None, bad=bad: bad if k == "keep_days" else d)
            self.assertEqual(s["keep_days"], 30.0, bad)

    def test_prune_drops_corrupt_lines_and_keeps_good(self):
        self.seed(1)
        with open(os.path.join(self.state, "receipts.jsonl"), "a") as fh:
            fh.write("{not json\n")
        self.run_cron(tools=[("terminal", "ok")], response="hi")
        self.assertEqual(len(self.receipts()), 2)
        self.assertNotIn("not json", Path(self.state, "receipts.jsonl").read_text())

    def test_pruned_file_stays_0600(self):
        self.seed(60)
        self.run_cron(tools=[("terminal", "ok")], response="hi")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.state, "receipts.jsonl")).st_mode), 0o600)


class FailOpenTests(Base):
    def test_hooks_return_none_and_never_raise_on_garbage(self):
        for name, cb in self.ctx.hooks.items():
            for kwargs in ({}, {"session_id": None}, {"session_id": 5, "platform": 7, "result": object()},
                           {"session_id": "cron_x_20261121_090000", "platform": "cron", "assistant_response": 12}):
                self.assertIsNone(cb(**kwargs), name)

    def test_unwritable_state_dir_does_not_raise(self):
        blocker = os.path.join(self.tmp, "blocker")
        Path(blocker).write_text("a file, not a directory")
        ctx = self.make_ctx(state_dir=os.path.join(blocker, "sub"))
        with self.assertLogs(plugin.log, level="WARNING"):
            self.run_cron(ctx=ctx, tools=[("terminal", "ok")], response="[SILENT]")

    def test_exception_inside_hook_body_is_caught_and_logged(self):
        with mock.patch.object(core, "classify", side_effect=RuntimeError("kaboom")):
            with self.assertLogs(plugin.log, level="WARNING") as cm:
                self.run_cron(tools=[("terminal", "ok")], response="hi")
        self.assertTrue(any("kaboom" in m for m in cm.output))

    def test_broken_config_reader_does_not_raise(self):
        ctx = FakeCtx()
        ctx.get_config = mock.Mock(side_effect=RuntimeError("config gone"))
        plugin.register(ctx)
        with mock.patch.dict(os.environ, {"HERMES_HOME": self.tmp}):
            self.run_cron(ctx=ctx, tools=[("terminal", "ok")], response="hi")
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "run-receipts", "receipts.jsonl")))

    def test_query_surfaces_swallow_errors(self):
        blocker = os.path.join(self.tmp, "blocker")
        Path(blocker).write_text("x")
        ctx = self.make_ctx(state_dir=os.path.join(blocker, "sub"))
        with self.assertLogs(plugin.log, level="WARNING"):
            self.assertIn("unavailable", ctx.commands["receipts"]("last"))
            out = json.loads(ctx.tools["run_receipts_query"][1]({"action": "last"}))
        self.assertIn("error", out)  # an unreadable store is reported, not shown as "no receipts"

    def test_a_failed_write_does_not_lose_the_run_slot(self):
        # the end hook raising must not leave state that makes the next run misbehave
        with mock.patch.object(core.Store, "_append_line", side_effect=OSError("disk full")):
            with self.assertLogs(plugin.log, level="WARNING"):
                self.run_cron(tools=[("terminal", "ok")], response="hi")
        self.run_cron(sid="cron_watch_20261121_100000", tools=[("terminal", "ok")], response="hi")
        self.assertEqual(self.receipts()[-1]["outcome"], "delivered")


class QueryTests(Base):
    def populate(self):
        self.run_cron(sid="cron_a_20261121_000001", tools=[("terminal", "ok")], response="[SILENT]")        # checked
        self.run_cron(sid="cron_b_20261121_000002", tools=[], response="[SILENT]")                            # unchecked
        self.run_cron(sid="cron_c_20261121_000003", tools=[("terminal", "error")], response="[SILENT]")       # with errors
        self.run_cron(sid="cron_d_20261121_000004", tools=[("terminal", "ok")], response="[CRON_FAILURE]\nx")  # failed
        self.run_cron(sid="cron_e_20261121_000005", tools=[("terminal", "ok")], response="News!")             # delivered
        self.run_cron(sid="cron_f_20261121_000006", end=False, skip_llm=True)                                 # open
        self.run_cron(sid="cron_f_20261121_000007", tools=[("terminal", "ok")], response="News!")             # -> incomplete f

    def query(self, **args):
        return json.loads(self.ctx.tools["run_receipts_query"][1](args))

    def test_tool_registered_read_only_schema(self):
        schema, _h = self.ctx.tools["run_receipts_query"]
        self.assertEqual(schema["name"], "run_receipts_query")
        self.assertEqual(schema["parameters"]["properties"]["action"]["enum"], ["last", "job", "suspicious"])

    def test_tool_suspicious_returns_the_right_rows(self):
        self.populate()
        out = self.query(action="suspicious")
        self.assertEqual(sorted((r["job_id"], r["outcome"]) for r in out["receipts"]),
                         [("b", "silent_unchecked"), ("c", "silent_with_errors"), ("d", "failed"), ("f", "incomplete")])
        self.assertEqual(out["count"], 4)

    def test_suspicious_limits_newest_first_in_tool_and_slash(self):
        path = self.store().receipts_path
        path.parent.mkdir()
        with path.open("w") as fh:
            for i in range(205):
                fh.write(json.dumps({"job_id": f"watch-{i}", "outcome": "failed",
                                     "ended_at": core.iso(T0)}) + "\n")
        cmd = self.ctx.commands["receipts"]
        for raw_n, expected in ((None, 10), (1, 1), (1000, 200)):
            with self.subTest(n=raw_n):
                opts = {} if raw_n is None else {"n": raw_n}
                out = self.query(action="suspicious", **opts)
                self.assertEqual(out["count"], expected)
                self.assertEqual([r["job_id"] for r in out["receipts"]],
                                 [f"watch-{i}" for i in range(204, 204 - expected, -1)])
                args = "suspicious" if raw_n is None else f"suspicious {raw_n}"
                self.assertEqual(len(cmd(args).splitlines()), expected)

    def test_suspicious_skips_non_string_outcomes(self):
        self.run_cron(sid="cron_good_20261121_090000", failed=True)
        with self.store().receipts_path.open("a") as fh:
            fh.write(json.dumps({"job_id": "broken", "outcome": [], "ended_at": core.iso(T0)}) + "\n")
        self.assertIn("job=good", self.ctx.commands["receipts"]("suspicious 1"))
        out = self.query(action="suspicious", n=1)
        self.assertEqual([r["job_id"] for r in out["receipts"]], ["good"])

    def test_tool_job_and_last(self):
        self.populate()
        out = self.query(action="job", job_id="f")
        self.assertEqual([r["outcome"] for r in out["receipts"]], ["delivered", "incomplete"])  # newest first
        last = self.query(action="last", n=2)
        self.assertEqual([r["job_id"] for r in last["receipts"]], ["f", "f"])
        self.assertEqual(self.query(action="last")["count"], 7)

    def test_tool_default_action_and_errors(self):
        self.populate()
        self.assertEqual(self.query()["action"], "last")
        self.assertIn("error", self.query(action="job"))
        self.assertIn("error", self.query(action="drop table"))
        self.assertEqual(self.query(action="job", job_id="nope")["count"], 0)

    def test_suspicious_window_is_7_days(self):
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "receipts.jsonl"), "w") as fh:
            for days, job in ((9, "stale"), (6, "recent")):
                t = T0 - days * DAY
                fh.write(json.dumps({"job_id": job, "outcome": "silent_unchecked", "started_at": core.iso(t),
                                     "ended_at": core.iso(t)}) + "\n")
        self.assertEqual([r["job_id"] for r in self.query(action="suspicious")["receipts"]], ["recent"])

    def test_tool_is_read_only(self):
        self.populate()
        before = Path(self.state, "receipts.jsonl").read_bytes()
        mtime = os.stat(os.path.join(self.state, "receipts.jsonl")).st_mtime_ns
        for a in ({"action": "last"}, {"action": "suspicious"}, {"action": "job", "job_id": "a"}):
            self.query(**a)
        self.ctx.commands["receipts"]("suspicious")
        self.assertEqual(Path(self.state, "receipts.jsonl").read_bytes(), before)
        self.assertEqual(os.stat(os.path.join(self.state, "receipts.jsonl")).st_mtime_ns, mtime)

    def test_query_read_paths_refuse_symlinks_without_mutating_storage(self):
        cmd = self.ctx.commands["receipts"]
        self.assertEqual(cmd("last"), "No receipts.")
        self.assertEqual(self.query(action="last")["count"], 0)
        self.assertFalse(Path(self.state).exists())

        target = Path(self.tmp, "target")
        target.mkdir()
        source = target / "receipts.jsonl"
        original = b'{"job_id":"private-fixture","outcome":"delivered"}\n'
        source.write_bytes(original)
        target.chmod(0o755)
        source.chmod(0o644)
        leaf_state = Path(self.tmp, "leaf-state")
        leaf_state.mkdir()
        leaf_state.chmod(0o755)
        (leaf_state / "receipts.jsonl").symlink_to(source)
        Path(self.state).symlink_to(target, target_is_directory=True)

        for state in (self.state, str(leaf_state)):
            ctx = self.make_ctx(state_dir=state)
            with self.subTest(state=state), self.assertLogs(plugin.log, level="WARNING"):
                self.assertIn("unavailable", ctx.commands["receipts"]("last"))
                self.assertEqual(json.loads(ctx.tools["run_receipts_query"][1]({"action": "last"})),
                                 {"error": "receipts unavailable"})
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(leaf_state.stat().st_mode), 0o755)
        self.assertEqual([p.name for p in target.iterdir()], ["receipts.jsonl"])
        self.assertEqual([p.name for p in leaf_state.iterdir()], ["receipts.jsonl"])

    def test_slash_command(self):
        self.populate()
        cmd = self.ctx.commands["receipts"]
        self.assertIn("/receipts last", cmd(""))
        self.assertEqual(len(cmd("last 3").splitlines()), 3)
        sus = cmd("suspicious")
        for word in ("silent_unchecked", "silent_with_errors", "incomplete", "failed"):
            self.assertIn(word, sus)
        self.assertNotIn("silent_checked", sus)
        self.assertEqual(len(cmd("job f").splitlines()), 2)
        self.assertIn("usage", cmd("job"))
        self.assertEqual(cmd("job nobody"), "No receipts.")
        self.assertIn("/receipts last", cmd("bogus"))

    def test_n_is_clamped(self):
        self.assertEqual(core.clamp_n(0), 1)
        self.assertEqual(core.clamp_n(10_000), 200)
        self.assertEqual(core.clamp_n("x"), 10)


class SilentRuleTests(unittest.TestCase):
    SAMPLES = ["[SILENT]", "[silent]", "SILENT", "NO_REPLY", "no reply", "[SILENT] nothing new", "Nothing.\n[SILENT]",
               "Nothing.\n\nNO_REPLY\n", "[静默]", "静默。", "*NO_REPLY*", "I said [SILENT] mid sentence",
               "Silent retry succeeded", "", "   ", "report\nmore report", "[SILENT", None, 5]

    def test_fallback_matches_staged_gateway_matcher_when_present(self):
        path = Path(os.environ.get("HERMES_AGENT_SRC", "~/.hermes/hermes-agent")).expanduser() / "gateway" / "response_filters.py"
        if not path.exists():
            self.skipTest("upstream source not on this host")
        spec = importlib.util.spec_from_file_location("upstream_response_filters", path)
        up = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(up)
        for s in self.SAMPLES:
            self.assertEqual(core._fallback_is_silent(s), up.is_autonomous_silence_response(s), repr(s))

    def test_fallback_known_answers(self):
        yes = ["[SILENT]", "NO_REPLY", "[SILENT] nothing new", "Nothing.\n[SILENT]", "静默。"]
        no = ["I said [SILENT] mid sentence", "Silent retry succeeded", "", "report\nmore report", "[SILENT"]
        for s in yes:
            self.assertTrue(core._fallback_is_silent(s), s)
        for s in no:
            self.assertFalse(core._fallback_is_silent(s), s)

    def test_upstream_matcher_is_used_when_importable(self):
        fake = types.ModuleType("gateway.response_filters")
        fake.is_autonomous_silence_response = lambda text: text == "UPSTREAM-SAYS-SILENT"
        with mock.patch.dict(sys.modules, {"gateway.response_filters": fake, "cron.scheduler": None}):
            self.assertTrue(core.is_silent("UPSTREAM-SAYS-SILENT"))
            self.assertFalse(core.is_silent("[SILENT]"))  # upstream wins over the copy

    def test_cron_scheduler_matchers_are_preferred_when_loaded(self):
        fake = types.ModuleType("cron.scheduler")
        fake._is_cron_silence_response = lambda text: True
        fake._cron_failure_marker_error = lambda text: "x" if text == "bad" else None
        with mock.patch.dict(sys.modules, {"cron.scheduler": fake}):
            self.assertTrue(core.is_silent("anything"))
            self.assertTrue(core.failure_marker("bad"))
            self.assertFalse(core.failure_marker("fine"))

    def test_broken_upstream_falls_back_to_copy(self):
        fake = types.ModuleType("gateway.response_filters")

        def boom(_):
            raise RuntimeError("upstream broke")
        fake.is_autonomous_silence_response = boom
        with mock.patch.dict(sys.modules, {"gateway.response_filters": fake, "cron.scheduler": None}):
            self.assertTrue(core.is_silent("[SILENT]"))
            self.assertFalse(core.is_silent("hello"))

    def test_missing_upstream_falls_back_to_copy(self):
        with mock.patch.dict(sys.modules, {"gateway.response_filters": None, "cron.scheduler": None}):
            self.assertTrue(core.is_silent("[SILENT]"))

    def test_failure_marker_fallback_is_strict(self):
        with mock.patch.dict(sys.modules, {"cron.scheduler": None}):
            self.assertTrue(core.failure_marker("[CRON_FAILURE]\nwhy"))
            self.assertTrue(core.failure_marker("[CRON_FAILURE]"))
            self.assertFalse(core.failure_marker("note\n[CRON_FAILURE]"))
            self.assertFalse(core.failure_marker("quoting [CRON_FAILURE] inline"))


class DocumentationTests(unittest.TestCase):
    def test_evidence_documents_tempdir_and_gateway_only_parity(self):
        text = (ROOT / "docs" / "EVIDENCE.md").read_text()
        self.assertTrue("TMPDIR=" in text, "missing configurable temp-dir example")
        self.assertTrue("~/.hermes/cache/scratch" not in text, "stale hardcoded scratch path")
        self.assertTrue("gateway/response_filters.py" in text and "not the scheduler emitter" in text,
                        "missing gateway-only parity scope")
        self.assertTrue("not a compatibility check across Hermes versions" in text,
                        "missing cross-version scope limit")

    def test_readme_and_tool_describe_observations_not_guarantees(self):
        text = (ROOT / "README.md").read_text()
        expected = ("separate from job output", "not verified transport delivery", "reported by Hermes",
                    "bracketed prefix", "localized markers", "max-iteration summary")
        for phrase in expected:
            with self.subTest(phrase=phrase):
                self.assertTrue(phrase in text, f"README missing: {phrase}")
        self.assertTrue("file the job can't edit" not in text, "README claims tamper resistance")
        desc = plugin.TOOL_SCHEMA["description"]
        self.assertNotIn("proves tools ran without error", desc)
        self.assertIn("not a delivery acknowledgment", desc)


class ManifestTests(unittest.TestCase):
    def test_manifest_defaults_to_metadata_only(self):
        text = (ROOT / "plugin.yaml").read_text()
        self.assertRegex(text, r"(?m)^  store_reason:\n    type: boolean\n    default: false$")

    def test_manifest_matches_registrations(self):
        ctx = FakeCtx()
        plugin.register(ctx)
        text = (ROOT / "plugin.yaml").read_text()
        for hook in ctx.hooks:
            self.assertIn(f"  - {hook}", text)
        self.assertEqual(sorted(ctx.hooks), ["on_session_end", "on_session_start", "post_llm_call", "post_tool_call"])
        self.assertIn("  - run_receipts_query", text)
        self.assertEqual(sorted(ctx.tools), ["run_receipts_query"])
        self.assertEqual(sorted(ctx.commands), ["receipts"])

    def test_no_network_or_subprocess_imports(self):
        for name in ("__init__.py", "core.py"):
            src = (ROOT / name).read_text()
            for banned in ("import socket", "import urllib", "import requests", "import http", "subprocess"):
                self.assertNotIn(banned, src, f"{name}: {banned}")


if __name__ == "__main__":
    unittest.main()
