"""Small stdlib check for the safety-critical local transport behavior."""
import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import direct
import gateway


class FakeChannel:
    def __init__(self, channel_id, guild_id):
        self.id = channel_id
        self.guild = type("Guild", (), {"id": guild_id})()
        self.sent = []

    async def send(self, content, *, allowed_mentions):
        self.sent.append((content, allowed_mentions))
        return type("Message", (), {"id": 987654321})()


class FakeClient:
    def __init__(self, channel):
        self.channel = channel

    async def fetch_channel(self, channel_id):
        return self.channel


class FakeReceiptDB:
    def __init__(self, fail_sent=False):
        self.rows = []
        self.fail_sent = fail_sent
        self.rolled_back = False

    def execute(self, sql, values):
        if self.fail_sent and "message_id,status" in sql:
            raise OSError("database unavailable")
        self.rows.append((sql, values))

    def commit(self):
        pass

    def rollback(self):
        self.rolled_back = True


class TransportCheck(unittest.TestCase):
    def test_allowlist_exact_persistence_dedup_and_pause(self):
        config = {"owner_user_id": "11", "guild_id": "22", "channels": {"work": "33"}}
        self.assertTrue(gateway.is_allowed(config, guild_id=22, channel_id=33, user_id=11))
        self.assertFalse(gateway.is_allowed(config, guild_id=99, channel_id=33, user_id=11))
        self.assertFalse(gateway.is_allowed(config, guild_id=22, channel_id=44, user_id=11))
        self.assertFalse(gateway.is_allowed(config, guild_id=22, channel_id=33, user_id=12))

        with tempfile.TemporaryDirectory() as temp:
            db = gateway.connect(config, Path(temp) / "test.sqlite3")
            original = "  <@999> keep spaces\n and exact text  "
            fields = dict(message_id="123", source="mention", received_at="2026-09-28T12:00:00+00:00",
                          guild_id="22", channel_id="33", author_id="11", role="work", exact_text=original)
            self.assertTrue(gateway.record_inbound(db, **fields))
            self.assertFalse(gateway.record_inbound(db, **fields))
            saved = db.execute("SELECT source,received_at,role,exact_text FROM inbound WHERE message_id='123'").fetchone()
            self.assertEqual(saved, ("mention", fields["received_at"], "work", original))
            self.assertTrue(gateway.get_paused(db))
            gateway.set_paused(db, False)
            self.assertFalse(gateway.get_paused(db))
            db.close()

    def test_transport_does_not_execute_generated_commands(self):
        source = Path(gateway.__file__).read_text(encoding="utf-8")
        self.assertFalse(hasattr(gateway, "dispatch_task"))
        for execution_primitive in ("create_subprocess_shell", "create_subprocess_exec", "Popen(", "eval("):
            self.assertNotIn(execution_primitive, source)

    def test_send_blocks_channel_from_other_guild(self):
        channel = FakeChannel(channel_id=33, guild_id=99)
        db = FakeReceiptDB()
        with self.assertRaisesRegex(RuntimeError, "Outbound blocked.*no message was sent"):
            asyncio.run(gateway.send_and_record(
                FakeClient(channel), db, role="work", channel_id="33", guild_id="22",
                content="report", allowed_mentions=None,
            ))
        self.assertEqual(channel.sent, [])
        self.assertEqual(db.rows[0][1][3], "blocked")

    def test_successful_send_with_failed_receipt_is_reported_as_delivered(self):
        channel = FakeChannel(channel_id=33, guild_id=22)
        db = FakeReceiptDB(fail_sent=True)
        with self.assertRaisesRegex(RuntimeError, r"delivered_receipt_unpersisted.*message_id=987654321"):
            asyncio.run(gateway.send_and_record(
                FakeClient(channel), db, role="work", channel_id="33", guild_id="22",
                content="report", allowed_mentions=None,
            ))
        self.assertEqual(len(channel.sent), 1)
        self.assertTrue(db.rolled_back)


