import unittest
from datetime import datetime, timezone

from orgcore import backlog, ledger, rehearsal
from orgcore.usage import FakeUsage

from .support import InstanceCase, board, run_cli

NOW = datetime(2026, 1, 5, 12, 0, 0, tzinfo=timezone.utc)
ROWS = board(("research", 60, ["- [todo] demo-notes: write the notes", "- [todo] demo-garden: list plants"]),
             ("build", 40, ["- [todo] demo-bikes: plan the repair"]))


class Boom(Exception):
    pass


class Rehearsal(InstanceCase):
    def setUp(self):
        super().setUp()
        self.write_board(ROWS)

    def events(self):
        return [(e["event"], e.get("attempt"), e.get("verdict")) for e in ledger.read(self.root)]

    def check_one_clean_circle(self):
        self.assertEqual(self.events(), [("dispatched", 1, None), ("submitted", 1, None), ("reviewed", 1, "rework"),
                                         ("dispatched", 2, None), ("submitted", 2, None), ("reviewed", 2, "pass")])
        rows = backlog.read(self.root / "projects" / "backlog.md").tasks
        self.assertEqual([t.state for t in rows], ["done", "todo", "todo"])
        feedback = ledger.read(self.root, ledger.FEEDBACK)
        self.assertEqual([e["rating"] for e in feedback], ["useful"])
        self.assertIn("Sources: none", next((self.root / "demo").glob("*.txt")).read_text(encoding="utf-8"))

    def test_full_circle_in_process(self):
        out = rehearsal.rehearse(self.instance(), "useful", now=NOW)
        self.check_one_clean_circle()
        text = "\n".join(out)
        for step in ("todo -> running", "running -> review", "review -> rework", "rework -> running", "review -> done",
                     "owner feedback saved: useful", "Model requests: 0. Messages sent: 0."):
            self.assertIn(step, text)

    def test_second_run_does_not_start_more_work(self):
        rehearsal.rehearse(self.instance(), "useful", now=NOW)
        out = rehearsal.rehearse(self.instance(), "useful", now=NOW)
        self.check_one_clean_circle()
        self.assertEqual(out, ["Model requests: 0. Messages sent: 0."])

    def test_tasks_option_takes_several_rows_through(self):
        rehearsal.rehearse(self.instance(), "rework", tasks=2, now=NOW)
        states = [t.state for t in backlog.read(self.root / "projects" / "backlog.md").tasks]
        self.assertEqual(sorted(states), ["done", "done", "todo"])
        self.assertEqual(len(ledger.read(self.root, ledger.FEEDBACK)), 2)

    def test_unknown_usage_means_nothing_runs(self):
        out = rehearsal.rehearse(self.instance(), "useful", source=FakeUsage([]), now=NOW)
        self.assertTrue(out[0].startswith("Stopped: usage: "))
        self.assertEqual(ledger.read(self.root), [])
        self.assertTrue(all(t.state == "todo" for t in backlog.read(self.root / "projects" / "backlog.md").tasks))

    def test_only_demo_projects_are_touched(self):
        def not_demo(org):
            for line in org["lines"]:
                for project in line["projects"]:
                    project["demo"] = project["id"] != "demo-bikes"
        self.edit_org(not_demo)
        rehearsal.rehearse(self.instance(), "useful", tasks=3, now=NOW)
        states = {t.project: t.state for t in backlog.read(self.root / "projects" / "backlog.md").tasks}
        self.assertEqual(states["demo-bikes"], "todo")
        self.assertEqual({states["demo-notes"], states["demo-garden"]}, {"done"})

    def test_killed_after_the_pass_verdict_still_gets_its_rating_and_no_extra_work(self):
        seen = []

        def die_on_second_review(step):
            seen.append(step)
            if seen.count("reviewed") == 2:
                raise Boom

        with self.assertRaises(Boom):
            rehearsal.rehearse(self.instance(), "useful", now=NOW, after_step=die_on_second_review)
        self.assertEqual(ledger.read(self.root, ledger.FEEDBACK), [])
        rehearsal.rehearse(self.instance(), "useful", now=NOW)
        self.check_one_clean_circle()


class KilledProcess(InstanceCase):
    """A real process dies with no cleanup at each step, then the same command is run again."""

    def test_each_kill_point_resumes_the_same_row(self):
        for step in rehearsal.STEPS:
            with self.subTest(killed_after=step):
                self.setUp()
                self.write_board(ROWS)
                crashed = run_cli("rehearse", self.root, "--rating", "useful", "--crash-after", step)
                self.assertEqual(crashed.returncode, 75, crashed.stderr)
                self.assertIn("claimed attempt 1", crashed.stdout)  # what happened before the crash is visible
                resumed = run_cli("rehearse", self.root, "--rating", "useful")
                self.assertEqual(resumed.returncode, 0, resumed.stderr)
                events = [(e["event"], e.get("attempt")) for e in ledger.read(self.root)]
                self.assertEqual(events.count(("dispatched", 1)), 1)
                self.assertEqual(events.count(("dispatched", 2)), 1)
                self.assertEqual(sum(1 for e, _ in events if e == "dispatched"), 2)
                states = [t.state for t in backlog.read(self.root / "projects" / "backlog.md").tasks]
                self.assertEqual(states, ["done", "todo", "todo"])
                self.assertEqual(len(ledger.read(self.root, ledger.FEEDBACK)), 1)

    def test_cli_prints_the_whole_circle(self):
        done = run_cli("rehearse", self.root, "--rating", "useful")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("review -> done", done.stdout)
        self.assertIn("owner feedback saved: useful", done.stdout)
        self.assertEqual(run_cli("check", self.root).returncode, 0)


if __name__ == "__main__":
    unittest.main()
