import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orgcore import backlog
from orgcore.backlog import Backlog, ParseError

ROOT = Path(__file__).resolve().parent.parent
WORKER = Path(__file__).with_name("_worker.py")
NOW = datetime(2026, 1, 5, 12, 0, 0, tzinfo=timezone.utc)

SAMPLE = """# Backlog

## Line: research
Weight: 60%
- [todo] demo-notes: first
- [running 2026-01-05T10:00:00Z] demo-notes: second
- [review] demo-garden: third

## Line: build
Weight: 40%
- [blocked needs parts] demo-bikes: fourth
"""


def run_worker(mode, path, arg=""):
    return subprocess.Popen([sys.executable, str(WORKER), mode, str(path), arg], stdout=subprocess.PIPE, text=True, cwd=ROOT)


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="orgcore-test-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = self.dir / "projects" / "backlog.md"
        self.path.parent.mkdir()


class RoundTrip(unittest.TestCase):
    def test_untouched_board_is_byte_identical(self):
        texts = [
            SAMPLE,
            SAMPLE.rstrip("\n"),  # no trailing newline
            SAMPLE.replace("\n", "\r\n"),  # Windows line endings
            "﻿" + SAMPLE,  # byte order mark
            SAMPLE.replace("third", "café — 测试 \U0001f600"),
            "",
            "\n\n",
            "free prose\n\n## Line: a\n\nmore prose\n",
        ]
        for text in texts:
            with self.subTest(text=text[:20]):
                self.assertEqual(Backlog(text).serialize(), text)

    def test_state_change_touches_only_its_row(self):
        text = SAMPLE.replace("\n", "\r\n")
        board = Backlog(text)
        task = board.find(("research", "demo-notes", "first"), "todo")
        board.set_state(task, "running", "2026-01-05T12:00:00Z")
        before, after = text.split("\n"), board.serialize().split("\n")
        changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        self.assertEqual(changed, [task.index])
        self.assertEqual(after[task.index], "- [running 2026-01-05T12:00:00Z] demo-notes: first\r")

    def test_parsed_fields(self):
        board = Backlog(SAMPLE)
        self.assertEqual(board.weights, {"research": 60.0, "build": 40.0})
        self.assertEqual([t.state for t in board.tasks], ["todo", "running", "review", "blocked"])
        self.assertEqual(board.tasks[3].detail, "needs parts")


