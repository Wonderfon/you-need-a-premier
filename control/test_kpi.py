import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kpi  # noqa: E402

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def run(kind, hours, item="", section="Arbitros 线", model="sonnet", effort="high", session="new", cost=1.0, **kw):
    return {"at": (NOW - timedelta(hours=hours)).isoformat(), "kind": kind, "section": section, "item": item,
            "role": "work", "model": model, "effort": effort, "session": session, "ok": True, "elapsed_s": 100,
            "cost_usd": cost, "output_tokens": 1000, **kw}


class KpiCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.d = d
        for k, v in {"RUNS_FILE": d / "runs.jsonl", "KPI_JSON": d / "kpi.json", "KPI_MD": d / "kpi.md",
                     "DEPUTIES_FILE": d / "deputies.json"}.items():
            p = mock.patch.object(kpi, k, v)
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, *rows):
        (self.d / "runs.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n", encoding="utf-8")

    def group(self, summary, kind, effort):
        return next(g for g in summary["groups"] if g["kind"] == kind and g["effort"] == effort)


class Kpi(KpiCase):
    def test_window_and_bad_lines(self):
        self.put(run("executor", 1), run("executor", 24 * 8), {"at": "garbage"})
        self.assertEqual(len(kpi.load(NOW)), 1)
        self.assertEqual(kpi.load(NOW)[0]["kind"], "executor")
        (self.d / "runs.jsonl").unlink()
        self.assertEqual(kpi.load(NOW), [])

    def test_grouping_stats(self):
        self.put(run("executor", 5, "a", cost=1.0, elapsed_s=100), run("executor", 4, "b", cost=3.0, elapsed_s=300, ok=False),
                 run("executor", 3, "c", effort="medium", cost=0.5),
                 run("executor", 2, "d", session="resume", cost=2.0), run("review", 1, "a", model="opus", effort="xhigh", cost=2.0))
        s = kpi.summarize(kpi.load(NOW))
        g = next(x for x in s["groups"] if (x["kind"], x["effort"], x["session"]) == ("executor", "high", "new"))
        self.assertEqual((g["n"], g["ok_rate"], g["median_elapsed_s"], g["median_cost_usd"], g["total_cost_usd"],
                          g["median_output_tokens"]), (2, 0.5, 200, 2.0, 4.0, 1000))
        self.assertEqual(len(s["groups"]), 4)
        self.assertEqual(s["n_runs"], 5)
        self.assertEqual(s["total_cost_usd"], 8.5)
        self.assertEqual(s["role_cost"]["work"]["share"], 1.0)

    def test_review_join_first_pass_rate(self):
        self.put(run("executor", 10, "a"), run("review", 9, "a", verdict="rework"),
                 run("executor", 8, "a"), run("review", 7, "a", verdict="pass"),     # a: rework then pass
                 run("executor", 6, "b"), run("review", 5, "b", verdict="pass"),
                 run("executor", 4, "c", effort="medium"), run("review", 3, "c", verdict="blocked"),
                 run("executor", 2, "d", effort="medium"),                          # not reviewed yet
                 run("executor", 1.5, "e", effort="medium"), run("review", 1, "e", verdict="unknown"),
                 run("review", 0.5, "b", section="量化线", verdict="pass"))             # other section: not joined
        rows = kpi.load(NOW)
        joined = kpi.review_join(rows)
        self.assertEqual([joined.get(i) for i, r in enumerate(rows) if r["kind"] == "executor"],
                         ["rework", "pass", "pass", "blocked", None, "unknown"])
        s = kpi.summarize(rows)
        high, med = self.group(s, "executor", "high"), self.group(s, "executor", "medium")
        self.assertEqual((high["reviewed_n"], high["pass"], high["rework"], high["blocked"], high["first_pass_rate"]),
                         (3, 2, 1, 0, 0.667))
        self.assertEqual((med["n"], med["reviewed_n"], med["blocked"], med["first_pass_rate"]), (3, 1, 1, 0.0))
        self.assertNotIn("first_pass_rate", self.group(s, "review", "high"))

    def test_review_belongs_to_the_executor_run_before_the_next_one(self):
        # executor ran twice with no review in between: the single review joins only the second run
        self.put(run("executor", 5, "a"), run("executor", 4, "a"), run("review", 3, "a", verdict="pass"))
        self.assertEqual(kpi.review_join(kpi.load(NOW)), {1: "pass"})

    def test_role_cost_share(self):
        self.put(run("executor", 3, cost=3.0), {**run("review", 2, cost=1.0), "role": "life"})
        share = kpi.summarize(kpi.load(NOW))["role_cost"]
        self.assertEqual((share["work"]["share"], share["life"]["share"], share["work"]["n"]), (0.75, 0.25, 1))

    def test_write_files(self):
        self.put(run("executor", 3, "a"), run("review", 2, "a", verdict="pass", model="opus", effort="xhigh"))
        kpi.write(NOW)
        data = json.loads((self.d / "kpi.json").read_text(encoding="utf-8"))
        self.assertEqual(data["n_runs"], 2)
        md = (self.d / "kpi.md").read_text(encoding="utf-8")
        self.assertIn("| executor | sonnet | high | new | 1 |", md)
        self.assertIn("n=2", md)
        self.assertIn("各角色成本占比", md)

    def test_executor_session_table(self):
        ex = lambda hours, item, sid, project="uvox", **kw: run("executor", hours, item, session_id=sid, project=project, **kw)
        self.put(ex(10, "a", "aaaaaaaa-1", cost=1.0), run("review", 9, "a", verdict="rework"),
                 ex(8, "a", "aaaaaaaa-1", cost=0.5), run("review", 7, "a", verdict="rework"),
                 {**run("lessons", 6, session_id="aaaaaaaa-1", project="uvox", cost=0.25)},
                 ex(5, "b", "bbbbbbbb-2", cost=2.0), run("review", 4, "b", verdict="pass"),
                 ex(3, "c", "cccccccc-3", project="umba", cost=0.5), ex(2, "legacy", "dddddddd-4", project=None))
        (self.d / "deputies.json").write_text(json.dumps({"work": {"executors": {
            "uvox": {"session_id": "bbbbbbbb-2", "reworks_in_row": 1, "retire": False,
                     "retired": [{"session_id": "aaaaaaaa-1", "runs": 2}]},
            "umba": {"session_id": "cccccccc-3", "reworks_in_row": 2, "retire": True}}}}), encoding="utf-8")
        kpi.write(NOW)
        table = {s["session"]: s for s in json.loads((self.d / "kpi.json").read_text(encoding="utf-8"))["executor_sessions"]}
        a = table["aaaaaaaa"]
        self.assertEqual((a["runs"], a["reviewed_n"], a["rework"], a["first_pass_rate"], a["cost_usd"], a["state"], a["reworks_in_row"]),
                         (2, 2, 2, 0.0, 1.75, "retired", None))  # cost includes the lessons run
        b = table["bbbbbbbb"]
        self.assertEqual((b["pass"], b["first_pass_rate"], b["state"], b["reworks_in_row"]), (1, 1.0, "live", 1))
        self.assertEqual((table["cccccccc"]["state"], table["cccccccc"]["reworks_in_row"], table["cccccccc"]["project"]),
                         ("retire", 2, "umba"))
        self.assertEqual((table["dddddddd"]["state"], table["dddddddd"]["project"]), ("-", None))
        md = (self.d / "kpi.md").read_text(encoding="utf-8")
        self.assertIn("## 执行者会话", md)
        self.assertIn("| work | uvox | bbbbbbbb | 1 | 1 | 1 | 0 | 1.00 | 2.00 | 1 | live |", md)

    def test_empty_ledger(self):
        kpi.write(NOW)
        self.assertEqual(json.loads((self.d / "kpi.json").read_text(encoding="utf-8"))["groups"], [])


if __name__ == "__main__":
    unittest.main()
