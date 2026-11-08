from __future__ import annotations
import json
import re
import unittest
from pathlib import Path

from . import i18n

STATIC = Path(__file__).resolve().parent / "static"
PAGES = ("dashboard.html", "todo.html", "index.html", "app.js", "rules.js")
CJK = re.compile(r"[\u4e00-\u9fff]")


class I18nTests(unittest.TestCase):
    def table(self) -> dict:
        return json.loads(i18n.EN_FILE.read_text(encoding="utf-8"))

    def test_zh_is_identity(self):
        for name in PAGES:
            text = (STATIC / name).read_text(encoding="utf-8")
            self.assertIs(i18n.page(text, "zh"), text)

    def test_unknown_language_is_an_error(self):
        with self.assertRaises(ValueError):
            i18n.page("x", "fr")

    def test_every_key_is_a_piece_of_some_page(self):
        sources = "".join((STATIC / n).read_text(encoding="utf-8") for n in PAGES) + "统计中…好诗集"  # the two fragments server.py inserts itself
        missing = [k for k in self.table() if k not in sources]
        self.assertEqual(missing, [])

    def test_en_pages_keep_only_the_logic_literals_in_chinese(self):
        identity = {k for k, v in self.table().items() if k == v}
        for name in PAGES:
            out = i18n.page((STATIC / name).read_text(encoding="utf-8"), "en")
            rest = CJK.findall(re.sub("|".join(re.escape(k) for k in identity), "", out))
            self.assertEqual(rest, [], name)

    def test_values_cannot_break_the_scripts(self):
        for k, v in self.table().items():
            if k != v:
                self.assertFalse(re.search(r"[`'\"\\<>]|\$\{", v), (k, v))

    def test_spacing_next_to_placeholders(self):
        out = i18n.page("`${n}小时前`", "en") if "小时前" in self.table() else None
        if out:
            self.assertNotRegex(out, r"\}[A-Za-z]|[A-Za-z]\$\{")

    def test_data_labels_only_in_label_fields_and_whole_string(self):
        d = {"section": "组织线", "label": "终端原话", "text": "终端原话", "items": [{"text": "定"}, {"kind_label": "定"}],
             "warnings": ["旧会话存档无法读取"], "message": "已撤回 3 件，回到原处。", "note": "组织线", "n": 3}
        self.assertEqual(i18n.data(d, "en"), {"section": "Organization line", "label": "Terminal quotes", "text": "终端原话",
                         "items": [{"text": "定"}, {"kind_label": "Decide"}], "warnings": ["Old session archive could not be read"],
                         "message": "Withdrew 3 cards; they are back.", "note": "组织线", "n": 3})
        self.assertEqual(i18n.data(d, "zh"), d)

if __name__ == "__main__":
    unittest.main()
