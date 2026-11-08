import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import plain_head_check as ph


class PlainHeadCheck(unittest.TestCase):
    def terms(self, text):
        return {h["term"].strip() for h in ph.check_text(text)["hits"]}

    def test_each_requested_kind(self):
        cases = [
            ("⑩ 简体胜过⑪组。", {"⑩", "⑪"}),
            ("独立判 P/F/U，P 还没解释。", {"P", "F", "U"}),
            ("用 Bootstrap 重采样。", {"Bootstrap"}),
            ("95%区间很窄，置信区间见后文。", {"95%", "置信区间"}),
            ("分支 claude/example 的提交 abc1234 待审。", {"claude/example", "abc1234"}),
            ("GBNF 与 SFT 均通过。", {"GBNF", "SFT"}),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertTrue(expected <= self.terms(text))

    def test_skip_title_open_commands_and_only_three_sentences_with_original_lines(self):
        text = ("# SFT ⑩ 标题\n\n打开命令（PowerShell）：`Start-Process 'C:\\work\\a.html'`\n"
                "```powershell\nStart-Process 'C:\\work\\GBNF.html'\n```\n\n"
                "第一句 P。第二句\n用 bootstrap。第三句 95% 没解释。第四句 API。\n")
        got = ph.check_text(text)
        self.assertEqual({(h["term"], h["line"]) for h in got["hits"]},
                         {("P", 8), ("bootstrap", 9), ("95%", 9)})
        self.assertEqual(len(got["head"]), 3)

    def test_short_paragraphs_decimals_and_setext_heading(self):
        text = "SFT 标题\n====\n\n比例为 1.38%。\n\n只需阅读即可\n\n没有副作用\n\nAPI。"
        self.assertEqual(self.terms(text), set())
        self.assertEqual(len(ph.head_sentences(text)), 3)
        bold = ph.check_text("**你要选 A 或 B。我建议 A。**\n\n95% 区间很宽。第四句 SFT。")
        self.assertEqual({h["term"] for h in bold["hits"]}, {"95%"})
        self.assertEqual(len(bold["head"]), 3)

    def test_explicit_glosses_and_plain_confidence(self):
        text = ("SFT（用示例训练模型）和语法约束（GBNF）已试过，API 是指程序之间传话的接口。"
                "P/F/U（通过/失败/不确定），P=通过，F（失败），U（不确定）。"
                "95%置信区间，也就是结果大概落在这个范围内。")
        self.assertEqual(self.terms(text), set())
        self.assertIn("SFT", self.terms("SFT先保持数字格式。"))
        self.assertIn("95%", self.terms("95%（稳定胜率）见正文。"))
        self.assertIn("GBNF", self.terms("GBNF 与语法约束（SFT）没关系。"))
        self.assertEqual(self.terms("SFT（用示例训练模型）已试过。SFT 有效果。"), set())

    def test_numbered_steps_are_not_groups_and_dates_are_not_hashes(self):
        self.assertEqual(self.terms("① 打开报告。② 点击复制。③ 再看 20261102 的记录。"), set())
        self.assertIn("①", self.terms("①组比②组好。"))
        self.assertIn("③", self.terms("单独③对原样胜率更高。"))
        self.assertEqual(self.terms("PUFFER 与 URLpath 不是单独的 P/F/U（通过/失败/不确定）。"), {"PUFFER"})

    def test_html_only_line_numbers_and_markdown_preferred(self):
        html = ("<html><head><title>SFT</title><style>API</style></head><body>\n"
                "<h1>GBNF ⑩</h1><div class='src'>由 <code>source.md</code> 生成</div>\n<p>打开报告：Start-Process x</p>\n"
                "<p>第一句 P。第二句 bootstrap。第三句 SFT。</p><script>API</script></body></html>")
        got = ph.check_text(html, html=True)
        self.assertEqual({(h["term"], h["line"]) for h in got["hits"]},
                         {("P", 4), ("bootstrap", 4), ("SFT", 4)})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "page.html").write_text(html, encoding="utf-8")
            md = root / "page.md"
            md.write_text("\ufeff# 标题\n\n只要读结论。", encoding="utf-8")
            got = ph.scan(root / "page.html")
            self.assertEqual(got["source"], str(md))
            self.assertEqual(got["hits"], [])

    def test_done_card_selection_dates_duplicates_and_no_task_body_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            backlog = Path(tmp) / "backlog.md"
            backlog.write_text(
                "- [done 10-24；给 owner\x2010-24：看，audits/old.md] 组织：旧\n"
                "- [review；给 owner\x2011-01：看，audits/review.md] 组织：未过审\n"
                "- [todo] 组织：给 owner\x2011-01：看，audits/todo.md\n"
                "- [done 10-27；给 owner\x2010-25：看 [A/B]，audits/a.html、audits/b.md:14；"
                "给 owner\x2010-26：再看，audits/a.html] 组织：引用 audits/reviewer-only.md\n"
                "- [done 11-02；给 owner\x2011-02：看，audits\\nested\\c.md，audits/../outside.md] 组织：新\n",
                encoding="utf-8")
            got = ph.owner_files(backlog, "10-25")
            self.assertEqual([r["file"] for r in got], ["audits/a.html", "audits/b.md", "audits/nested/c.md"])
            self.assertEqual(got[0]["backlog_lines"], [4, 4])

    def test_cli_real_files_read_only_advisory_missing_and_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audits = root / "audits"
            audits.mkdir()
            report = audits / "report.md"
            report.write_text("# 标题\n\nP 与 bootstrap 没解释。", encoding="utf-8")
            before = report.read_bytes()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = ph.main([str(audits), "--json"])
            self.assertEqual(code, 0)
            rows = json.loads(out.getvalue())
            self.assertEqual(len(rows), 1)
            self.assertEqual({h["term"] for h in rows[0]["hits"]}, {"P", "bootstrap"})
            self.assertEqual(report.read_bytes(), before)
            self.assertIn("未扫描", ph.scan(audits / "missing.md")["status"])
            self.assertIn("未扫描", ph.scan(audits / "image.png")["status"])
            self.assertIn(str(report) + ":3", ph.markdown(rows))


if __name__ == "__main__":
    unittest.main()
