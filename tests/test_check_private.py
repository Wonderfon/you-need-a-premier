import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

import importlib.util

spec = importlib.util.spec_from_file_location("check_private", ROOT / "tools" / "check_private.py")
check_private = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_private)

GIT = os.environ.get("ORGCORE_GIT") or shutil.which("git")


def run_check(*args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = check_private.main(["check_private.py", *map(str, args)])
    return code, out.getvalue()


@unittest.skipUnless(GIT, "git not available")
class CheckPrivate(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="orgcore-priv-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.repo = self.dir / "repo"
        self.repo.mkdir()
        self.banned = self.dir / "banned.txt"
        self.banned.write_text("# comment\nzzqq-secret\nre:\\bticket-\\d{4}\\b\n", encoding="utf-8")
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "dev")
        self.git("config", "user.email", "dev@example.invalid")

    def git(self, *args):
        subprocess.run([GIT, "-c", "safe.directory=*", "-C", str(self.repo), *args], check=True, capture_output=True)

    def commit(self, message="work"):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def test_clean_repo_reports_zero(self):
        (self.repo / "a.txt").write_text("nothing to see\n", encoding="utf-8")
        self.commit()
        code, out = run_check(self.banned, self.repo)
        self.assertEqual((code, out.strip()), (0, "hits: 0"))

    def test_tree_hit_reports_file_and_line_but_never_the_term(self):
        (self.repo / "a.txt").write_text("fine\nhas ZZQQ-Secret inside\nticket-1234 here\n", encoding="utf-8")
        self.commit()
        code, out = run_check(self.banned, self.repo)
        self.assertEqual(code, 1)
        self.assertIn("a.txt:2", out)
        self.assertIn("a.txt:3", out)
        self.assertNotIn("zzqq", out.lower())
        self.assertNotIn("1234", out)

    def test_credential_shapes_are_found_without_a_ban_list_entry_and_never_printed(self):
        fake = "sk-ant-" + "a1B2c3D4e5" * 3  # built at run time so this file holds no credential-shaped text
        (self.repo / "a.txt").write_text(f"fine\nkey: {fake}\n", encoding="utf-8")
        (self.repo / "b.txt").write_text('password = "' + "x9" * 10 + '"\n', encoding="utf-8")
        self.commit()
        (self.repo / "a.txt").write_text("clean now\n", encoding="utf-8")
        self.commit()
        code, out = run_check(self.banned, self.repo)
        self.assertEqual(code, 1)
        self.assertIn("b.txt:1", out)
        self.assertRegex(out, r"history \w+: a\.txt:\d+")
        self.assertNotIn("a1B2c3", out)
        self.assertNotIn("x9x9", out)

    def test_history_hit_survives_deletion_from_the_tree(self):
        (self.repo / "a.txt").write_text("zzqq-secret\n", encoding="utf-8")
        self.commit()
        (self.repo / "a.txt").write_text("clean now\n", encoding="utf-8")
        self.commit()
        code, out = run_check(self.banned, self.repo)
        self.assertEqual(code, 1)
        self.assertIn("history", out)
        self.assertIn("a.txt", out)
        self.assertNotIn("zzqq", out.lower())

    def test_commit_message_and_file_name_hits(self):
        (self.repo / "zzqq-secret.txt").write_text("ok\n", encoding="utf-8")
        self.commit("mentions ticket-9999")
        code, out = run_check(self.banned, self.repo)
        self.assertEqual(code, 1)
        self.assertIn("file name", out)
        self.assertIn("commit metadata", out)

    def test_a_long_number_rule_hits_ids_but_not_the_digits_of_a_decimal(self):
        rule = self.dir / "digits.txt"
        rule.write_text(check_private.LONG_NUMBER + "\n", encoding="utf-8")
        terms = check_private.load_terms(rule)
        self.assertTrue(check_private.hit(terms, "id " + "7" * 19 + " here"))
        for text in ("total_cost_usd: 0.1234567890123456789", "pi = 3.14159265358979323846", "1234567890"):
            self.assertFalse(check_private.hit(terms, text), text)
    def test_setup_errors_exit_2(self):
        self.assertEqual(run_check(self.dir / "missing.txt", self.repo)[0], 2)
        (self.dir / "empty.txt").write_text("# only a comment\n", encoding="utf-8")
        self.assertEqual(run_check(self.dir / "empty.txt", self.repo)[0], 2)


if __name__ == "__main__":
    unittest.main()