class BadInput(unittest.TestCase):
    def errors(self, text):
        with self.assertRaises(ParseError) as caught:
            Backlog(text)
        return caught.exception.errors

    def test_each_kind_of_bad_row_is_reported_with_its_line_number(self):
        cases = {
            "## Line: a\n- [todo] no colon here\n": (2, "malformed"),
            "## Line: a\n- [started] p: x\n": (2, "unknown state"),
            "## Line: a\n- [running] p: x\n": (2, "running row needs"),
            "## Line: a\n- [running yesterday] p: x\n": (2, "running row needs"),
            "- [todo] p: x\n": (1, "outside a line section"),
            "## Chapter one\n": (1, "section heading"),
            "## Line: a\n## Line: a\n": (2, "duplicate"),
            "## Line: a\nWeight: lots\n": (2, "weight row must be"),
            "## Line: a\nWeight: 0%\n": (2, "weight row must be"),
            "## Line: a\nWeight: 5%\nWeight: 6%\n": (3, "second weight"),
            "Weight: 5%\n": (1, "outside a line section"),
        }
        for text, (number, fragment) in cases.items():
            with self.subTest(text=text):
                found = self.errors(text)
                self.assertEqual(found[0][0], number)
                self.assertIn(fragment, found[0][1])

    def test_all_errors_are_collected_not_just_the_first(self):
        found = self.errors("## Line: a\n- [todo] bad\n- [nope] p: x\n")
        self.assertEqual([n for n, _ in found], [2, 3])

    def test_bad_board_on_disk_is_never_rewritten(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        path = self.dir / "b.md"
        path.write_text("## Line: a\n- [todo] broken\n", encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaises(ParseError):
            backlog.claim(path)
        self.assertEqual(path.read_bytes(), before)

    def test_illegal_transition_and_bad_detail(self):
        board = Backlog(SAMPLE)
        with self.assertRaises(ValueError):
            board.set_state(board.tasks[0], "done")  # todo -> done skips review
        with self.assertRaises(ValueError):
            board.set_state(board.tasks[0], "blocked", "has ] bracket")

    def test_check_against_config(self):
        board = Backlog(SAMPLE)
        board.check_against({"research": {"demo-notes", "demo-garden"}, "build": {"demo-bikes"}})
        with self.assertRaises(ParseError) as caught:
            board.check_against({"research": {"demo-notes"}, "build": {"demo-bikes"}})
        self.assertEqual(caught.exception.errors[0][0], 7)


class Operations(TempDirCase):
    def test_file_roundtrip_keeps_bytes_and_skips_rewrite(self):
        data = SAMPLE.replace("\n", "\r\n").encode("utf-8")
        self.path.write_bytes(data)
        backlog.update(self.path, lambda board: None)
        self.assertEqual(self.path.read_bytes(), data)

    def test_claim_marks_first_todo_running_with_time(self):
        self.path.write_text(SAMPLE, encoding="utf-8")
        task = backlog.claim(self.path, NOW)
        self.assertEqual(task.text, "first")
        self.assertIn("- [running 2026-01-05T12:00:00Z] demo-notes: first", self.path.read_text(encoding="utf-8"))
        self.assertIsNone(backlog.claim(self.path, NOW))

    def test_review_rework_cycle(self):
        self.path.write_text("## Line: a\n- [todo] p: x\n", encoding="utf-8")
        key = ("a", "p", "x")
        backlog.claim(self.path, NOW)
        for current, new in [("running", "review"), ("review", "rework"), ("rework", "running"),
                             ("running", "review"), ("review", "done")]:
            backlog.transition(self.path, key, current, new, format_detail(new))
        self.assertIn("- [done", self.path.read_text(encoding="utf-8"))
        with self.assertRaises(KeyError):
            backlog.transition(self.path, key, "todo", "running")

    def test_add_task_goes_to_end_of_its_section(self):
        self.path.write_text(SAMPLE, encoding="utf-8")
        backlog.add_task(self.path, "research", "demo-garden", "added")
        lines = self.path.read_text(encoding="utf-8").split("\n")
        self.assertEqual(lines[lines.index("- [review] demo-garden: third") + 1], "- [todo] demo-garden: added")
        self.assertEqual(backlog.read(self.path).tasks[-1].text, "fourth")

    def test_recover_running_resets_only_old_rows(self):
        self.path.write_text(SAMPLE.replace("- [review]", "- [running 2026-01-05T11:59:00Z]"), encoding="utf-8")
        reset = backlog.recover_running(self.path, timedelta(minutes=30), NOW)
        self.assertEqual([t.text for t in reset], ["second"])
        states = {t.text: t.state for t in backlog.read(self.path).tasks}
        self.assertEqual(states["second"], "todo")
        self.assertEqual(states["third"], "running")
        self.assertEqual(backlog.recover_running(self.path, timedelta(minutes=30), NOW), [])


def format_detail(state):
    return "2026-01-05T12:00:00Z" if state == "running" else ""


class Concurrency(TempDirCase):
    def test_parallel_claimers_never_take_the_same_row(self):
        rows = [f"- [todo] demo-notes: job-{n:02d}" for n in range(60)]
        self.path.write_text("## Line: research\n" + "\n".join(rows) + "\n", encoding="utf-8")
        workers = [run_worker("claim", self.path) for _ in range(4)]
        claimed = [line for w in workers for line in w.communicate(timeout=120)[0].split()]
        self.assertEqual(len(claimed), 60)
        self.assertEqual(len(set(claimed)), 60)
        self.assertTrue(all(t.state == "running" for t in backlog.read(self.path).tasks))

    def test_parallel_writers_do_not_overwrite_each_other(self):
        self.path.write_text("## Line: research\nWeight: 100%\n- [todo] demo-notes: seed\n", encoding="utf-8")
        workers = [run_worker("add", self.path, f"w{n}") for n in range(3)]
        for worker in workers:
            worker.communicate(timeout=120)
            self.assertEqual(worker.returncode, 0)
        texts = {t.text for t in backlog.read(self.path).tasks}
        self.assertEqual(texts, {"seed"} | {f"w{n}-{i}" for n in range(3) for i in range(25)})

    def test_lock_is_released_when_the_holder_is_killed(self):
        self.path.write_text(SAMPLE, encoding="utf-8")
        holder = run_worker("hold", self.path)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        with self.assertRaises(TimeoutError):
            with backlog.file_lock(self.path, timeout=0.3):
                pass
        holder.kill()
        holder.wait()
        holder.stdout.close()
        self.assertIsNotNone(backlog.claim(self.path, NOW))  # no stale-lock wait, no manual cleanup


if __name__ == "__main__":
    unittest.main()
