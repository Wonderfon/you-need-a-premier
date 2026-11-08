import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("md2html.py")


class MarkdownToHtml(unittest.TestCase):
    def render_markdown(self, text):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            source = Path(folder) / "list.md"
            source.write_text(text, encoding="utf-8")
            subprocess.run(
                [sys.executable, str(SCRIPT), str(source)],
                capture_output=True, text=True, encoding="utf-8", check=True,
            )
            return source.with_suffix(".html").read_text(encoding="utf-8").replace("\n", "")

    def test_top_level_ordered_list_keeps_non_one_start(self):
        page = self.render_markdown("7. first\n8. second\n")
        self.assertIn('<ol start="7"><li>first</li><li>second</li></ol>', page)

    def test_ordered_list_closes_before_following_block(self):
        for block, expected in (
            ("paragraph", "<p>paragraph</p>"),
            ("## Heading", "<h2>Heading</h2>"),
            ("> quote", "<blockquote>quote</blockquote>"),
            ("```\ncode\n```", "<pre><code>code</code></pre>"),
            ("| A |\n| --- |\n| B |", "<table>"),
        ):
            with self.subTest(block=block):
                page = self.render_markdown("1. item\n" + block + "\n")
                self.assertIn("<ol><li>item</li></ol>" + expected, page)

    def test_cli_writes_utf8_standalone_page_for_owner_facing_report(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            source = Path(folder) / "report.md"
            source.write_text(
                "# 现场结果\n\n数字和来源：**通过**。\n\n"
                "| 项目 | 来源 |\n| --- | --- |\n"
                "| 数量 | [原文](https://example.test/?a=1&b=2) |\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [sys.executable, str(SCRIPT), str(source)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            )
            page_path = source.with_suffix(".html")
            page = page_path.read_text(encoding="utf-8")

        self.assertEqual(result.stdout.strip(), str(page_path))
        self.assertIn('<meta charset="utf-8">', page)
        self.assertIn("<style>", page)
        self.assertIn("<h1>现场结果</h1>", page)
        self.assertIn("<strong>通过</strong>", page)
        self.assertIn('<a href="https://example.test/?a=1&amp;b=2">原文</a>', page)

    def test_ordered_and_mixed_nested_lists_preserve_start_number(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            source = Path(folder) / "lists.md"
            source.write_text(
                "# Steps\n\n"
                "1. First\n"
                "  - Nested bullet\n"
                "  - Another nested bullet\n"
                "  3. Nested numbered item\n"
                "2. Second\n"
                "- Follow-up\n"
                "  4. Publish\n"
                "  5. Verify\n",
                encoding="utf-8",
            )
            subprocess.run(
                [sys.executable, str(SCRIPT), str(source)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            )
            page = source.with_suffix(".html").read_text(encoding="utf-8")

        compact = re.sub(r">\s+<", "><", re.sub(r"\s+", " ", page))
        self.assertIn(
            "<ol><li>First <ul><li>Nested bullet </li><li>Another nested bullet </li></ul>"
            '<ol start="3"><li>Nested numbered item </li></ol></li><li>Second </li></ol>',
            compact,
        )
        self.assertIn(
            '<ul><li>Follow-up <ol start="4"><li>Publish </li><li>Verify </li></ol></li></ul>',
            compact,
        )


if __name__ == "__main__":
    unittest.main()