class SplitLineConfig(unittest.TestCase):
    """10-27 work-line split: a sixth channel (role uvox), never two roles on one channel, and a read-only way to find its ID."""

    def write_config(self, channels):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "config.json"
        path.write_text(json.dumps({"owner_user_id": "11", "guild_id": "22", "channels": channels}), encoding="utf-8")
        old, gateway.CONFIG_PATH = gateway.CONFIG_PATH, path
        self.addCleanup(setattr, gateway, "CONFIG_PATH", old)

    def test_new_line_channel_loads_and_a_shared_id_is_refused(self):
        self.write_config({"work": "33", "uvox": "44", "coordinator": "55"})
        self.assertEqual(gateway.load_config()["channels"]["uvox"], "44")
        self.write_config({"work": "33", "uvox": "33"})
        with self.assertRaisesRegex(RuntimeError, "share one channel ID"):
            gateway.load_config()

    def test_channel_lines_name_the_role_already_using_an_id_and_print_nothing_else(self):
        kind = lambda name: type("T", (), {"name": name})()
        chans = [type("C", (), {"id": 44, "name": "beacon-listener", "type": kind("text")})(),
                 type("C", (), {"id": 33, "name": "arbitros", "type": kind("text")})()]
        self.assertEqual(gateway.channel_lines(chans, {"work": "33"}),
                         ["33\tarbitros\ttext\twork", "44\tbeacon-listener\ttext\t"])


class FileChannel(FakeChannel):
    async def send(self, content, *, allowed_mentions, files=None):
        self.sent.append((content, files))
        return type("Message", (), {"id": 5})()


class FakeAttachment:
    def __init__(self, filename, size=10):
        self.filename, self.size = filename, size

    async def save(self, path):
        Path(path).write_bytes(b"x")


