import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import claude_tag_check as c


SAMPLE = "\n".join((
    "# 合成待办",
    "## 组织线",
    "- [todo] 组织：只在 windows 的 .venv 里测〔执行者：Claude〕",
    "- [todo] 输入台：只改文案〔执行者：Claude〕",
    "- [todo] 组织：正文引用「不要标〔执行者：Claude〕」",
    "- [review] 组织：Windows 审查〔执行者：Claude〕",
    "### 子标题不切换工作线",
    "- [todo] （返工）组织：引用「〔执行者：Claude〕」后仍有真标签〔执行者：Claude〕，WinError 1920",
    "## 量化线",
    "- [todo] 量化：SEC efts 需要环境变量，用 claude -p〔执行者：Claude〕",
    "- [done] 量化：旧行〔执行者：Claude〕",
))


class ClaudeTagCheck(unittest.TestCase):
    def test_reasons_missing_reasons_and_quoted_tag(self):
        rows = list(c.check(SAMPLE))
        self.assertEqual([r["line"] for r in rows], [3, 4, 5, 8, 10])
        self.assertEqual([r["status"] for r in rows],
                         ["有理由词", "可能不需要", "引用，不是标签", "有理由词", "有理由词"])
        self.assertEqual(rows[0]["reasons"], ["Windows", "venv"])
        self.assertEqual(rows[1]["reasons"], [])
        self.assertEqual(rows[3]["project"], "组织")
        self.assertEqual(rows[4]["reasons"], ["环境变量", "claude -p", "SEC", "efts"])

    def test_sections_config_and_crlf(self):
        with patch.dict(c.CONFIG, sections=("量化线",)):
            rows = list(c.check(SAMPLE.replace("\n", "\r\n")))
        self.assertEqual([r["line"] for r in rows], [10])
        self.assertEqual(len(list(c.check(SAMPLE, sections=("组织线",)))), 4)
        self.assertEqual(len(list(c.check(SAMPLE, sections=()))), 5)

    def test_preview_limit_does_not_limit_reasons(self):
        text = "- [todo] Thinking Engines: " + "字" * 100 + " PowerShell Start-ScheduledTask〔执行者：Claude〕"
        row, = c.check(text)
        self.assertEqual(row["project"], "Thinking Engines")
        self.assertEqual(len(row["preview"]), 80)
        self.assertEqual(row["reasons"], ["Start-ScheduledTask", "PowerShell"])

    def test_quote_overrides_reasons_and_unclosed_quote_is_not_a_quote(self):
        text = ("- [todo] 组织：Windows「〔执行者：Claude〕」和「〔执行者：Claude〕」\n"
                "- [todo] 组织：「内层「说明」后的〔执行者：Claude〕」\n"
                "- [todo] 组织：未闭合「〔执行者：Claude〕")
        self.assertEqual([r["status"] for r in c.check(text)],
                         ["引用，不是标签", "引用，不是标签", "可能不需要"])

    def test_keyword_boundaries_and_config(self):
        row, = c.check("- [todo] 组织：local-secretary SECURITY events〔执行者：Claude〕")
        self.assertEqual(row["reasons"], [])
        with patch.dict(c.CONFIG, reason_words=("自定义理由",)):
            row, = c.check("- [todo] 组织：自定义理由〔执行者：Claude〕")
        self.assertEqual(row["reasons"], ["自定义理由"])

    def test_cli_reads_utf8_file_without_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "合成 backlog.md"
            path.write_text(SAMPLE, encoding="utf-8")
            before = path.read_bytes()
            result = subprocess.run(
                [sys.executable, "-B", "-X", "utf8", str(Path(c.__file__)), str(path), "--section", "组织线"],
                capture_output=True, text=True, encoding="utf-8", timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("共 4 条", result.stdout)
            self.assertIn("引用，不是标签", result.stdout)
            self.assertNotIn("量化线", result.stdout)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(Path(folder).iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
