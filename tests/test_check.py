import unittest

from orgcore import check

from .support import InstanceCase, board


class OnePass(InstanceCase):
    def errors(self):
        return "\n".join(check.check_instance(self.root)[0])

    def test_demo_is_clean(self):
        self.assertEqual(check.check_instance(self.root)[0], [])

    def test_bad_board_rows_and_config_problems_are_reported_in_one_run(self):
        self.edit_org(lambda org: org["lines"][0].pop("weight"))
        self.write_board("## Line: research\nWeight: 60%\n- [todo] broken row\n- [started] demo-notes: x\n")
        text = self.errors()
        self.assertIn("org.json: lines[0].weight: missing required field", text)
        self.assertIn("projects/backlog.md line 3: malformed task row", text)
        self.assertIn("projects/backlog.md line 4: unknown state 'started'", text)

    def test_weight_disagreement_names_both_values_and_the_authority(self):
        self.write_board(board(("research", 55, ["- [todo] demo-notes: a"]), ("build", 40, [])))
        text = self.errors()
        self.assertIn("weight mismatch for line 'research': org.json 60, projects/backlog.md 55", text)
        self.assertIn("the board is authoritative", text)

    def test_missing_weight_row_missing_section_unknown_project_and_duplicates_together(self):
        self.write_board("## Line: research\n- [todo] ghost: a\n- [todo] demo-notes: same\n- [todo] demo-notes: same\n"
                         "## Line: extra\nWeight: 5%\n")
        text = self.errors()
        self.assertIn("line 'research' has no Weight row", text)
        self.assertIn("line 'build' has no section", text)
        self.assertIn("project 'ghost' is not in line 'research'", text)
        self.assertIn("section 'extra' is not a configured line", text)
        self.assertIn("duplicate of line 3", text)

    def test_missing_board_file_is_one_clear_error(self):
        (self.root / "projects" / "backlog.md").unlink()
        self.assertIn("projects/backlog.md: file not found", self.errors())


if __name__ == "__main__":
    unittest.main()