class FileTransfer(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def test_send_with_files_attaches_and_records_names(self):
        f = self.dir / "报告.txt"
        f.write_text("hi", encoding="utf-8")
        channel, db = FileChannel(33, 22), FakeReceiptDB()
        asyncio.run(gateway.send_and_record(FakeClient(channel), db, role="work", channel_id="33", guild_id="22",
                                            content="r", allowed_mentions=None, files=[str(f)]))
        self.assertEqual([x.filename for x in channel.sent[0][1]], ["报告.txt"])
        self.assertIn("报告.txt", db.rows[0][1][-1])

    def test_check_upload_files_rules(self):
        ok = self.dir / "a.txt"
        ok.write_text("x")
        gateway.check_upload_files([str(ok)])
        big = self.dir / "big.bin"
        with big.open("wb") as h:
            h.truncate(gateway.MAX_UPLOAD_MB * 1024 * 1024 + 1)
        with self.assertRaisesRegex(RuntimeError, "email"):
            gateway.check_upload_files([str(big)])
        with self.assertRaisesRegex(RuntimeError, "not an existing file"):
            gateway.check_upload_files([str(self.dir / "none.txt")])
        with self.assertRaisesRegex(RuntimeError, "At most"):
            gateway.check_upload_files([str(ok)] * 11)
        for name in (".env", ".env.local", "a.dpapi", "x.pem", "x.key", "id_rsa.pub", "client_secret_1.json",
                     "my_Token.txt", "Password list.txt", "notes-secret.md"):
            p = self.dir / name
            p.write_text("x")
            with self.assertRaisesRegex(RuntimeError, "credential", msg=name):
                gateway.check_upload_files([str(p)])
        folder = self.dir / "02_secrets"
        folder.mkdir()
        (folder / "a.txt").write_text("x")
        with self.assertRaisesRegex(RuntimeError, "credential"):
            gateway.check_upload_files([str(folder / "a.txt")])

    def test_send_with_bad_file_fails_before_login(self):
        config = {"channels": {"work": "33"}, "guild_id": "22"}
        with self.assertRaisesRegex(RuntimeError, "not an existing file"):
            asyncio.run(gateway.send_report(config, "work", "x", [str(self.dir / "nope")]))

    def test_save_attachments_sanitizes_dedups_and_skips_large(self):
        atts = [FakeAttachment("../../报告 终稿.pdf"), FakeAttachment("报告 终稿.pdf"),
                FakeAttachment('a:b*c〔1〕.txt'), FakeAttachment("huge.bin", size=gateway.MAX_DOWNLOAD_MB * 1024 * 1024 + 1)]
        saved, skipped = asyncio.run(gateway.save_attachments(42, atts, self.dir))
        self.assertEqual([p.name for p in saved], ["报告 终稿.pdf", "报告 终稿 (2).pdf", "a_b_c_1_.txt"])
        self.assertTrue(all(p.parent == self.dir / "42" and p.exists() for p in saved))
        self.assertEqual(len(skipped), 1)
        self.assertIn("huge.bin", skipped[0])

    def test_save_failure_is_reported_not_raised(self):
        class Broken(FakeAttachment):
            async def save(self, path):
                raise OSError("disk")
        saved, skipped = asyncio.run(gateway.save_attachments(43, [Broken("bad.txt"), FakeAttachment("ok.txt")], self.dir))
        self.assertEqual([p.name for p in saved], ["ok.txt"])
        self.assertEqual(skipped, ["bad.txt（保存失败：OSError）"])


class HoldNotices(unittest.TestCase):
    """10-26: a proactive send waits while the owner's request in that channel is queued/running; replies never wait."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "d.sqlite3"
        self.config = {"channels": {"work": "33", "life": "44"}, "guild_id": "22"}
        self.db = gateway.connect(self.config, self.path)
        direct.initialize(self.db)
        self.addCleanup(self.db.close)

    def request(self, mid, channel, status):
        self.db.execute("INSERT INTO inbound VALUES(?,?,?,?,?,?,?,?)", (mid, "direct:message", "t", "22", channel, "11", "work", "x"))
        self.db.execute("INSERT INTO direct_requests(message_id,status,queued_only,updated_at) VALUES(?,?,0,'t')", (mid, status))
        self.db.commit()

    def answer_later(self, mid, delay, log):
        async def go():
            await asyncio.sleep(delay)
            other = sqlite3.connect(self.path)  # the tick's own connection
            other.execute("UPDATE direct_requests SET status='answered' WHERE message_id=?", (mid,))
            other.commit(); other.close()
            log.append("answered")
        return go()

    def send(self, reply, log, wait_s=0.1, hold=True, line="量化线"):
        sent = []

        class Client:
            def __init__(self, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def login(self, token): pass

        async def record(client, db, **kw):
            log.append("sent"); sent.append(kw["content"]); return "1"

        real = gateway.hold_for_owner_reply
        quick = lambda db, ch: real(db, ch, max_s=wait_s, poll_s=0.02)
        with patch.object(gateway, "connect", lambda config: sqlite3.connect(self.path)), patch.object(gateway, "token_from_dpapi", lambda: "x"), \
                patch.object(gateway, "send_and_record", record), patch.object(gateway, "hold_for_owner_reply", quick), \
                patch("discord.Client", Client):
            async def both():
                await asyncio.gather(gateway.send_report(self.config, "work", "报告", reply=reply, hold=hold, line=line), *extra)
            extra = self.extra
            asyncio.run(both())
        return sent

    extra = ()

    def test_waiting_only_for_queued_or_running_in_the_same_channel(self):
        self.request("1", "33", "queued")
        self.request("2", "44", "answered")
        self.request("3", "44", "queued_only")
        self.assertTrue(gateway.owner_waiting(self.db, "33"))
        self.assertFalse(gateway.owner_waiting(self.db, "44"))
        self.db.execute("UPDATE direct_requests SET status='running' WHERE message_id='1'")
        self.assertTrue(gateway.owner_waiting(self.db, "33"))

    def test_notice_waits_for_the_reply_then_goes_with_a_source_line(self):
        self.request("1", "33", "queued")
        log = []
        self.extra = [self.answer_later("1", 0.1, log)]
        sent = self.send(False, log, wait_s=2)
        self.assertEqual(log, ["answered", "sent"])
        self.assertEqual(sent, ["〔量化线主动通知，不是回复你刚才的消息〕\n报告"])

    def test_no_hold_goes_at_once_with_the_source_line(self):  # the supervisor loop must not stall
        self.request("1", "33", "queued")
        log = []
        self.extra = []
        t0 = time.monotonic()
        self.assertEqual(self.send(False, log, wait_s=5, hold=False), ["〔量化线主动通知，不是回复你刚才的消息〕\n报告"])
        self.assertLess(time.monotonic() - t0, 1)

    def test_notice_goes_after_the_timeout_even_if_unanswered(self):
        self.request("1", "33", "queued")
        log = []
        self.extra = []
        self.assertEqual(self.send(False, log, wait_s=0.1), ["〔量化线主动通知，不是回复你刚才的消息〕\n报告"])
        self.assertEqual(log, ["sent"])

    def test_reply_is_neither_held_nor_tagged(self):
        self.request("1", "33", "queued")
        log = []
        self.extra = []
        t0 = time.monotonic()
        self.assertEqual(self.send(True, log, wait_s=5), ["报告"])
        self.assertLess(time.monotonic() - t0, 1)

    def test_notice_without_pending_request_goes_at_once_untouched(self):
        self.request("2", "44", "answered")
        self.request("3", "33", "queued_only")
        log = []
        self.extra = []
        t0 = time.monotonic()
        self.assertEqual(self.send(False, log, wait_s=5), ["报告"])
        self.assertLess(time.monotonic() - t0, 1)

    def test_missing_direct_requests_table_does_not_block(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE inbound(message_id TEXT, channel_id TEXT)")
        self.assertFalse(gateway.owner_waiting(db, "33"))


if __name__ == "__main__":
    unittest.main()
