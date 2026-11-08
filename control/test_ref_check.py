import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import ref_check as rc


class ReferenceCheck(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.root = self.workspace / "incore"
        (self.root / "control" / "roles").mkdir(parents=True)
        (self.root / "records").mkdir()
        (self.root / "records" / "policy.json").write_text("{}", encoding="utf-8")
        self.source = self.root / "control" / "roles" / "rules.md"

    def check_text(self, text):
        self.source.write_text(text, encoding="utf-8")
        return rc.scan(self.root, ("control/roles/*.md",))

    def test_default_sources_scan_the_private_layer_roles(self):
        layer = self.root / "data" / "config-sync" / "private-layer" / "roles"
        layer.mkdir(parents=True)
        (layer / "tick-rules.md").write_text("records/policy.json records/gone.json\n", encoding="utf-8")
        self.source.write_text("# placeholder\n", encoding="utf-8")
        result = rc.scan(self.root, rc.CONFIG["files"])
        self.assertEqual((result["files"], result["references"], len(result["missing"])), (2, 2, 1))
        self.assertIn("data/config-sync/private-layer/roles/tick-rules.md", str(result["missing"][0]))

    def test_existing_file_and_directory(self):
        result = self.check_text("`records/policy.json`、incore/records/\n")
        self.assertEqual((result["files"], result["references"], result["missing"]), (1, 2, []))

    def test_missing_file_reports_location_and_same_directory_candidates(self):
        result = self.check_text("# 规则\n出处：records/polciy.json\n")
        self.assertEqual(result["missing"], [{
            "source": "control/roles/rules.md", "line": 2,
            "path": "records/polciy.json", "candidates": ["records/policy.json"],
        }])
        rendered = rc.render(result)
        self.assertIn("### control/roles/rules.md", rendered)
        self.assertIn("第 2 行", rendered)
        self.assertIn("records/policy.json", rendered)

    def test_line_numbers_and_ranges_are_removed(self):
        for suffix in (":12", ":12-16", ":12–16", ":6,15"):
            with self.subTest(suffix=suffix):
                result = self.check_text(f"records/policy.json{suffix}\n")
                self.assertEqual((result["references"], result["missing"]), (1, []))

    def test_placeholders_are_skipped_without_checking_partial_names(self):
        result = self.check_text("audits/<日期>-<主题>.md projects/intent/<项目>.md records/*.json records/* records/**\n")
        self.assertEqual((result["references"], result["skipped"], result["missing"]), (0, 5, []))

    def test_trailing_punctuation_and_markdown_are_removed(self):
        for suffix in ("」，。)", "）", ".", ";", "：后文", "、别的", "**。", "〕。", "】", "》"):
            with self.subTest(suffix=suffix):
                result = self.check_text(f"见records/policy.json{suffix}\n")
                self.assertEqual((result["references"], result["missing"]), (1, []))

    def test_adjacent_references_and_unicode_names(self):
        (self.root / "records" / "原话.json").write_text("{}", encoding="utf-8")
        result = self.check_text("records/原话.json，audits/缺失.md:3）。\n")
        self.assertEqual(result["references"], 2)
        self.assertEqual(result["missing"][0]["path"], "audits/缺失.md")
        self.assertEqual(result["missing"][0]["candidates"], [])

    def test_cli_default_sources_include_workspace_agents_and_do_not_write(self):
        self.source.write_text("records/polciy.json\n", encoding="utf-8")
        (self.workspace / "AGENTS.md").write_text("records/policy.json\n", encoding="utf-8")
        before = {p.relative_to(self.workspace): p.read_bytes() for p in self.workspace.rglob("*") if p.is_file()}
        completed = subprocess.run(
            [sys.executable, str(Path(rc.__file__)), "--root", str(self.root)],
            capture_output=True, text=True, encoding="utf-8", check=True,
        )
        after = {p.relative_to(self.workspace): p.read_bytes() for p in self.workspace.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertIn("扫了 2 个文件，引用 2 条，缺 1 条", completed.stdout)
        self.assertIn("control/roles/rules.md", completed.stdout)


if __name__ == "__main__":
    unittest.main()
