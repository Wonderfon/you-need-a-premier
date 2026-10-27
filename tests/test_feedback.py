import unittest
from datetime import datetime, timezone

from orgcore import feedback, ledger

from .support import InstanceCase, board, run_cli

NOW = datetime(2026, 1, 5, 12, 0, 0, tzinfo=timezone.utc)
KEYS = {name: ("research", "demo-notes", name) for name in ("a", "b", "c", "d")}


class FeedbackSummary(InstanceCase):
    def setUp(self):
        super().setUp()
        self.write_board(board(("research", 60, [
            "- [done 2026-01-05T09:00:00Z] demo-notes: a", "- [done 2026-01-05T09:00:00Z] demo-notes: b",
            "- [done 2026-01-05T09:00:00Z] demo-notes: c", "- [todo] demo-notes: d"]), ("build", 40, [])))

    def rate(self, name, rating):
        ledger.append(self.root, ledger.FEEDBACK, NOW, event="feedback", task=ledger.task_id(KEYS[name]), rating=rating, note="")

    def test_latest_rating_wins_and_the_three_views(self):
        self.rate("a", "useful")
        self.rate("a", "rework")  # a later rating replaces the earlier one
        self.rate("b", "useful")
        summary = feedback.summarize(self.instance())
        self.assertEqual(summary.latest[ledger.task_id(KEYS["a"])]["rating"], "rework")
        self.assertEqual(summary.counts(), {"useful": 1, "not_useful": 0, "rework": 1})
        self.assertEqual([t.text for t in summary.unrated], ["c"])  # "d" is not finished, so it is not listed
        self.assertEqual(summary.rework, [ledger.task_id(KEYS["a"])])

    def test_a_rating_for_a_row_no_longer_on_the_board_is_still_counted_and_named_honestly(self):
        ledger.append(self.root, ledger.FEEDBACK, NOW, event="feedback", task="0123456789", rating="rework", note="")
        text = "\n".join(feedback.describe(feedback.summarize(self.instance())))
        self.assertIn("0123456789 (no longer on the board)", text)

    def test_empty_file(self):
        summary = feedback.summarize(self.instance())
        self.assertEqual((summary.latest, summary.rework, len(summary.unrated)), ({}, [], 3))

    def test_command(self):
        self.rate("a", "rework")
        result = run_cli("feedback", self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Finished rows without a rating: 2", result.stdout)
        self.assertIn("Rows the owner rated 'rework': 1", result.stdout)
        self.assertIn("research/demo-notes: a", result.stdout)


if __name__ == "__main__":
    unittest.main()
