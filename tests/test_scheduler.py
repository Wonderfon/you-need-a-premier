import json
import unittest
from datetime import datetime, timedelta, timezone

from orgcore import backlog, ledger, scheduler, usage, workflow
from orgcore.usage import FakeUsage

from .support import InstanceCase, board, run_worker

NOW = datetime(2026, 1, 5, 12, 0, 0, tzinfo=timezone.utc)


def rows(project, n, prefix):
    return [f"- [todo] {project}: {prefix}{i}" for i in range(n)]


class Dispatch(InstanceCase):
    def healthy(self):
        return FakeUsage.healthy(self.instance().policy, NOW)

    def finish(self, task):
        for current, new in [("running", "review"), ("review", "done")]:
            backlog.transition(self.instance().backlog_path, task.key, current, new)

    def test_line_choice_follows_spend_plus_one_over_weight(self):
        self.write_board(board(("research", 60, rows("demo-notes", 6, "r")), ("build", 40, rows("demo-bikes", 6, "b"))))
        picked = []
        for _ in range(8):
            result = scheduler.dispatch(self.instance(), self.healthy(), "executor-1", NOW)
            picked.append(result.task.section)
            self.finish(result.task)
        # (spend+1)/weight, research 60 build 40: r .017 b .025 | r .033 b .025 | r .033 b .05 | r .05 b .05 (tie: listed
        # first) | r .067 b .05 | r .067 b .075 | r .083 b .075 | r .083 b .1
        self.assertEqual(picked, ["research", "build", "research", "research", "build", "research", "build", "research"])

    def test_inside_a_line_rows_go_in_board_order_one_at_a_time(self):
        self.write_board(board(("research", 60, rows("demo-notes", 3, "r")), ("build", 40, [])))
        first = scheduler.dispatch(self.instance(), self.healthy(), "executor-1", NOW)
        self.assertEqual(first.task.text, "r0")
        second = scheduler.dispatch(self.instance(), self.healthy(), "executor-2", NOW)
        self.assertEqual((second.code, second.reasons[0][:10]), ("busy", "1 row(s) r"))
        self.finish(first.task)
        self.assertEqual(scheduler.dispatch(self.instance(), self.healthy(), "executor-1", NOW).task.text, "r1")

    def test_rework_row_is_taken_before_new_rows_of_its_line(self):
        self.write_board(board(("research", 60, ["- [todo] demo-notes: new", "- [rework fix it] demo-notes: old"]),
                               ("build", 40, [])))
        self.assertEqual(scheduler.dispatch(self.instance(), self.healthy(), "e", NOW).task.text, "old")

    def test_spend_window_forgets_old_dispatches(self):
        events = [{"event": "dispatched", "line": "research", "ts": "2026-01-04T12:00:00Z"},
                  {"event": "dispatched", "line": "research", "ts": "2025-12-01T12:00:00Z"}]
        self.assertEqual(scheduler.recent_spend(events, NOW), {"research": 1})

    def test_no_work(self):
        self.write_board(board(("research", 60, ["- [done] demo-notes: x"]), ("build", 40, [])))
        self.assertEqual(scheduler.dispatch(self.instance(), self.healthy(), "e", NOW).code, "no_work")

    def test_unknown_or_unreadable_usage_blocks_and_changes_nothing(self):
        before = self.root.joinpath("projects", "backlog.md").read_bytes()
        for source in (FakeUsage([]), usage.JsonFileUsage(self.root / "state" / "usage.json")):
            result = scheduler.dispatch(self.instance(), source, "e", NOW)
            self.assertEqual(result.code, "usage")
            self.assertTrue(any("unknown" in r or "not found" in r for r in result.reasons), result.reasons)
        self.assertEqual(self.root.joinpath("projects", "backlog.md").read_bytes(), before)
        self.assertEqual(ledger.read(self.root), [])

    def test_stale_and_over_limit_usage_block_with_reasons(self):
        stale = FakeUsage([usage.Observation(w["name"], 5, NOW - timedelta(hours=9), NOW + timedelta(hours=1), "x", True)
                           for w in self.instance().policy["subscription_windows"]])
        result = scheduler.dispatch(self.instance(), stale, "e", NOW)
        self.assertTrue(all("stale" in r for r in result.reasons) and len(result.reasons) == 2)
        high = FakeUsage.healthy(self.instance().policy, NOW, used_percent=75)
        self.assertIn("reserve boundary", scheduler.dispatch(self.instance(), high, "e", NOW).reasons[0])

    def test_live_mode_refuses_synthetic_readings(self):
        self.edit_policy(live_enabled=True, live_consent={"granted_at": "2026-01-05T11:00:00Z", "projects": ["demo-notes"],
                                                           "task_scope": "one row"})
        result = scheduler.dispatch(self.instance(), self.healthy(), "e", NOW)
        self.assertIn("not accepted in live mode", result.reasons[0])

    def test_board_and_config_disagreement_refuses_dispatch(self):
        self.write_board(board(("research", 50, rows("demo-notes", 1, "r")), ("build", 40, [])))
        result = scheduler.dispatch(self.instance(), self.healthy(), "e", NOW)
        self.assertEqual(result.code, "inconsistent")
        self.assertIn("weight mismatch for line 'research'", result.reasons[0])

    def test_dispatch_records_ledger_and_attempt_numbers(self):
        self.write_board(board(("research", 60, rows("demo-notes", 1, "r")), ("build", 40, [])))
        first = scheduler.dispatch(self.instance(), self.healthy(), "executor-1", NOW)
        self.assertEqual(first.attempt, 1)
        backlog.transition(self.instance().backlog_path, first.task.key, "running", "review")
        backlog.transition(self.instance().backlog_path, first.task.key, "review", "rework", "fix")
        self.assertEqual(scheduler.dispatch(self.instance(), self.healthy(), "executor-1", NOW).attempt, 2)
        events = ledger.read(self.root)
        self.assertEqual([(e["event"], e["attempt"], e["line"]) for e in events], [("dispatched", 1, "research")] * 1 + [("dispatched", 2, "research")])


class TwoSchedulersAtOnce(InstanceCase):
    def test_no_row_is_dispatched_twice(self):
        self.edit_policy(max_concurrency=100)
        self.write_board(board(("research", 60, rows("demo-notes", 14, "r")), ("build", 40, rows("demo-bikes", 10, "b"))))
        workers = [run_worker("dispatch", self.root, f"sched-{n}") for n in range(4)]
        lines = [line for w in workers for line in w.communicate(timeout=120)[0].splitlines()]
        claimed = [line.split(" ", 1)[1] for line in lines if not line.startswith("REFUSED")]
        self.assertEqual(len(claimed), 24)
        self.assertEqual(len(set(claimed)), 24)
        self.assertEqual(sum(1 for t in backlog.read(self.root / "projects" / "backlog.md").tasks if t.state == "running"), 24)
        dispatched = [e for e in ledger.read(self.root) if e["event"] == "dispatched"]
        self.assertEqual(len(dispatched), 24)

    def test_concurrency_cap_holds_across_processes(self):
        self.write_board(board(("research", 60, rows("demo-notes", 6, "r")), ("build", 40, rows("demo-bikes", 6, "b"))))
        workers = [run_worker("dispatch", self.root, f"sched-{n}") for n in range(4)]
        lines = [line for w in workers for line in w.communicate(timeout=120)[0].splitlines()]
        self.assertEqual(sum(1 for line in lines if not line.startswith("REFUSED")), 1)
        self.assertEqual(sum(1 for line in lines if line == "REFUSED busy"), 4)  # every worker ends refused, one after its own claim


if __name__ == "__main__":
    unittest.main()
