import unittest

from orgcore import check, intent

from .support import InstanceCase, board

GOOD = '''# Intent: demo-notes

- [I-001] 2026-01-05 | source: chat-1 | "Keep the notes under one page."
- [I-002] 2026-01-09 | source: chat-2 | "Add a reading list." | superseded-by: I-003
- [I-003] 2026-01-12 | source: chat-3 | "Add a reading list, five items at most."
'''


def rows(*extra):
    return board(("research", 60, [*extra, "- [todo] demo-garden: plain row"]), ("build", 40, []))


class IntentCase(InstanceCase):
    def write_intent(self, project, text):
        path = self.root / "projects" / "intent" / f"{project}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


class IntentFile(IntentCase):
    def test_parse_reads_words_date_source_and_supersession(self):
        entries = intent.parse(GOOD)
        self.assertEqual([e.id for e in entries], ["I-001", "I-002", "I-003"])
        self.assertEqual(entries[0].quote, "Keep the notes under one page.")
        self.assertEqual((entries[0].date, entries[0].source), ("2026-01-05", "chat-1"))
        self.assertEqual(entries[1].superseded_by, "I-003")

    def test_every_problem_is_reported_with_its_line_number(self):
        bad = GOOD + '- [I-004] 2026-13-01 | source: x | "bad month"\n- [I-001] 2026-02-01 | source: y | "dup"\n- [I-5] oops\n'
        with self.assertRaises(intent.IntentError) as raised:
            intent.parse(bad, "f.md")
        text = "\n".join(raised.exception.errors)
        self.assertIn("f.md line 6: '2026-13-01' is not a real date", text)
        self.assertIn("f.md line 7: duplicate id I-001", text)
        self.assertIn("f.md line 8: malformed entry", text)

    def test_add_numbers_entries_and_can_supersede(self):
        first = intent.add(self.root, "demo-notes", "Keep it short.", "chat-1", "2026-01-05")
        second = intent.add(self.root, "demo-notes", "Keep it to one page.", "chat-2", "2026-01-06", supersedes=first.id)
        self.assertEqual((first.id, second.id), ("I-001", "I-002"))
        entries = intent.read(self.root, "demo-notes")
        self.assertEqual(entries[0].superseded_by, "I-002")
        self.assertIsNone(entries[1].superseded_by)
        self.assertEqual(intent.read(self.root, "demo-garden"), [])  # no file: no entries, no error

    def test_add_refuses_bad_input_and_never_overwrites_a_damaged_file(self):
        with self.assertRaises(intent.IntentError):
            intent.add(self.root, "demo-notes", 'has "quotes"', "chat-1", "2026-01-05")
        with self.assertRaises(intent.IntentError):
            intent.add(self.root, "demo-notes", "ok", "chat-1", "2026-01-05", supersedes="I-009")
        path = self.write_intent("demo-notes", "- [I-001] broken line\n")
        with self.assertRaises(intent.IntentError):
            intent.add(self.root, "demo-notes", "ok", "chat-1", "2026-01-05")
        self.assertEqual(path.read_text(encoding="utf-8"), "- [I-001] broken line\n")


class IntentReferences(IntentCase):
    def errors(self, *task_rows):
        self.write_board(rows(*task_rows))
        return check.check_instance(self.root)[0]

    def test_a_valid_citation_passes(self):
        self.write_intent("demo-notes", GOOD)
        self.assertEqual(self.errors("- [todo] demo-notes: write it (intent: I-001, I-003)"), [])

    def test_citing_an_intent_that_does_not_exist_is_reported(self):
        self.write_intent("demo-notes", GOOD)
        errors = self.errors("- [todo] demo-notes: write it (intent: I-009)")
        self.assertEqual(len(errors), 1)
        self.assertIn("cites I-009, which is not in projects/intent/demo-notes.md", errors[0])
        self.assertIn("backlog.md line 5", errors[0])

    def test_citing_a_superseded_intent_is_reported(self):
        self.write_intent("demo-notes", GOOD)
        self.assertIn("I-002, superseded by I-003", self.errors("- [todo] demo-notes: x (intent: I-002)")[0])

    def test_citing_when_the_project_has_no_intent_file_is_reported(self):
        self.assertIn("projects/intent/demo-notes.md does not exist", self.errors("- [todo] demo-notes: x (intent: I-001)")[0])

    def test_rows_without_a_citation_need_no_intent_file(self):
        self.assertEqual(self.errors("- [todo] demo-notes: nothing cited"), [])

    def test_a_damaged_intent_file_is_reported_not_swallowed(self):
        self.write_intent("demo-notes", GOOD + "- [I-004] not an entry\n")
        errors = self.errors("- [todo] demo-notes: x (intent: I-001)")
        self.assertEqual(len(errors), 2)
        self.assertIn("projects/intent/demo-notes.md line 6: malformed entry", errors[0])
        self.assertIn("fix the file and run check again to verify the citation", errors[1])
        self.assertIn("backlog.md line 5", errors[1])

    def test_a_bad_entry_is_not_reported_as_missing_when_another_entry_is_superseded_by_it(self):
        text = GOOD.replace("2026-01-12", "2026-02-30")  # I-003 exists but its date is not real
        with self.assertRaises(intent.IntentError) as raised:
            intent.parse(text, "f.md")
        self.assertEqual(raised.exception.errors, ["f.md line 5: '2026-02-30' is not a real date"])
        malformed = GOOD.replace('"Add a reading list, five items at most."', "oops")
        with self.assertRaises(intent.IntentError) as raised:
            intent.parse(malformed, "f.md")
        self.assertEqual(len(raised.exception.errors), 1)
        self.assertIn("malformed entry", raised.exception.errors[0])
        with self.assertRaises(intent.IntentError) as raised:  # a pointer to an id that is nowhere is still reported
            intent.parse(GOOD.replace("superseded-by: I-003", "superseded-by: I-009"), "f.md")
        self.assertIn("I-002 is superseded by I-009, which does not exist", raised.exception.errors[0])

    def test_check_command_fails_on_a_bad_citation(self):
        from .support import run_cli
        self.write_board(rows("- [todo] demo-notes: x (intent: I-001)"))
        result = run_cli("check", self.root)
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not exist", result.stderr)


if __name__ == "__main__":
    unittest.main()
