import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import meeting  # noqa: E402
import tick  # noqa: E402

STREAM = "\n".join(json.dumps(e) for e in (
    {"type": "assistant", "message": {"usage": {"input_tokens": 3, "cache_read_input_tokens": 1000}}},
    {"type": "result", "is_error": False, "result": "结论：卡在抓取。", "total_cost_usd": 0.4,
     "usage": {"input_tokens": 3, "cache_read_input_tokens": 1000, "output_tokens": 20}}))


class MeetingCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.d = Path(self.tmp.name)
        (d / "rules.md").write_text("RULES", encoding="utf-8")
        for k, v in {"DATA": d, "DEPUTIES_FILE": d / "deputies.json", "RUNS_FILE": d / "runs.jsonl", "TICKS_DIR": d / "ticks",
                     "LOG_FILE": d / "tick-log.jsonl", "RULES_FILE": d / "rules.md"}.items():
            p = mock.patch.object(tick, k, v)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(meeting, "MEETINGS", d / "meetings")
        p.start()
        self.addCleanup(p.stop)
        tick.save_deputies({"intelligence": {"session_id": "sid-intel", "created_at": None, "status": "idle", "current": None,
                                             "last": None, "runs": 3}})
        self.sessions = []

        def fake_session_run(kind, prompt, label, sid, resume, effort, on_retry=None, project=None, model=None, compact_at=None):
            f = d / f"{label}.jsonl"
            f.write_text(STREAM, encoding="utf-8")
            self.sessions.append({"kind": kind, "prompt": prompt, "sid": sid, "resume": resume, "model": model, "effort": effort})
            return 0, f, sid, resume
        p = mock.patch.object(tick, "session_run", fake_session_run)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def start_meeting(self):
        self.assertEqual(meeting.start("t1", "intelligence", "work", "议题=说卡点"), 0)
        return next((self.d / "meetings").glob("*-t1.md"))

    def test_start_say_and_minutes_format(self):
        path = self.start_meeting()
        text = path.read_text(encoding="utf-8")
        self.assertIn("# 会议：t1", text)
        self.assertIn("- 议题：说卡点", text)
        self.assertIn("- 与会人：情报专员 · intelligence、Arbitros 副总理 · work", text)
        said = "第一行\n  “原话” 不改 *格式*  \n第三行"
        meeting.say("owner", said)
        self.assertIn(f"## owner · ", path.read_text(encoding="utf-8"))
        self.assertTrue(path.read_text(encoding="utf-8").endswith(said + "\n"))
        self.assertEqual(meeting.start("t1", "work"), 2)  # no overwrite
        self.assertEqual(meeting.start("t2", "nobody"), 2)

    def test_turn_resumes_the_resident_session_and_appends_the_reply(self):
        path = self.start_meeting()
        meeting.say("owner", "各说一句")
        self.assertEqual(meeting.turn("intelligence"), 0)
        s = self.sessions[0]
        self.assertEqual((s["kind"], s["sid"], s["resume"], s["model"], s["effort"]), ("meeting", "sid-intel", True, "sonnet", "high"))  # owner\x2010-25: intelligence at high
        self.assertIn("说卡点", s["prompt"])
        self.assertIn("各说一句", s["prompt"])  # minutes so far
        text = path.read_text(encoding="utf-8")
        self.assertRegex(text, r"\n## 情报专员 · intelligence · \d\d:\d\d\n\n结论：卡在抓取。\n$")
        self.assertFalse(tick.deputy_lock("intelligence").exists())  # lock released
        self.assertEqual(tick.load_deputies()["intelligence"]["status"], "idle")
        # the second speaker's prompt contains the first's reply
        self.assertEqual(meeting.turn("intelligence"), 0)
        self.assertIn("结论：卡在抓取。", self.sessions[1]["prompt"])

    def test_runs_row_is_a_meeting_kind_with_cost_and_file_and_off_the_ledger_shares(self):
        path = self.start_meeting()
        meeting.turn("intelligence")
        rows = [json.loads(x) for x in (self.d / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["kind"], r["role"], r["item"], r["model"], r["ok"]), ("meeting", "intelligence", str(path), "sonnet", True))
        self.assertAlmostEqual(r["cost_usd"], 0.4)
        self.assertNotIn("meeting", tick.COST_KINDS)

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_lock_busy_retries_then_gives_up_without_running(self):
        self.start_meeting()
        lock = tick.deputy_lock("intelligence")
        lock.write_text(str(__import__("os").getpid()))  # a live holder (this process)
        sleeps = []
        with mock.patch.object(meeting.time, "sleep", sleeps.append), mock.patch.object(meeting, "LOCK_WAIT_S", 60):
            self.assertEqual(meeting.turn("intelligence"), 3)
        self.assertEqual(sleeps, [15, 15, 15, 15])
        self.assertEqual(self.sessions, [])
        self.assertTrue(lock.exists())  # not ours: untouched

    @unittest.skipUnless(sys.platform == "win32", "tick.pid_alive requires Windows tasklist and creationflags")
    def test_lock_freed_during_the_wait_runs_the_turn(self):
        self.start_meeting()
        lock = tick.deputy_lock("intelligence")
        lock.write_text(str(__import__("os").getpid()))
        with mock.patch.object(meeting.time, "sleep", lambda s: lock.unlink()):
            self.assertEqual(meeting.turn("intelligence"), 0)
        self.assertEqual(len(self.sessions), 1)

    def test_only_attendees_can_be_called(self):
        self.start_meeting()
        self.assertEqual(meeting.turn("quant"), 2)
        self.assertEqual(self.sessions, [])

    def test_word_limit_defaults_to_400_and_can_be_raised(self):
        self.start_meeting()
        meeting.turn("intelligence")
        self.assertIn("400 字以内", self.sessions[0]["prompt"])
        meeting.turn("intelligence", words=1500)
        self.assertIn("1500 字以内", self.sessions[1]["prompt"])
        self.assertNotIn("400 字以内", self.sessions[1]["prompt"])

    def test_turn_cli_takes_word_limit_and_rejects_bad_values(self):
        self.start_meeting()
        self.assertEqual(meeting.main(["turn", "intelligence", "字数=1500"]), 0)
        self.assertIn("1500 字以内", self.sessions[0]["prompt"])
        self.assertEqual(meeting.main(["turn", "intelligence", "t1", "字数=1500"]), 0)
        self.assertIn("1500 字以内", self.sessions[1]["prompt"])
        for bad in ("字数=abc", "字数=0", "字数=-5", "字数="):
            self.assertEqual(meeting.main(["turn", "intelligence", bad]), 2)
        self.assertEqual(len(self.sessions), 2)


if __name__ == "__main__":
    unittest.main()
