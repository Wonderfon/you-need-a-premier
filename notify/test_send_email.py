import tempfile
import unittest
from pathlib import Path
from unittest import mock

import send_email


class BuildMessage(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def test_plain_message_has_no_attachment(self):
        msg = send_email.build_message("s", "b")
        self.assertEqual(list(msg.iter_attachments()), [])
        self.assertEqual(msg["Subject"], "[Org Core] s")

    def test_chinese_filename_and_type_survive(self):
        f = self.dir / "报告 终稿.pdf"
        f.write_bytes(b"%PDF-1")
        g = self.dir / "data.unknownext"
        g.write_bytes(b"\x00\x01")
        msg = send_email.build_message("s", "b", [f, g])
        a, b = msg.iter_attachments()
        self.assertEqual(a.get_filename(), "报告 终稿.pdf")
        self.assertEqual(a.get_content_type(), "application/pdf")
        self.assertEqual(a.get_content(), b"%PDF-1")
        self.assertEqual(b.get_content_type(), "application/octet-stream")

    def test_refusals(self):
        with self.assertRaisesRegex(ValueError, "existing file"):
            send_email.build_message("s", "b", [self.dir / "none.txt"])
        for name in (".env", ".env.x", "a.dpapi", "k.pem", "k.key", "id_rsa", "client_secret.json", "Token.txt",
                     "my password.txt", "x-secret.txt"):
            p = self.dir / name
            p.write_text("x")
            with self.assertRaisesRegex(ValueError, "credential", msg=name):
                send_email.build_message("s", "b", [p])
        folder = self.dir / "02_secrets"
        folder.mkdir()
        (folder / "a.txt").write_text("x")
        with self.assertRaisesRegex(ValueError, "credential"):
            send_email.build_message("s", "b", [folder / "a.txt"])

    def test_total_size_limit(self):
        a, b = self.dir / "a.bin", self.dir / "b.bin"
        for p in (a, b):
            with p.open("wb") as h:
                h.truncate(10 * 1024 * 1024)
        with self.assertRaisesRegex(ValueError, "18 MB"):
            send_email.build_message("s", "b", [a, b])

    def test_send_validates_before_connecting(self):
        with mock.patch.object(send_email.smtplib, "SMTP_SSL") as smtp:
            with self.assertRaises(ValueError):
                send_email.send("s", "b", [self.dir / "none.txt"])
        smtp.assert_not_called()

    def test_receipt_lists_attachments_without_content(self):
        f = self.dir / "a.txt"
        f.write_text("hello")
        with mock.patch.object(send_email.smtplib, "SMTP_SSL"), mock.patch.object(send_email, "app_password", return_value="x"), \
                mock.patch.object(send_email, "ROOT", self.dir):
            send_email.send("s", "b", [f])
        receipt = (self.dir / "data" / "sent.jsonl").read_text(encoding="utf-8")
        self.assertIn('"attachments": [{"name": "a.txt", "bytes": 5}]', receipt)
        self.assertNotIn("hello", receipt)


if __name__ == "__main__":
    unittest.main()
