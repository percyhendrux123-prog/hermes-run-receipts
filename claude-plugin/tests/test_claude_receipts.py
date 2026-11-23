"""Tests for the Claude Code run-receipts plugin (stdlib unittest).

The hook tests run the real hook script as a subprocess with sample hook events on
stdin, the way Claude Code does, and inspect the files it wrote. Field and event names
are the ones in the Claude Code hooks reference. Temporary state lives in a scratch
directory and nothing touches the real ~/.claude.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
PLUGIN = os.path.join(os.path.dirname(HERE), "run-receipts")
SCRIPTS = os.path.join(PLUGIN, "scripts")
HOOK = os.path.join(SCRIPTS, "receipt_hook.py")
QUERY = os.path.join(SCRIPTS, "receipts_query.py")
SCRATCH = os.path.realpath(os.path.expanduser("~/.hermes/cache/scratch"))
if not os.path.isdir(SCRATCH):
    SCRATCH = os.path.realpath(tempfile.gettempdir())

sys.path.insert(0, SCRIPTS)
import receipts_core as core  # noqa: E402

CANARY = "CANARY-9f8e7d6c"
KEYS = {"v", "session_id", "source", "started_at", "ended_at", "duration_s", "end_reason",
        "tool_calls", "tool_errors", "tools", "tool_stats", "outcome", "cause"}


def ev(name, sid="sess-1", **extra):
    payload = {"session_id": sid, "transcript_path": "/home/example/" + CANARY + ".jsonl",
               "cwd": "/home/example/client-" + CANARY, "hook_event_name": name}
    payload.update(extra)
    return payload


def start(sid="sess-1", source="startup"):
    return ev("SessionStart", sid, source=source, model="claude-x")


def tool(name="Bash", sid="sess-1", **extra):
    return ev("PostToolUse", sid, tool_name=name,
              tool_input={"command": "echo " + CANARY}, tool_response={"stdout": CANARY},
              tool_use_id="toolu_01", **extra)


def tool_fail(name="Bash", sid="sess-1", **extra):
    return ev("PostToolUseFailure", sid, tool_name=name,
              tool_input={"command": "echo " + CANARY}, error="Exit code 1\n" + CANARY,
              is_interrupt=False, tool_use_id="toolu_02", **extra)


def stop(sid="sess-1", message="done", **extra):
    return ev("Stop", sid, stop_hook_active=False, last_assistant_message=message + " " + CANARY if message else message, **extra)


def stop_failure(error="rate_limit", sid="sess-1"):
    return ev("StopFailure", sid, error=error, error_details=CANARY,
              last_assistant_message="API Error: " + CANARY)


def end(sid="sess-1", reason="other"):
    return ev("SessionEnd", sid, reason=reason)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(dir=SCRATCH, prefix="rr-claude-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = os.path.join(self.tmp, "state")
        self.file = os.path.join(self.state, "receipts.jsonl")

    def env(self, **extra):
        env = dict(os.environ)
        for key in ("RUN_RECEIPTS_DIR", "RUN_RECEIPTS_KEEP_DAYS", "RUN_RECEIPTS_STALE_HOURS",
                    "CLAUDE_CONFIG_DIR"):
            env.pop(key, None)
        env["HOME"] = self.tmp  # never touch the real ~/.claude
        env["RUN_RECEIPTS_DIR"] = self.state
        env.update(extra)
        return env

    def hook(self, payload, **env_extra):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        return subprocess.run([sys.executable, HOOK], input=data, capture_output=True,
                              env=self.env(**env_extra), timeout=30)

    def play(self, *events, **env_extra):
        for event in events:
            done = self.hook(event, **env_extra)
            self.assertEqual(done.returncode, 0)
            self.assertEqual(done.stdout, b"")
            self.assertEqual(done.stderr, b"")

    def rows(self):
        if not os.path.exists(self.file):
            return []
        with open(self.file, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle.read().splitlines()]

    def one(self):
        rows = self.rows()
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def query(self, *args, **env_extra):
        done = subprocess.run([sys.executable, QUERY, *args], capture_output=True,
                              env=self.env(**env_extra), timeout=30)
        return done, (json.loads(done.stdout) if done.stdout else None)

    def open_files(self):
        folder = os.path.join(self.state, "open")
        return sorted(os.listdir(folder)) if os.path.isdir(folder) else []


class TestOutcomes(Base):
    def test_completed_with_tools(self):
        self.play(start(), tool("Bash"), tool("Read"), tool("Bash"), stop(), end())
        row = self.one()
        self.assertEqual(row["outcome"], "completed")
        self.assertEqual((row["tool_calls"], row["tool_errors"]), (3, 0))
        self.assertEqual(row["tools"], ["Bash", "Read"])
        self.assertEqual(row["tool_stats"], {"Bash": {"calls": 2, "errors": 0}, "Read": {"calls": 1, "errors": 0}})
        self.assertEqual(row["session_id"], "sess-1")
        self.assertEqual(row["source"], "startup")
        self.assertEqual(row["end_reason"], "other")
        self.assertNotIn("cause", row)
        self.assertEqual(self.open_files(), [])

    def test_completed_no_tools(self):
        self.play(start(), stop(), end())
        row = self.one()
        self.assertEqual(row["outcome"], "completed_no_tools")
        self.assertEqual((row["tool_calls"], row["tools"]), (0, []))

    def test_completed_with_errors(self):
        self.play(start(), tool("Bash"), tool_fail("Bash"), stop(), end())
        row = self.one()
        self.assertEqual(row["outcome"], "completed_with_errors")
        self.assertEqual((row["tool_calls"], row["tool_errors"]), (2, 1))
        self.assertEqual(row["tool_stats"]["Bash"], {"calls": 2, "errors": 1})

    def test_failed_on_stop_failure(self):
        self.play(start(), tool(), stop_failure("rate_limit"), end())
        row = self.one()
        self.assertEqual((row["outcome"], row["cause"]), ("failed", "rate_limit"))

    def test_unknown_failure_type_is_stored_as_unknown(self):
        self.play(start(), stop_failure("made up " + CANARY), end())
        row = self.one()
        self.assertEqual((row["outcome"], row["cause"]), ("failed", "unknown"))

    def test_failed_on_empty_final_response(self):
        self.play(start(), tool(), stop(message=""), end())
        row = self.one()
        self.assertEqual((row["outcome"], row["cause"]), ("failed", "empty_response"))

    def test_missing_message_field_is_not_called_empty(self):
        event = stop()
        del event["last_assistant_message"]
        self.play(start(), event, end())
        self.assertEqual(self.one()["outcome"], "completed_no_tools")

    def test_later_good_turn_replaces_earlier_failure(self):
        self.play(start(), stop_failure("server_error"), tool(), stop(), end())
        self.assertEqual(self.one()["outcome"], "completed")

    def test_later_failure_replaces_earlier_good_turn(self):
        self.play(start(), tool(), stop(), stop_failure("server_error"), end())
        self.assertEqual(self.one()["outcome"], "failed")

    def test_no_turn_when_session_ends_without_a_finished_turn(self):
        self.play(start(), end(reason="prompt_input_exit"))
        row = self.one()
        self.assertEqual((row["outcome"], row["cause"]), ("no_turn", "no_turn_completed"))
        self.assertEqual(row["end_reason"], "prompt_input_exit")

    def test_tool_calls_without_a_finished_turn_is_no_turn_with_counts(self):
        self.play(start(), tool(), tool(), end())
        row = self.one()
        self.assertEqual((row["outcome"], row["tool_calls"]), ("no_turn", 2))

    def test_odd_end_reason_becomes_other(self):
        self.play(start(), stop(), end(reason="surprise " + CANARY))
        self.assertEqual(self.one()["end_reason"], "other")

    def test_duration_and_timestamps(self):
        self.play(start(), stop(), end())
        row = self.one()
        self.assertRegex(row["started_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertRegex(row["ended_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertGreaterEqual(row["duration_s"], 0)
        self.assertLess(row["duration_s"], 30)


class TestSessionHandling(Base):
    def test_two_sessions_are_kept_apart(self):
        self.play(start("a"), start("b"), tool("Bash", "a"), tool("Read", "b"), tool("Read", "b"),
                  stop(sid="a"), stop(sid="b"), end("b"), end("a"))
        by_id = {r["session_id"]: r for r in self.rows()}
        self.assertEqual(by_id["a"]["tool_calls"], 1)
        self.assertEqual(by_id["b"]["tool_calls"], 2)

    def test_subagent_activity_is_not_counted(self):
        self.play(start(), tool("Bash"), tool("Read", agent_id="def456", agent_type="Explore"),
                  tool_fail("Grep", agent_id="def456", agent_type="Explore"),
                  stop(agent_id="def456", agent_type="Explore"), end())
        row = self.one()
        self.assertEqual((row["outcome"], row["tool_calls"], row["tool_errors"]), ("no_turn", 1, 0))

    def test_session_end_with_no_start_writes_nothing(self):
        self.play(end("never-started"))
        self.assertEqual(self.rows(), [])

    def test_tool_event_with_no_start_creates_nothing(self):
        self.play(tool(sid="ghost"))
        self.assertEqual(self.open_files(), [])
        self.assertEqual(self.rows(), [])

    def test_compact_keeps_counts_and_writes_one_receipt(self):
        self.play(start(), tool(), start(source="compact"), tool(), stop(), end())
        row = self.one()
        self.assertEqual((row["tool_calls"], row["source"]), (2, "startup"))

    def test_restart_of_an_unended_session_closes_the_old_record_as_incomplete(self):
        self.play(start(), tool(), start(source="resume"), tool(), tool(), stop(), end())
        old, new = self.rows()
        self.assertEqual((old["outcome"], old["cause"], old["tool_calls"]), ("incomplete", "no_end_seen", 1))
        self.assertNotIn("ended_at", old)
        self.assertEqual((new["outcome"], new["source"], new["tool_calls"]), ("completed", "resume", 2))

    def test_stale_open_session_becomes_incomplete_on_next_start(self):
        self.play(start("old"), tool("Bash", "old"))
        path = os.path.join(self.state, "open", "old.jsonl")
        past = time.time() - 24 * 3600
        os.utime(path, (past, past))
        self.play(start("new"), stop(sid="new"), end("new"))
        rows = {r["session_id"]: r for r in self.rows()}
        self.assertEqual(rows["old"]["outcome"], "incomplete")
        self.assertEqual(rows["old"]["tool_calls"], 1)
        self.assertEqual(rows["new"]["outcome"], "completed_no_tools")
        self.assertEqual(self.open_files(), [])

    def test_fresh_open_session_is_left_alone(self):
        self.play(start("running"), start("other"))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.open_files(), ["other.jsonl", "running.jsonl"])

    def test_stale_hours_setting(self):
        self.play(start("old"))
        path = os.path.join(self.state, "open", "old.jsonl")
        past = time.time() - 2 * 3600
        os.utime(path, (past, past))
        self.play(start("new"))
        self.assertEqual(self.rows(), [])  # default 12h: not stale yet
        self.play(start("newer"), RUN_RECEIPTS_STALE_HOURS="1")
        self.assertEqual([r["session_id"] for r in self.rows()], ["old"])

    def test_late_end_after_incomplete_writes_nothing_more(self):
        self.play(start("old"), tool(sid="old"))
        past = time.time() - 24 * 3600
        os.utime(os.path.join(self.state, "open", "old.jsonl"), (past, past))
        self.play(start("new"), end("old"))
        self.assertEqual([r["outcome"] for r in self.rows()], ["incomplete"])

    def test_duplicate_end_writes_one_receipt(self):
        self.play(start(), stop(), end(), end())
        self.assertEqual(len(self.rows()), 1)

    def test_non_filename_safe_session_id_is_hashed_and_not_stored(self):
        weird = "../../etc/passwd " + CANARY
        self.play(start(weird), stop(sid=weird), end(weird))
        row = self.one()
        self.assertIsNone(row["session_id"])
        self.assertEqual(os.listdir(self.state).count("receipts.jsonl"), 1)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "etc")))

    def test_odd_tool_names_are_stored_as_unknown(self):
        self.play(start(), tool("rm -rf " + CANARY), tool("mcp__srv__do_thing"), stop(), end())
        row = self.one()
        self.assertEqual(row["tools"], ["mcp__srv__do_thing", "unknown"])


class TestPrivacy(Base):
    def test_no_canary_text_reaches_disk(self):
        self.play(start(), tool(), tool_fail(), stop(), end(), start("two"), stop_failure(sid="two"), end("two"))
        for root, _dirs, files in os.walk(self.tmp):
            for name in files:
                with open(os.path.join(root, name), "rb") as handle:
                    self.assertNotIn(CANARY.encode(), handle.read(), os.path.join(root, name))

    def test_no_canary_in_open_log_mid_session(self):
        self.play(start(), tool(), tool_fail(), stop())
        with open(os.path.join(self.state, "open", "sess-1.jsonl"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn(CANARY, text)
        self.assertNotIn("/home/example", text)

    def test_receipt_has_only_known_fields(self):
        self.play(start(), tool(), stop(), end())
        self.assertLessEqual(set(self.one()), KEYS)

    def test_permissions(self):
        self.play(start(), tool(), stop(), end())
        self.assertEqual(stat.S_IMODE(os.stat(self.file).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.state, "open")).st_mode), 0o700)

    def test_loose_existing_modes_are_tightened(self):
        os.makedirs(os.path.join(self.state, "open"))
        os.chmod(self.state, 0o755)
        with open(self.file, "w", encoding="utf-8") as handle:
            handle.write("")
        os.chmod(self.file, 0o644)
        self.play(start(), stop(), end())
        self.assertEqual(stat.S_IMODE(os.stat(self.file).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode), 0o700)

    def test_hook_output_is_empty_and_never_a_decision(self):
        for event in (start(), tool(), tool_fail(), stop(), stop_failure(), end()):
            done = self.hook(event)
            self.assertEqual((done.returncode, done.stdout, done.stderr), (0, b"", b""))


class TestFailOpen(Base):
    def test_garbage_input_exits_zero_and_writes_nothing(self):
        for junk in (b"", b"not json", b"[1,2]", b"null", b'{"hook_event_name":"Stop"}',
                     b'{"session_id":5,"hook_event_name":"SessionStart"}', b"\xff\xfe\x00"):
            done = self.hook(junk)
            self.assertEqual((done.returncode, done.stdout), (0, b""), junk)
        self.assertEqual(self.rows(), [])

    def test_unwritable_state_dir_exits_zero(self):
        blocker = os.path.join(self.tmp, "file-not-dir")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("x")
        done = self.hook(start(), RUN_RECEIPTS_DIR=os.path.join(blocker, "sub"))
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, b"", b""))

    def test_symlinked_state_dir_is_refused(self):
        real = os.path.join(self.tmp, "real")
        os.mkdir(real)
        os.symlink(real, self.state)
        done = self.hook(start())
        self.assertEqual(done.returncode, 0)
        self.assertEqual(os.listdir(real), [])

    def test_symlinked_receipts_file_is_refused(self):
        os.makedirs(os.path.join(self.state, "open"))
        target = os.path.join(self.tmp, "target.jsonl")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("")
        os.symlink(target, self.file)
        self.play(start(), stop(), end())
        self.assertEqual(os.path.getsize(target), 0)

    def test_symlinked_open_log_is_refused(self):
        os.makedirs(os.path.join(self.state, "open"))
        target = os.path.join(self.tmp, "victim.jsonl")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("keep\n")
        os.symlink(target, os.path.join(self.state, "open", "sess-1.jsonl"))
        done = self.hook(start())
        self.assertEqual(done.returncode, 0)
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "keep\n")

    def test_unknown_events_are_ignored(self):
        self.play(start(), ev("Notification", message=CANARY), ev("PreToolUse", tool_name="Bash"),
                  stop(), end())
        self.assertEqual(self.one()["outcome"], "completed_no_tools")


class TestPrune(Base):
    def seed(self, *rows):
        os.makedirs(self.state, exist_ok=True)
        with open(self.file, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write((row if isinstance(row, str) else json.dumps(row)) + "\n")
        os.chmod(self.file, 0o600)

    def old_row(self, days, sid):
        return {"v": 1, "session_id": sid, "started_at": core.iso(time.time() - days * 86400 - 5),
                "ended_at": core.iso(time.time() - days * 86400), "outcome": "completed",
                "tool_calls": 1, "tool_errors": 0, "tools": ["Bash"], "tool_stats": {}}

    def test_old_receipts_removed_when_a_new_one_is_written(self):
        self.seed(self.old_row(40, "old"), self.old_row(5, "recent"), "{torn")
        self.play(start("new"), stop(sid="new"), end("new"))
        self.assertEqual([r["session_id"] for r in self.rows()], ["recent", "new"])
        self.assertEqual(stat.S_IMODE(os.stat(self.file).st_mode), 0o600)

    def test_keep_days_setting(self):
        self.seed(self.old_row(5, "five"), self.old_row(1, "one"))
        self.play(start("new"), stop(sid="new"), end("new"), RUN_RECEIPTS_KEEP_DAYS="3")
        self.assertEqual([r["session_id"] for r in self.rows()], ["one", "new"])

    def test_bad_keep_days_falls_back_to_default(self):
        self.seed(self.old_row(40, "old"), self.old_row(5, "recent"))
        self.play(start("new"), stop(sid="new"), end("new"), RUN_RECEIPTS_KEEP_DAYS="banana")
        self.assertEqual([r["session_id"] for r in self.rows()], ["recent", "new"])

    def test_nothing_removed_when_nothing_is_old(self):
        self.seed(self.old_row(2, "a"))
        with open(self.file, encoding="utf-8") as handle:
            before = handle.read()
        self.play(start("new"), stop(sid="new"), end("new"))
        with open(self.file, encoding="utf-8") as handle:
            self.assertTrue(handle.read().startswith(before))

    def test_torn_fragment_is_discarded_on_prune(self):
        self.seed(self.old_row(2, "a"), "{torn")
        self.play(start("new"), stop(sid="new"), end("new"))
        self.assertEqual([r["session_id"] for r in self.rows()], ["a", "new"])


class TestQuery(Base):
    def seed_run(self):
        self.play(start("a"), tool(sid="a"), stop(sid="a"), end("a"),
                  start("b"), tool(sid="b"), tool_fail(sid="b"), stop(sid="b"), end("b"),
                  start("c"), stop_failure(sid="c"), end("c"),
                  start("d"), stop(sid="d"), end("d"))

    def test_last_is_newest_first_and_limited(self):
        self.seed_run()
        done, out = self.query("last", "2")
        self.assertEqual(done.returncode, 0)
        self.assertEqual([r["session_id"] for r in out["receipts"]], ["d", "c"])
        self.assertEqual(out["count"], 2)

    def test_default_limit_and_clamp(self):
        for i in range(12):
            self.play(start("s%d" % i), stop(sid="s%d" % i), end("s%d" % i))
        _, out = self.query("last")
        self.assertEqual(out["count"], 10)
        _, out = self.query("last", "0")
        self.assertEqual(out["count"], 1)
        _, out = self.query("last", "99999")
        self.assertEqual(out["count"], 12)

    def test_suspicious(self):
        self.seed_run()
        _, out = self.query("suspicious")
        self.assertEqual(sorted(r["session_id"] for r in out["receipts"]), ["b", "c"])

    def test_suspicious_ignores_rows_older_than_seven_days(self):
        os.makedirs(self.state)
        old = {"v": 1, "session_id": "old", "ended_at": core.iso(time.time() - 8 * 86400),
               "started_at": core.iso(time.time() - 8 * 86400), "outcome": "failed"}
        with open(self.file, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(old) + "\n")
        _, out = self.query("suspicious")
        self.assertEqual(out["count"], 0)

    def test_session_and_outcome(self):
        self.seed_run()
        _, out = self.query("session", "b")
        self.assertEqual([r["outcome"] for r in out["receipts"]], ["completed_with_errors"])
        _, out = self.query("outcome", "completed_no_tools")
        self.assertEqual([r["session_id"] for r in out["receipts"]], ["d"])

    def test_bad_arguments_are_refused(self):
        for args in ((), ("nope",), ("last", "x"), ("last", "1", "2"), ("session",),
                     ("session", "a b"), ("outcome", "bogus"), ("session", "../x")):
            done, out = self.query(*args)
            self.assertEqual(done.returncode, 2, args)
            self.assertIn("error", out)

    def test_query_is_read_only(self):
        for args in (("last",), ("suspicious",), ("session", "a"), ("outcome", "failed")):
            done, out = self.query(*args)
            self.assertEqual(done.returncode, 0)
            self.assertEqual(out["count"], 0)
        self.assertFalse(os.path.exists(self.state))

    def test_query_does_not_change_modes_or_content(self):
        self.seed_run()
        os.chmod(self.file, 0o644)
        with open(self.file, "rb") as handle:
            before = handle.read()
        self.query("last")
        self.assertEqual(stat.S_IMODE(os.stat(self.file).st_mode), 0o644)
        with open(self.file, "rb") as handle:
            self.assertEqual(handle.read(), before)

    def test_symlinked_receipts_file_is_refused(self):
        os.makedirs(self.state)
        target = os.path.join(self.tmp, "t.jsonl")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"outcome": "failed"}) + "\n")
        os.symlink(target, self.file)
        done, out = self.query("last")
        self.assertEqual(done.returncode, 2)
        self.assertIn("error", out)

    def test_symlinked_state_dir_is_refused(self):
        real = os.path.join(self.tmp, "real")
        os.mkdir(real)
        os.symlink(real, self.state)
        done, out = self.query("last")
        self.assertEqual(done.returncode, 2)

    def test_malformed_rows_do_not_hide_valid_ones(self):
        os.makedirs(self.state)
        good = {"v": 1, "session_id": "ok", "started_at": core.iso(time.time()),
                "ended_at": core.iso(time.time()), "outcome": "failed"}
        with open(self.file, "w", encoding="utf-8") as handle:
            handle.write("{torn\n[1]\n" + json.dumps({"outcome": 5, "session_id": ["x"]}) + "\n"
                         + json.dumps(good) + "\n")
        _, out = self.query("suspicious")
        self.assertEqual([r["session_id"] for r in out["receipts"]], ["ok"])

    def test_tampered_rows_cannot_push_extra_text_through(self):
        os.makedirs(self.state)
        row = {"v": 1, "session_id": "ok", "outcome": "completed", "reason": CANARY,
               "tools": ["Bash", "bad " + CANARY], "extra": CANARY,
               "tool_stats": {"Bash": {"calls": 1, "errors": 0, "note": CANARY}}}
        with open(self.file, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
        done, out = self.query("last")
        self.assertNotIn(CANARY, done.stdout.decode())
        self.assertLessEqual(set(out["receipts"][0]), KEYS)

    def test_query_reads_the_configured_dir(self):
        self.seed_run()
        other = os.path.join(self.tmp, "elsewhere")
        _, out = self.query("last", RUN_RECEIPTS_DIR=other)
        self.assertEqual(out["count"], 0)


class TestStateDir(Base):
    def test_default_follows_claude_config_dir(self):
        cfg = os.path.join(self.tmp, "cfg")
        env = self.env(CLAUDE_CONFIG_DIR=cfg)
        del env["RUN_RECEIPTS_DIR"]
        for payload in (start(), stop(), end()):
            subprocess.run([sys.executable, HOOK], input=json.dumps(payload).encode(), env=env,
                           capture_output=True, timeout=30, check=True)
        self.assertTrue(os.path.exists(os.path.join(cfg, "run-receipts", "receipts.jsonl")))

    def test_default_falls_back_to_home_dot_claude(self):
        env = self.env()
        del env["RUN_RECEIPTS_DIR"]
        for payload in (start(), stop(), end()):
            subprocess.run([sys.executable, HOOK], input=json.dumps(payload).encode(), env=env,
                           capture_output=True, timeout=30, check=True)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, ".claude", "run-receipts", "receipts.jsonl")))


class TestPackaging(unittest.TestCase):
    def load(self, *parts):
        with open(os.path.join(PLUGIN, *parts), encoding="utf-8") as handle:
            return json.load(handle)

    def test_manifest(self):
        manifest = self.load(".claude-plugin", "plugin.json")
        self.assertEqual(manifest["name"], "axiom-run-receipts")
        self.assertEqual(manifest["displayName"], "Run Receipts")
        self.assertEqual(manifest["version"], "0.1.0")
        self.assertEqual(manifest["license"], "MIT")
        self.assertEqual(manifest["author"], {"name": "Deploy Axiom"})
        self.assertTrue(os.path.exists(os.path.join(PLUGIN, ".claude-plugin", "icon.png")))

    def test_marketplace(self):
        root = os.path.dirname(os.path.dirname(HERE))
        with open(os.path.join(root, ".claude-plugin", "marketplace.json"), encoding="utf-8") as handle:
            market = json.load(handle)
        self.assertEqual(market["plugins"][0]["name"], "axiom-run-receipts")
        self.assertTrue(os.path.isdir(os.path.join(root, market["plugins"][0]["source"])))

    def test_hooks_use_documented_events_only_and_never_block(self):
        documented = {"SessionStart", "SessionEnd", "PostToolUse", "PostToolUseFailure", "Stop", "StopFailure"}
        hooks = self.load("hooks", "hooks.json")["hooks"]
        self.assertEqual(set(hooks), documented)
        for groups in hooks.values():
            for group in groups:
                for handler in group["hooks"]:
                    self.assertEqual(handler["type"], "command")
                    self.assertNotIn("async", handler)
                    self.assertTrue(handler["args"][0].startswith("${CLAUDE_PLUGIN_ROOT}/scripts/"))

    def test_code_has_no_network_or_subprocess(self):
        for name in ("receipts_core.py", "receipt_hook.py", "receipts_query.py"):
            with open(os.path.join(SCRIPTS, name), encoding="utf-8") as handle:
                text = handle.read()
            for banned in ("import socket", "urllib", "http.client", "subprocess", "os.system", "requests"):
                self.assertNotIn(banned, text, name)

    def test_skill_is_read_only_and_scoped(self):
        with open(os.path.join(PLUGIN, "skills", "receipts", "SKILL.md"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/receipts_query.py *)", text)
        self.assertNotIn("Write", text.split("---")[1])


if __name__ == "__main__":
    unittest.main()
