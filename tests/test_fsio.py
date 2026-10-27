import unittest
from pathlib import Path
from unittest import mock

from orgcore import fsio


class ReadText(unittest.TestCase):
    def test_a_transient_permission_error_is_retried(self):
        real = Path.read_bytes
        failures = [PermissionError(13, "swap in progress")] * 3

        def flaky(path):
            if failures:
                raise failures.pop()
            return real(path)

        target = Path(__file__)
        with mock.patch.object(Path, "read_bytes", flaky), mock.patch.object(fsio.time, "sleep"):
            self.assertIn("ReadText", fsio.read_text(target))
        self.assertEqual(failures, [])

    def test_a_permission_error_that_never_clears_is_raised(self):
        with mock.patch.object(Path, "read_bytes", side_effect=PermissionError), mock.patch.object(fsio.time, "sleep"):
            with self.assertRaises(PermissionError):
                fsio.read_text(Path("x"))

    def test_the_other_errors_are_not_retried(self):
        with mock.patch.object(fsio.time, "sleep") as sleep:
            with self.assertRaises(FileNotFoundError):
                fsio.read_text(Path(__file__).with_name("nope.txt"))
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()