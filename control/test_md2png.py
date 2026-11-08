import importlib.util
import math
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import md2png


class ChromiumLibraries(unittest.TestCase):
    def test_windows_and_system_linux_library_keep_the_default_environment(self):
        with mock.patch.object(md2png.sys, "platform", "win32"), \
                mock.patch.object(md2png.ctypes, "CDLL") as load:
            self.assertIsNone(md2png.chromium_env())
            load.assert_not_called()
        with mock.patch.object(md2png.sys, "platform", "linux"), \
                mock.patch.object(md2png.ctypes, "CDLL") as load:
            self.assertIsNone(md2png.chromium_env())
            load.assert_called_once_with("libnspr4.so")

    def test_first_cached_library_is_added_without_changing_the_process_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            directories = [Path(folder) / name for name in ("missing", "cached", "later")]
            for directory in directories[1:]:
                directory.mkdir()
                (directory / "libnspr4.so").touch()
            with mock.patch.object(md2png.sys, "platform", "linux"), \
                    mock.patch.object(md2png.ctypes, "CDLL", side_effect=OSError("missing")), \
                    mock.patch.dict(md2png.CONFIG, linux_library_dirs=directories), \
                    mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "existing", "MD2PNG_TEST": "keep"}):
                env = md2png.chromium_env()
                self.assertEqual(env["LD_LIBRARY_PATH"], str(directories[1]) + os.pathsep + "existing")
                self.assertEqual(env["MD2PNG_TEST"], "keep")
                self.assertEqual(os.environ["LD_LIBRARY_PATH"], "existing")
                with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": ""}):
                    self.assertEqual(md2png.chromium_env()["LD_LIBRARY_PATH"], str(directories[1]))

    def test_missing_library_error_lists_every_checked_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            directories = [Path(folder) / name for name in ("first", "second")]
            with mock.patch.object(md2png.sys, "platform", "linux"), \
                    mock.patch.object(md2png.ctypes, "CDLL", side_effect=OSError("missing")), \
                    mock.patch.dict(md2png.CONFIG, linux_library_dirs=directories):
                with self.assertRaises(OSError) as error:
                    md2png.chromium_env()
            self.assertEqual(str(error.exception), "libnspr4.so unavailable; checked cache directories: "
                             + ", ".join(map(str, directories)))


class PageSlices(unittest.TestCase):
    def test_slice_count_and_complete_contiguous_coverage(self):
        for height in (1, 1289, 1290, 1291, 2580, 3871):
            with self.subTest(height=height):
                parts = md2png.slices(height, 1290)
                self.assertEqual(len(parts), math.ceil(height / 1290))
                self.assertEqual(sum(h for _, h in parts), height)
                self.assertEqual(parts[0][0], 0)
                self.assertTrue(all(0 < h <= 1290 for _, h in parts))
                self.assertTrue(all(y + h == ny for (y, h), (ny, _) in zip(parts, parts[1:])))


class OutputCleanup(unittest.TestCase):
    def test_rerun_keeps_backup_and_removes_surplus_pages(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "report.html"
            source.write_text("<p>Report</p>", encoding="utf-8")
            output = source.with_suffix("")
            output.mkdir()
            backup = output / "page-001-backup.png"
            backup.write_bytes(b"keep")
            surplus = output / "page-002.png"
            surplus.write_bytes(b"remove")
            long_number = output / "page-1000.png"
            long_number.write_bytes(b"remove")

            page = mock.Mock()
            page.evaluate.side_effect = [None, None, {"width": 430, "scroll_width": 430, "height": 10, "overflow": []}]
            page.screenshot.return_value = b"png"
            context = mock.Mock()
            context.new_page.return_value = page
            browser = mock.Mock()
            browser.new_context.return_value = context

            paths = md2png.render(source, browser)

            self.assertEqual([p.name for p in paths], ["page-001.png"])
            self.assertEqual(backup.read_bytes(), b"keep")
            self.assertFalse(surplus.exists())
            self.assertFalse(long_number.exists())


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Optional Playwright Chromium is not installed")
class BrowserRendering(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.playwright = sync_playwright().start()
        if not Path(cls.playwright.chromium.executable_path).is_file():
            cls.playwright.stop()
            raise unittest.SkipTest("Optional Playwright Chromium is not installed")
        cls.browser = cls.playwright.chromium.launch(env=md2png.chromium_env())

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def test_markdown_mobile_reflow_and_html_cli(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            source = Path(folder) / "手机 报告.md"
            source.write_text(
                "# 手机截图\n\n" + "long_unbroken_text_" * 30 + "\n\n```\n"
                + "C:\\owner\\long path\\" * 30 + "\n```\n\n"
                + "|" + "|".join(f"Column {i}" for i in range(10)) + "|\n"
                + "|" + "---|" * 10 + "\n"
                + "|" + "|".join("abcdefghij0123456789" for _ in range(10)) + "|\n"
                + "\n\nMore text 中文。\n" * 90,
                encoding="utf-8",
            )
            paths = md2png.render(source, self.browser)
            page = self.browser.new_page(viewport={"width": 430, "height": 1290}, device_scale_factor=2)
            try:
                # Check md2html itself, before md2png injects screenshot CSS.
                page.goto(source.with_suffix(".html").as_uri())
                metrics = md2png.layout_metrics(page)
                self.assertEqual(metrics["scroll_width"], 430)
                self.assertEqual(metrics["overflow"], [])
                page.add_style_tag(content=md2png.MOBILE_CSS)
                page.evaluate("() => document.fonts.ready")
                metrics = md2png.layout_metrics(page)
                self.assertEqual(len(paths), math.ceil(metrics["height"] / 1290))
            finally:
                page.close()
            expected = md2png.slices(metrics["height"], 1290)
            for path, (_, height) in zip(paths, expected):
                data = path.read_bytes()
                self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
                self.assertEqual(struct.unpack(">II", data[16:24]), (860, height * 2))
                self.assertLessEqual(len(data), 10_000_000)
            # Rerun a shorter .html via the real CLI; surplus PNGs must disappear.
            source.with_suffix(".html").write_text(
                '<!doctype html><meta charset="utf-8"><p>Short 中文</p>', encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(Path(md2png.__file__)), str(source.with_suffix(".html"))],
                capture_output=True, text=True, encoding="utf-8", check=True,
            )
            self.assertEqual(len(result.stdout.splitlines()), 1)
            self.assertEqual(len(list(source.with_suffix("").glob("*.png"))), 1)
            with self.assertRaisesRegex(ValueError, "hand-made page"):
                md2png.render(source, self.browser)

    def test_overflow_is_reported_instead_of_silently_cropped(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            source = Path(folder) / "overflow.html"
            source.write_text('<div style="width:900px">Must not be cropped</div>', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "still overflows"):
                md2png.render(source, self.browser)
            self.assertFalse(source.with_suffix("").exists())

    def test_existing_html_is_reflowed_without_changing_the_source(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            source = Path(folder) / "report.html"
            original = ('<!doctype html><meta charset="utf-8"><pre>' + 'C:\\long_path\\' * 100
                        + '</pre><p>' + 'unbroken_link_' * 100 + '</p>')
            source.write_text(original, encoding="utf-8")
            paths = md2png.render(source, self.browser)
            self.assertGreater(len(paths), 0)
            self.assertEqual(source.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
