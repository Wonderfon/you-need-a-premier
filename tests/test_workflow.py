import json
import unittest
from datetime import datetime, timedelta, timezone

from orgcore import backlog, ledger, scheduler, workflow
from orgcore.fake import FakeExecutor, FakeReviewer
from orgcore.usage import FakeUsage

from .support import InstanceCase, board, run_worker

NOW = datetime(2026, 1, 5, 12, 0, 0, tzinfo=timezone.utc)
SOLO = board(("research", 60, ["- [todo] demo-notes: one"]), ("build", 40, []))


class Roles(InstanceCase):
    def setUp(self):
        super().setUp()
        self.write_board(SOLO)
        self.inst = self.instance()
        self.task = scheduler.dispatch(self.inst, FakeUsage.healthy(self.inst.policy, NOW), "executor-1", NOW).task
        self.key = self.task.key

    def state(self):
        return backlog.read(self.inst.backlog_path).tasks[0].state

    def test_only_the_dispatched_executor_can_submit_and_only_to_review(self):
        with self.assertRaises(workflow.RoleError):
            workflow.submit(self.inst, self.key, "executor-2", "demo/x.txt")
        with self.assertRaises(ValueError):
            workflow.submit(self.inst, self.key, "executor-1", "  ")
        self.assertEqual(self.state(), "running")
        workflow.submit(self.inst, self.key, "executor-1", "demo/x.txt")
        self.assertEqual(self.state(), "review")
        with self.assertRaises(KeyError):  # no way for the executor to move it further with this API
            workflow.submit(self.inst, self.key, "executor-1", "demo/x.txt")

    def test_executor_cannot_review_its_own_work_and_verdict_needs_evidence(self):
        workflow.submit(self.inst, self.key, "executor-1", "demo/x.txt")
        with self.assertRaises(workflow.RoleError):
            workflow.review(self.inst, self.key, "executor-1", "pass", "looks fine")
        for bad in (("reviewer-1", "pass", " "), ("reviewer-1", "maybe", "text")):
            with self.assertRaises(ValueError):
                workflow.review(self.inst, self.key, *bad)
        self.assertEqual(self.state(), "review")

    def test_verdicts_map_to_states_and_rework_keeps_the_reason(self):
        workflow.submit(self.inst, self.key, "executor-1", "demo/x.txt")
        workflow.review(self.inst, self.key, "reviewer-1", "rework", "missing the Sources line [see rule 2]")
        row = backlog.read(self.inst.backlog_path).tasks[0]
        self.assertEqual((row.state, row.detail), ("rework", "missing the Sources line [see rule 2"))
        again = scheduler.dispatch(self.inst, FakeUsage.healthy(self.inst.policy, NOW), "executor-2", NOW)
        self.assertEqual(again.attempt, 2)
        workflow.submit(self.inst, self.key, "executor-2", "demo/x.txt")
        with self.assertRaises(workflow.RoleError):  # reviewer1 is fine, but the first executor is not
            workflow.review(self.inst, self.key, "executor-1", "pass", "ok")
        workflow.review(self.inst, self.key, "reviewer-1", "pass", "all good")
        self.assertEqual(self.state(), "done")
        kinds = [(e["event"], e["actor"]) for e in ledger.read(self.root)]
        self.assertEqual(kinds, [("dispatched", "executor-1"), ("submitted", "executor-1"), ("reviewed", "reviewer-1"),
                                 ("dispatched", "executor-2"), ("submitted", "executor-2"), ("reviewed", "reviewer-1")])

    def test_feedback_only_for_finished_rows_and_known_ratings(self):
        with self.assertRaises(KeyError):
            workflow.record_feedback(self.inst, self.key, "useful")
        workflow.submit(self.inst, self.key, "executor-1", "demo/x.txt")
        workflow.review(self.inst, self.key, "reviewer-1", "pass", "ok")
        with self.assertRaises(ValueError):
            workflow.record_feedback(self.inst, self.key, "great")
        event = workflow.record_feedback(self.inst, self.key, "not_useful", "too short", NOW)
        self.assertEqual(ledger.read(self.root, ledger.FEEDBACK), [event])

    def test_fake_workers(self):
        executor, reviewer = FakeExecutor(), FakeReviewer()
        first = executor.work(self.root, self.key, 1)
        self.assertEqual(reviewer.judge(self.root, first)[0], "rework")
        self.assertEqual(executor.work(self.root, self.key, 2), first)
        self.assertEqual(reviewer.judge(self.root, first)[0], "pass")
        self.assertEqual(reviewer.judge(self.root, "../outside.txt")[0], "blocked")


class Recovery(InstanceCase):
    def test_live_long_running_row_is_not_recovered_but_a_dead_one_is(self):
        self.write_board(board(("research", 60, ["- [running 2026-01-01T00:00:00Z] demo-notes: slow"]), ("build", 40, [])))
        inst = self.instance()
        key = ("research", "demo-notes", "slow")
        holder = run_worker("holdclaim", self.root, json.dumps(key))
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            reset = backlog.recover_running(inst.backlog_path, timedelta(hours=1), NOW, alive=workflow.worker_alive(inst.root))
            self.assertEqual(reset, [])
            self.assertEqual(backlog.read(inst.backlog_path).tasks[0].state, "running")
        finally:
            holder.kill()
            holder.wait()
            holder.stdout.close()
        reset = backlog.recover_running(inst.backlog_path, timedelta(hours=1), NOW, alive=workflow.worker_alive(inst.root))
        self.assertEqual([t.text for t in reset], ["slow"])
        self.assertEqual(backlog.read(inst.backlog_path).tasks[0].state, "todo")

    def test_ledger_torn_last_line_is_dropped_on_the_next_append(self):
        ledger.append(self.root, "t.jsonl", NOW, event="a")
        path = ledger.path_of(self.root, "t.jsonl")
        with path.open("ab") as stream:
            stream.write(b'{"event": "half')
        self.assertEqual([e["event"] for e in ledger.read(self.root, "t.jsonl")], ["a"])
        ledger.append(self.root, "t.jsonl", NOW, event="b")
        self.assertEqual([e["event"] for e in ledger.read(self.root, "t.jsonl")], ["a", "b"])
        with path.open("ab") as stream:
            stream.write(b"garbage\n")
        ledger.append(self.root, "t.jsonl", NOW, event="c")  # a complete bad line in the middle is never silently dropped
        with self.assertRaises(ValueError):
            ledger.read(self.root, "t.jsonl")


if __name__ == "__main__":
    unittest.main()
