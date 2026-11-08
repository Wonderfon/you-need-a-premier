import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backfill_ledger as bf  # noqa: E402


def stream(result="done", cost=0.25):
    return "\n".join([json.dumps({"type": "assistant"}), json.dumps({"type": "result", "is_error": False, "result": result,
                      "total_cost_usd": cost, "usage": {"input_tokens": 3, "cache_read_input_tokens": 100,
                                                        "cache_creation_input_tokens": 7, "output_tokens": 55}})])


class Backfill(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.d = Path(self.tmp.name)
        (d / "ticks").mkdir()
        for k, v in {"LOG_FILE": d / "tick-log.jsonl", "TICKS_DIR": d / "ticks", "RUNS_FILE": d / "runs.jsonl"}.items():
            p = mock.patch.object(bf, k, v)
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def run_file(self, name, text=None):
        f = self.d / "ticks" / name
        f.write_text(text or stream(), encoding="utf-8")
        return f

    def make_log(self):
        ex = self.run_file("20260930-100000-executor.jsonl")
        rev = self.run_file("20260930-110000-review-work.jsonl", stream("ok\nVERDICT: pass"))
        own = self.run_file("20260930-120000-owner-life.jsonl")
        self.run_file("20260930-090000-report-life.jsonl")
        self.run_file("20260930-090500-report-life-retry.jsonl")
        self.run_file("20260930-090000-other-life.jsonl")  # not a ledger run
        entries = [
            {"at": "2026-09-30T10:30:00+00:00", "decision": "ran", "kind": "executor", "section": "量化线", "item": "q1",
             "model": "sonnet", "run_file": str(ex), "exit_code": 0, "elapsed_s": 60},
            {"at": "2026-09-30T11:30:00+00:00", "decision": "ran", "kind": "review", "section": "工作线", "item": "w1",
             "model": "opus", "run_file": str(rev), "exit_code": 0, "elapsed_s": 30},
            {"at": "2026-09-30T12:30:00+00:00", "decision": "owner_ran", "role": "life", "item": "hi", "model": "opus",
             "run_file": str(own), "exit_code": 0, "ok": True, "elapsed_s": 5},
            {"at": "2026-09-30T13:00:00+00:00", "decision": "paused"},
            {"at": "2026-09-30T13:30:00+00:00", "decision": "ran", "kind": "executor", "section": "工作线", "item": "gone",
             "model": "sonnet", "run_file": str(self.d / "ticks" / "missing.jsonl"), "exit_code": 0, "elapsed_s": 1},
        ]
        (self.d / "tick-log.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\nbroken\n", encoding="utf-8")

    def rows(self):
        return [json.loads(x) for x in (self.d / "runs.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_builds_rows_with_inherited_effort_and_session(self):
        self.make_log()
        self.assertEqual(bf.backfill(), 5)  # executor, review, owner, 2 report files; paused/missing/other skipped
        rows = {Path(r["run_file"]).name: r for r in self.rows()}
        ex = rows["20260930-100000-executor.jsonl"]
        self.assertEqual((ex["role"], ex["section"], ex["kind"], ex["model"], ex["effort"], ex["session"], ex["item"]),
                         ("quant", "量化线", "executor", "sonnet", "high", "fresh", "q1"))
        self.assertEqual((ex["at"], ex["elapsed_s"], ex["exit_code"], ex["ok"], ex["cost_usd"], ex["input_tokens"],
                          ex["cache_read_tokens"], ex["cache_creation_tokens"], ex["output_tokens"], ex["verdict"]),
                         ("2026-09-30T10:30:00+00:00", 60, 0, True, 0.25, 3, 100, 7, 55, None))
        rev = rows["20260930-110000-review-work.jsonl"]
        self.assertEqual((rev["role"], rev["effort"], rev["session"], rev["verdict"]), ("work", "xhigh", "resume", "pass"))
        own = rows["20260930-120000-owner-life.jsonl"]
        self.assertEqual((own["kind"], own["role"], own["section"]), ("owner", "life", "生活线"))
        rep = rows["20260930-090000-report-life.jsonl"]
        retry = rows["20260930-090500-report-life-retry.jsonl"]
        self.assertEqual((rep["kind"], rep["role"], rep["effort"], rep["session"]), ("report", "life", "xhigh", "resume"))
        self.assertEqual(retry["session"], "new")

    def test_idempotent(self):
        self.make_log()
        self.assertEqual(bf.backfill(), 5)
        before = (self.d / "runs.jsonl").read_text(encoding="utf-8")
        self.assertEqual(bf.backfill(), 0)
        self.assertEqual((self.d / "runs.jsonl").read_text(encoding="utf-8"), before)
        self.run_file("20260930-130000-report-work.jsonl")  # a new run file is picked up on the next pass
        self.assertEqual(bf.backfill(), 1)

    def test_skips_files_already_in_ledger_from_live_lanes(self):
        self.make_log()
        ex = str(self.d / "ticks" / "20260930-100000-executor.jsonl")
        (self.d / "runs.jsonl").write_text(json.dumps({"run_file": ex, "kind": "executor"}) + "\n", encoding="utf-8")
        self.assertEqual(bf.backfill(), 4)
        self.assertEqual(sum(1 for r in self.rows() if r["run_file"] == ex), 1)

    def test_empty_inputs(self):
        self.assertEqual(bf.backfill(), 0)


if __name__ == "__main__":
    unittest.main()
