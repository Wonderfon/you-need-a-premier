import unittest

from .page_smoke_check import EXPECTED_PAGES, assert_page_coverage


class TestPageSmokeCoverage(unittest.TestCase):
    def pages(self, paths):
        return [{"path": path, "handler": "page"} for path in paths]

    def test_expected_page_list_is_nonempty_and_accepted(self):
        self.assertTrue(EXPECTED_PAGES)
        assert_page_coverage(self.pages(EXPECTED_PAGES))

    def test_no_discovered_pages_fails(self):
        with self.assertRaisesRegex(AssertionError, "No page routes discovered"):
            assert_page_coverage([])

    def test_missing_and_unexpected_pages_are_reported(self):
        paths = [path for path in EXPECTED_PAGES if path != "/review/all"] + ["/new-page"]
        with self.assertRaisesRegex(AssertionError, r"missing=\['/review/all'\].*unexpected=\['/new-page'\]"):
            assert_page_coverage(self.pages(paths))


if __name__ == "__main__":
    unittest.main()
