from __future__ import annotations

import hashlib
import json
import re
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer
from multidict import CIMultiDict

from .server import (CSRF_COOKIE, IDENTITY_HEADER, OWNER_LOGIN_KEY, PRIVATE_CONFIG, STORE_KEY,
                     build_backup_status, create_app, load_private_config)
from .store import InputError, InputStore

PRIVATE_HOST = "voice-pc.example-tailnet.ts.net"
OWNER = "owner@example.com"


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = InputStore(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def test_audio_chunk_retry_is_idempotent_and_conflicts_do_not_replace_bytes(self):
        session = self.store.create_audio_session("audio_upload", "原音频", "audio/webm", "voice.webm")
        sid = session["id"]
        first, second = b"webm-header-and-first-frame", b"second-frame"
        h1 = hashlib.sha256(first).hexdigest()
        h2 = hashlib.sha256(second).hexdigest()

        self.assertEqual(self.store.put_chunk(sid, 0, first, h1), {"duplicate": False, "stored": True})
        restarted_store = InputStore(Path(self.temp.name))
        self.assertEqual(restarted_store.put_chunk(sid, 0, first, h1), {"duplicate": True, "stored": True})
        with self.assertRaises(InputError) as conflict:
            self.store.put_chunk(sid, 0, b"different bytes", hashlib.sha256(b"different bytes").hexdigest())
        self.assertEqual(conflict.exception.status, 409)
        restarted_store.put_chunk(sid, 1, second, h2)

        completed = restarted_store.finalize_audio(sid, 2, len(first) + len(second))
        original = restarted_store.get_session_path(sid).read_bytes()
        self.assertEqual(original, first + second)
        self.assertEqual(completed["original_sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(completed["status"], "queued")
        self.assertEqual(restarted_store.finalize_audio(sid, 2, len(original))["original_sha256"], completed["original_sha256"])

    def test_chunk_limit_is_enforced_before_more_original_bytes_are_written(self):
        session = self.store.create_audio_session("audio_capture", "录音", "audio/webm")
        sid = session["id"]
        with patch.object(sys.modules[InputStore.__module__], "MAX_SESSION_BYTES", 5):
            data = b"1234"
            self.store.put_chunk(sid, 0, data, hashlib.sha256(data).hexdigest())
            too_much = b"56"
            with self.assertRaises(InputError) as limited:
                self.store.put_chunk(sid, 1, too_much, hashlib.sha256(too_much).hexdigest())
        self.assertEqual(limited.exception.status, 413)
        self.assertEqual(self.store._chunk_path(sid, 0).read_bytes(), data)
        self.assertFalse(self.store._chunk_path(sid, 1).exists())

    def test_original_text_corrections_and_instruction_are_separate_parented_versions(self):
        session, original = self.store.create_text_session("想法", "原始输入：准备下周联系实验室。")
        self.assertEqual(len(self.store.session_detail(session["id"])["revisions"]), 1)
        first = self.store.append_revision(session["id"], "correction", "识别纠错：准备下周联系实验室。", original["id"])
        branch = self.store.append_revision(session["id"], "correction", "表达改写：我下周会联系实验室。", original["id"])
        instruction = self.store.append_revision(
            session["id"], "instruction", "请帮我起草一封联系实验室的邮件。", branch["id"], "explicit_user_instruction"
        )

        revisions = self.store.session_detail(session["id"])["revisions"]
        by_id = {revision["id"]: revision for revision in revisions}
        self.assertEqual(len(revisions), 4)
        self.assertEqual(by_id[original["id"]]["content"], "原始输入：准备下周联系实验室。")
        self.assertEqual(by_id[first["id"]]["parent_id"], original["id"])
        self.assertEqual(by_id[branch["id"]]["parent_id"], original["id"])
        self.assertEqual(by_id[instruction["id"]]["parent_id"], branch["id"])
        self.assertEqual(by_id[branch["id"]]["sha256"], hashlib.sha256(branch["content"].encode()).hexdigest())
        exported = self.store.list_instructions()
        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0]["instruction_revision_id"], instruction["id"])
        self.assertEqual(exported[0]["source_document_id"], session["id"])
        self.assertEqual(exported[0]["source_revision_id"], branch["id"])
        self.assertEqual(exported[0]["dispatch_status"], "unknown_to_input_service")

    def test_audio_chunks_must_be_contiguous_and_hash_valid(self):
        session = self.store.create_audio_session("audio_capture", "录音", "audio/webm")
        sid = session["id"]
        with self.assertRaises(InputError):
            self.store.put_chunk(sid, 0, b"bytes", "0" * 64)
        good = b"chunk two"
        self.store.put_chunk(sid, 1, good, hashlib.sha256(good).hexdigest())
        with self.assertRaises(InputError) as missing:
            self.store.finalize_audio(sid, 2, len(good))
        self.assertEqual(missing.exception.status, 409)
        self.assertEqual(self.store._chunk_path(sid, 1).read_bytes(), good)

    def _transcribed_audio(self):
        store = self.store
        session = store.create_audio_session("audio_capture", "录音", "audio/webm")
        data = b"complete original recording"
        store.put_chunk(session["id"], 0, data, hashlib.sha256(data).hexdigest())
        session = store.finalize_audio(session["id"], 1, len(data))
        asr = store.append_revision(session["id"], "asr", "明天下午三点开会", source="asr", data={"text": "明天下午三点开会"})
        return session, asr, data

    def test_verification_binds_exact_revision_and_original_audio_hash(self):
        session, asr, data = self._transcribed_audio()
        sid, digest = session["id"], session["original_sha256"]
        with self.assertRaises(InputError) as wrong_hash:
            self.store.verify_revision(sid, asr["id"], "0" * 64)
        self.assertEqual(wrong_hash.exception.status, 409)
        with self.assertRaises(InputError) as malformed:
            self.store.verify_revision(sid, asr["id"], "not-a-hash")
        self.assertEqual(malformed.exception.status, 400)
        other, other_asr, _ = self._transcribed_audio()
        with self.assertRaises(InputError) as foreign:
            self.store.verify_revision(sid, other_asr["id"], digest)
        self.assertEqual(foreign.exception.status, 404)
        self.assertEqual(self.store.list_verified_training_rows(), [])

        verification = self.store.verify_revision(sid, asr["id"], digest.upper())
        self.assertEqual(verification["revision_id"], asr["id"])
        self.assertEqual(verification["revision_sha256"], asr["sha256"])
        self.assertEqual(verification["original_sha256"], digest)
        self.assertEqual(verification["scope"], "full_recording")
        self.assertEqual(self.store.verify_revision(sid, asr["id"], digest)["id"], verification["id"])
        rows = self.store.list_verified_training_rows()
        self.assertEqual([(row["revision_id"], row["text"]) for row in rows], [(asr["id"], "明天下午三点开会")])
        self.assertEqual(rows[0]["original_sha256"], digest)
        self.assertNotIn(other["id"], [row["session_id"] for row in rows])

        self.assertEqual(self.store.get_session(sid)["original_sha256"], digest)
        original = self.store.get_session_path(sid).read_bytes()
        self.assertEqual(original, data)
        self.assertEqual(hashlib.sha256(original).hexdigest(), digest)

    def test_rewrite_instruction_and_text_revisions_are_not_eligible_for_verification(self):
        session, asr, _ = self._transcribed_audio()
        sid, digest = session["id"], session["original_sha256"]
        rewrite = self.store.append_revision(sid, "correction", "我们明天下午三点开会。", asr["id"],
                                             data={"correction_type": "user_rewrite"})
        draft = self.store.append_revision(sid, "correction", "我们明天下午三点开会", rewrite["id"],
                                           data={"correction_type": "draft"})
        untyped = self.store.append_revision(sid, "correction", "明天下午三点开会", draft["id"])
        instruction = self.store.append_revision(sid, "instruction", "安排会议", untyped["id"], "explicit_user_instruction")
        for revision in (rewrite, draft, untyped, instruction):
            with self.assertRaises(InputError) as rejected:
                self.store.verify_revision(sid, revision["id"], digest)
            self.assertEqual(rejected.exception.status, 400)

        text_session, original_text = self.store.create_text_session("文字", "不是录音")
        with self.assertRaises(InputError) as no_audio:
            self.store.verify_revision(text_session["id"], original_text["id"], digest)
        self.assertEqual(no_audio.exception.status, 409)
        self.assertEqual(self.store.list_verified_training_rows(), [])
        self.assertEqual(self.store.get_session(sid)["original_sha256"], digest)

    def test_purpose_chosen_at_confirm_decides_not_earlier_drafts_or_labels(self):
        session, asr, _ = self._transcribed_audio()
        sid, digest = session["id"], session["original_sha256"]
        draft = self.store.append_revision(sid, "correction", "明天下午四点开会", asr["id"],
                                           data={"correction_type": "draft"})
        # Confirmed as a rewrite by mistake, then re-confirmed as verbatim: the last purpose wins.
        rewrite = self.store.append_revision(sid, "correction", "明天下午四点开会", draft["id"],
                                             data={"correction_type": "user_rewrite"})
        literal = self.store.append_revision(sid, "correction", "明天下午四点开会", rewrite["id"],
                                             data={"correction_type": "recognition_correction"})
        self.store.verify_revision(sid, literal["id"], digest)
        self.assertEqual([(row["revision_id"], row["text"]) for row in self.store.list_verified_training_rows()],
                         [(literal["id"], "明天下午四点开会")])
        # And the other way: re-confirming as a rewrite retires the verbatim check.
        self.store.append_revision(sid, "correction", "明天下午四点开会", literal["id"],
                                   data={"correction_type": "user_rewrite"})
        self.assertEqual(self.store.list_verified_training_rows(), [])

    def test_verbatim_check_survives_only_a_different_text_rewrite_after_drafts(self):
        session, asr, _ = self._transcribed_audio()
        sid, digest = session["id"], session["original_sha256"]
        fix = self.store.append_revision(sid, "correction", "明天下午四点开会", asr["id"],
                                         data={"correction_type": "recognition_correction"})
        self.store.verify_revision(sid, fix["id"], digest)

        def check_is_current(revision_id):
            by_id = {rev["id"]: rev for rev in self.store.session_detail(sid)["revisions"]}
            listed = revision_id in [row["revision_id"] for row in self.store.list_verified_training_rows()]
            self.assertEqual(listed, by_id[revision_id]["verification"]["current"])
            return listed

        # draft, then a different-text rewrite: the verbatim check stays (owner 10/1 00:13)
        draft = self.store.append_revision(sid, "correction", "明天下午四点开会吧", fix["id"], data={"correction_type": "draft"})
        self.assertFalse(check_is_current(fix["id"]))  # drafts alone: unconfirmed edits pending
        rewrite =self.store.append_revision(sid, "correction", "下午四点开会", draft["id"], data={"correction_type": "user_rewrite"})
        self.assertTrue(check_is_current(fix["id"]))
        with self.assertRaises(InputError) as stale:  # still only the latest revision can be newly verified
            self.store.verify_revision(sid, fix["id"], digest)
        self.assertEqual(stale.exception.status, 409)

        # a trailing unconfirmed draft retires it
        self.store.append_revision(sid, "correction", "下午四点开会。", rewrite["id"], data={"correction_type": "draft"})
        self.assertFalse(check_is_current(fix["id"]))

    def test_check_before_a_later_correction_or_asr_is_retired_even_if_a_rewrite_follows(self):
        for later_kind in ("correction", "asr"):
            with self.subTest(later=later_kind):
                session, asr, _ = self._transcribed_audio()
                sid, digest = session["id"], session["original_sha256"]
                first = self.store.append_revision(sid, "correction", "明天下午四点开会", asr["id"],
                                                   data={"correction_type": "recognition_correction"})
                self.store.verify_revision(sid, first["id"], digest)
                if later_kind == "correction":
                    later = self.store.append_revision(sid, "correction", "明天下午五点开会", first["id"],
                                                       data={"correction_type": "recognition_correction"})
                else:
                    later = self.store.append_revision(sid, "asr", "明天下午五点开会", source="asr", data={"text": "x"})
                self.store.append_revision(sid, "correction", "下午五点开会", later["id"], data={"correction_type": "user_rewrite"})
                self.assertEqual(self.store.list_verified_training_rows(), [])
                by_id = {rev["id"]: rev for rev in self.store.session_detail(sid)["revisions"]}
                self.assertFalse(by_id[first["id"]]["verification"]["current"])

    def test_later_confirmed_correction_stays_valid_beside_rewrite(self):
        session, asr, _ = self._transcribed_audio()
        sid, digest = session["id"], session["original_sha256"]
        first = self.store.append_revision(sid, "correction", "明天下午四点开会", asr["id"],
                                           data={"correction_type": "recognition_correction"})
        self.store.verify_revision(sid, first["id"], digest)
        second = self.store.append_revision(sid, "correction", "明天下午五点开会", first["id"],
                                            data={"correction_type": "recognition_correction"})
        self.store.verify_revision(sid, second["id"], digest)
        self.store.append_revision(sid, "correction", "下午五点开会", second["id"], data={"correction_type": "user_rewrite"})
        self.assertEqual([row["revision_id"] for row in self.store.list_verified_training_rows()], [second["id"]])
        by_id = {rev["id"]: rev for rev in self.store.session_detail(sid)["revisions"]}
        self.assertTrue(by_id[second["id"]]["verification"]["current"])
        self.assertFalse(by_id[first["id"]]["verification"]["current"])

    def test_new_edit_or_asr_does_not_inherit_and_invalidates_verification(self):
        session, asr, data = self._transcribed_audio()
        sid, digest = session["id"], session["original_sha256"]
        self.store.verify_revision(sid, asr["id"], digest)

        fix = self.store.append_revision(sid, "correction", "明天下午四点开会", asr["id"],
                                         data={"correction_type": "recognition_correction"})
        self.assertEqual(self.store.list_verified_training_rows(), [])
        by_id = {rev["id"]: rev for rev in self.store.session_detail(sid)["revisions"]}
        self.assertFalse(by_id[asr["id"]]["verification"]["current"])
        self.assertIsNone(by_id[fix["id"]]["verification"])
        self.assertEqual(by_id[asr["id"]]["content"], "明天下午三点开会")
        with self.assertRaises(InputError) as stale:
            self.store.verify_revision(sid, asr["id"], digest)
        self.assertEqual(stale.exception.status, 409)

        self.store.verify_revision(sid, fix["id"], digest)
        self.assertEqual([row["revision_id"] for row in self.store.list_verified_training_rows()], [fix["id"]])
        # An instruction is not a text edit and must not invalidate the check.
        self.store.append_revision(sid, "instruction", "安排会议", fix["id"], "explicit_user_instruction")
        self.assertEqual([row["revision_id"] for row in self.store.list_verified_training_rows()], [fix["id"]])

        rerun = self.store.append_revision(sid, "asr", "明天下午四点开会", source="asr", data={"text": "明天下午四点开会"})
        self.assertEqual(self.store.list_verified_training_rows(), [])
        detail = self.store.session_detail(sid)
        by_id = {rev["id"]: rev for rev in detail["revisions"]}
        self.assertIsNone(by_id[rerun["id"]]["verification"])
        self.assertFalse(by_id[fix["id"]]["verification"]["current"])
        self.assertEqual(len(detail["revisions"]), 4)
        self.assertEqual(detail["original_sha256"], digest)
        self.assertEqual(self.store.get_session_path(sid).read_bytes(), data)


class BackupStatusTests(unittest.TestCase):
    def test_zip_backup_requires_zip_receipt_and_matching_package_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = {"stage": "staged_for_drive_sync", "checked_at": "2026-09-28T10:12:00Z",
                      "snapshot_id": "test", "archive_format": "zip_incremental_v1", "package_sha256": "abc"}
            receipt = {"verification": "remote_manifest_content_verified", "snapshot_id": "test",
                       "verified_at_utc": "2026-09-28T10:13:00Z"}
            (root / "status.json").write_text(json.dumps(status), encoding="utf-8")
            (root / "cloud-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
            self.assertFalse(build_backup_status(root)["remote_matches_latest_local"])
            receipt.update(verification="remote_zip_content_verified", package_sha256="wrong")
            (root / "cloud-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
            self.assertFalse(build_backup_status(root)["remote_matches_latest_local"])
            receipt["package_sha256"] = "abc"
            (root / "cloud-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
            self.assertTrue(build_backup_status(root)["remote_matches_latest_local"])

    def test_chat_capture_failure_surfaces_even_when_existing_files_are_backed_up(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "status.json").write_text(json.dumps({
                "stage": "local_snapshot_verified", "checked_at": "2026-09-28T10:12:00Z",
                "snapshot_id": "20260928T101112123456Z-abcdef0123",
                "chat_capture": {"status": "failed", "reason": "private diagnostic"}
            }), encoding="utf-8")
            result = build_backup_status(root)
            self.assertTrue(result["local_snapshot_verified"])
            self.assertTrue(result["last_attempt_failed"])
            self.assertEqual(result["failure_kind"], "chat_capture")
            self.assertNotIn("private diagnostic", repr(result))

    def test_drive_staging_never_counts_as_remote_receipt_and_stale_receipt_is_distinguished(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot_id = "20260928T101112123456Z-abcdef0123"
            (root / "status.json").write_text(json.dumps({
                "stage": "staged_for_drive_sync", "checked_at": "2026-09-28T10:12:00Z",
                "staged_at": "2026-09-28T10:11:30Z", "snapshot_id": snapshot_id,
                "cloud_received": True, "cloud_file_id": "must-not-be-trusted"
            }), encoding="utf-8")

            staged = build_backup_status(root)
            self.assertTrue(staged["local_snapshot_verified"])
            self.assertTrue(staged["drive_staged_for_sync"])
            self.assertFalse(staged["remote_receipt_valid"])
            self.assertFalse(staged["remote_matches_latest_local"])

            receipt = {"verification": "remote_manifest_content_verified", "snapshot_id": snapshot_id,
                       "verified_at_utc": "2026-09-28T10:13:00Z"}
            (root / "cloud-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
            verified = build_backup_status(root)
            self.assertTrue(verified["remote_receipt_valid"])
            self.assertTrue(verified["remote_matches_latest_local"])

            receipt["snapshot_id"] = "older-snapshot"
            (root / "cloud-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
            stale = build_backup_status(root)
            self.assertTrue(stale["remote_receipt_valid"])
            self.assertFalse(stale["remote_matches_latest_local"])

    def test_failed_attempt_does_not_hide_prior_snapshot_time_or_claim_fresh_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot_id = "20260928T101112123456Z-abcdef0123"
            snapshots = root / "snapshots"
            snapshots.mkdir()
            (snapshots / f"{snapshot_id}.json").write_text(json.dumps({
                "snapshot_id": snapshot_id,
                "created_at": "2026-09-28T10:11:12.123Z",
                "files": []
            }), encoding="utf-8")
            (root / "status.json").write_text(json.dumps({
                "stage": "backup_failed", "checked_at": "2026-09-28T10:15:00Z",
                "error": "private error detail must not be surfaced"
            }), encoding="utf-8")

            status = build_backup_status(root)
            self.assertTrue(status["local_snapshot_recorded"])
            self.assertFalse(status["local_snapshot_verified"])
            self.assertEqual(status["local_snapshot_id"], snapshot_id)
            self.assertEqual(status["local_snapshot_at_utc"], "2026-09-28T10:11:12Z")
            self.assertTrue(status["last_attempt_failed"])
            self.assertNotIn("error", status)
            self.assertNotIn("private error detail", repr(status))

class TrustBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.app = create_app(Path(self.temp.name), expected_port=port, dashboard_root=Path(self.temp.name))
        self.client = TestClient(TestServer(self.app, host="127.0.0.1", port=port))
        await self.client.start_server()
        self.origin = f"http://127.0.0.1:{port}"
        page = await self.client.get("/")
        html = await page.text()
        match = re.search(r'name="csrf-token" content="([^"]+)"', html)
        self.assertIsNotNone(match)
        self.token = match.group(1)
        self.csrf_headers = {
            "Origin": self.origin,
            "Cookie": f"{CSRF_COOKIE}={self.token}",
            "X-CSRF-Token": self.token,
        }

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_second_tab_keeps_token_and_invalid_cookie_is_replaced(self):
        page = await self.client.get("/", headers={"Cookie": f"{CSRF_COOKIE}={self.token}"})
        self.assertIn(f'content="{self.token}"', await page.text())
        self.assertEqual(page.cookies[CSRF_COOKIE]["max-age"], "")
        saved = await self.client.post("/api/text", json={"text": "first tab still saves"},
                                       headers=self.csrf_headers)
        self.assertEqual(saved.status, 201)
        page = await self.client.get("/", headers={"Cookie": f"{CSRF_COOKIE}=invalid-token"})
        html = await page.text()
        self.assertNotIn('content="invalid-token"', html)
        self.assertRegex(html, r'name="csrf-token" content="[A-Za-z0-9_-]{43}"')

    async def test_mutations_require_same_origin_cookie_and_csrf_token(self):
        path = "/api/text"
        payload = {"title": "test", "text": "original"}
        no_csrf = await self.client.post(path, json=payload, headers={"Origin": self.origin})
        self.assertEqual(no_csrf.status, 403)
        wrong_origin = await self.client.post(
            path, json=payload,
            headers={"Origin": "https://attacker.example", "Cookie": f"{CSRF_COOKIE}={self.token}", "X-CSRF-Token": self.token},
        )
        self.assertEqual(wrong_origin.status, 403)
        wrong_host = await self.client.post(path, json=payload, headers={**self.csrf_headers, "Host": "attacker.example"})
        self.assertEqual(wrong_host.status, 421)

        accepted = await self.client.post(path, json=payload, headers=self.csrf_headers)
        self.assertEqual(accepted.status, 201)
        saved = await accepted.json()
        self.assertEqual(saved["revision"]["content"], "original")
        self.assertEqual(saved["revision"]["kind"], "original_text")
        instruction = await self.client.post(
            f"/api/sessions/{saved['session']['id']}/instructions",
            json={"parent_revision_id": saved["revision"]["id"], "text": "明确创建的任务"},
            headers=self.csrf_headers,
        )
        self.assertEqual(instruction.status, 201)
        exported = await self.client.get("/api/instructions.json")
        self.assertEqual(exported.status, 200)
        package = await exported.json()
        item = package["instructions"][0]
        self.assertEqual(item["source_document_id"], saved["session"]["id"])
        self.assertEqual(item["source_revision_id"], saved["revision"]["id"])
        self.assertEqual(item["dispatch_status"], "unknown_to_input_service")

    async def test_default_stays_localhost_only_and_ignores_forwarded_headers(self):
        page = await self.client.get("/")
        self.assertFalse(page.cookies[CSRF_COOKIE]["secure"])
        payload = {"text": "must not be saved"}
        private_origin = {"Origin": f"https://{PRIVATE_HOST}"}
        remote = await self.client.post("/api/text", json=payload, headers={
            **self.csrf_headers, **private_origin, "Host": PRIVATE_HOST, IDENTITY_HEADER: OWNER,
        })
        self.assertEqual(remote.status, 421)
        https_origin = await self.client.post("/api/text", json=payload, headers={**self.csrf_headers, **private_origin})
        self.assertEqual(https_origin.status, 403)
        forwarded = await self.client.post("/api/text", json=payload, headers={
            **self.csrf_headers, **private_origin, "X-Forwarded-Host": PRIVATE_HOST, "X-Forwarded-Proto": "https",
        })
        self.assertEqual(forwarded.status, 403)
        self.assertEqual(self.app[STORE_KEY].list_sessions(), [])

    async def test_recording_outside_the_newest_200_opens_by_id_and_the_page_reads_session_param(self):
        store = self.app[STORE_KEY]
        ids = [store.create_audio_session("audio_capture", f"录音 {i}", "audio/webm")["id"] for i in range(201)]
        listed = {item["id"] for item in (await (await self.client.get("/api/sessions")).json())["sessions"]}
        self.assertEqual(len(listed), 200)
        (old,) = set(ids) - listed                       # the one the list no longer shows
        detail = await self.client.get(f"/api/sessions/{old}")
        self.assertEqual(detail.status, 200)
        self.assertEqual((await detail.json())["session"]["id"], old)
        self.assertEqual((await self.client.get(f"/?session={old}")).status, 200)
        nav = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document", "Sec-Fetch-User": "?1"}
        self.assertEqual((await self.client.get(f"/?session={old}", headers=nav)).status, 200)          # a click from a page on disk
        self.assertEqual((await self.client.get(f"/?session={old}", headers={**nav, "Sec-Fetch-User": ""})).status, 403)   # not user-activated
        self.assertEqual((await self.client.get(f"/api/sessions/{old}", headers=nav)).status, 403)       # the API stays closed to other sites
        self.assertEqual((await self.client.get("/static/app.js", headers=nav)).status, 403)
        js = await (await self.client.get("/static/app.js")).text()
        self.assertIn('new URLSearchParams(location.search).get("session")', js)

    async def test_chunk_endpoint_accepts_same_bytes_twice_but_rejects_conflict(self):
        created = await self.client.post(
            "/api/sessions",
            json={"source": "audio_capture", "title": "test recording", "mime_type": "audio/webm"},
            headers=self.csrf_headers,
        )
        self.assertEqual(created.status, 201)
        sid = (await created.json())["session"]["id"]
        data = b"partial browser recording"
        digest = hashlib.sha256(data).hexdigest()
        url = f"/api/sessions/{sid}/chunks/0"
        headers = {**self.csrf_headers, "Content-Type": "application/octet-stream", "X-Chunk-SHA256": digest}
        first = await self.client.put(url, data=data, headers=headers)
        retry = await self.client.put(url, data=data, headers=headers)
        self.assertEqual(first.status, 200)
        self.assertFalse((await first.json())["duplicate"])
        self.assertTrue((await retry.json())["duplicate"])
        conflict_bytes = b"different bytes"
        conflict = await self.client.put(
            url, data=conflict_bytes,
            headers={**headers, "X-Chunk-SHA256": hashlib.sha256(conflict_bytes).hexdigest()},
        )
        self.assertEqual(conflict.status, 409)
        self.assertEqual(self.app[STORE_KEY]._chunk_path(sid, 0).read_bytes(), data)

    async def test_verification_endpoint_requires_csrf_and_rejects_rewrites(self):
        store = self.app[STORE_KEY]
        session = store.create_audio_session("audio_capture", "录音", "audio/webm")
        data = b"api recording"
        store.put_chunk(session["id"], 0, data, hashlib.sha256(data).hexdigest())
        session = store.finalize_audio(session["id"], 1, len(data))
        asr = store.append_revision(session["id"], "asr", "原始转写", source="asr", data={"text": "原始转写"})
        url = f"/api/sessions/{session['id']}/verifications"
        payload = {"revision_id": asr["id"], "original_sha256": session["original_sha256"]}

        no_csrf = await self.client.post(url, json=payload, headers={"Origin": self.origin})
        self.assertEqual(no_csrf.status, 403)
        self.assertEqual(store.list_verified_training_rows(), [])
        accepted = await self.client.post(url, json=payload, headers=self.csrf_headers)
        self.assertEqual(accepted.status, 201)
        self.assertEqual((await accepted.json())["verification"]["revision_id"], asr["id"])
        detail = await (await self.client.get(f"/api/sessions/{session['id']}")).json()
        self.assertTrue(detail["session"]["revisions"][0]["verification"]["current"])

        rewrite = await self.client.post(
            f"/api/sessions/{session['id']}/corrections",
            json={"parent_revision_id": asr["id"], "text": "改写后的表达", "correction_type": "user_rewrite"},
            headers=self.csrf_headers,
        )
        rewrite_id = (await rewrite.json())["revision"]["id"]
        rejected = await self.client.post(url, json={**payload, "revision_id": rewrite_id}, headers=self.csrf_headers)
        self.assertEqual(rejected.status, 400)
        draft = await self.client.post(
            f"/api/sessions/{session['id']}/corrections",
            json={"parent_revision_id": rewrite_id, "text": "改写后的表达再改", "correction_type": "draft"},
            headers=self.csrf_headers,
        )
        self.assertEqual(draft.status, 201)
        draft_id = (await draft.json())["revision"]["id"]
        unconfirmed = await self.client.post(url, json={**payload, "revision_id": draft_id}, headers=self.csrf_headers)
        self.assertEqual(unconfirmed.status, 400)
        self.assertEqual(store.list_verified_training_rows(), [])
        self.assertEqual(store.get_session(session["id"])["original_sha256"], hashlib.sha256(data).hexdigest())


class PrivateHttpsOriginTests(unittest.IsolatedAsyncioTestCase):
    """Requests as Tailscale Serve forwards them: HTTPS Host/Origin arriving on the loopback listener."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.app = create_app(Path(self.temp.name), expected_port=self.port,
                              https_host=PRIVATE_HOST, owner_login=OWNER, dashboard_root=Path(self.temp.name))
        self.client = TestClient(TestServer(self.app, host="127.0.0.1", port=self.port))
        await self.client.start_server()
        self.owner = {"Host": PRIVATE_HOST, IDENTITY_HEADER: OWNER}
        page = await self.client.get("/", headers=self.owner)
        self.assertEqual(page.status, 200)
        self.assertTrue(page.cookies[CSRF_COOKIE]["secure"])
        match = re.search(r'name="csrf-token" content="([^"]+)"', await page.text())
        self.assertIsNotNone(match)
        self.token = match.group(1)
        # Only the explicit headers below may carry the cookie.
        self.client.session.cookie_jar.clear()
        self.headers = {
            **self.owner,
            "Origin": f"https://{PRIVATE_HOST}",
            "Cookie": f"{CSRF_COOKIE}={self.token}",
            "X-CSRF-Token": self.token,
        }

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_configured_private_origin_saves_and_localhost_still_works(self):
        saved = await self.client.post("/api/text", json={"text": "phone input"}, headers=self.headers)
        self.assertEqual(saved.status, 201)
        sid = (await saved.json())["session"]["id"]
        detail = await self.client.get(f"/api/sessions/{sid}", headers=self.owner)
        self.assertEqual(detail.status, 200)
        self.assertEqual((await detail.json())["session"]["revisions"][0]["content"], "phone input")
        script = await self.client.get("/static/app.js", headers=self.owner)
        self.assertEqual(script.status, 200)

        # Local processes are inside the existing trust boundary and need no identity header.
        local = f"127.0.0.1:{self.port}"
        page = await self.client.get("/", headers={"Host": local, "Cookie": f"{CSRF_COOKIE}={self.token}"})
        self.assertEqual(page.status, 200)
        self.assertFalse(page.cookies[CSRF_COOKIE]["secure"])
        local_headers = {key: value for key, value in self.headers.items() if key != IDENTITY_HEADER}
        local_saved = await self.client.post("/api/text", json={"text": "desktop input"}, headers={
            **local_headers, "Host": local, "Origin": f"http://{local}",
        })
        self.assertEqual(local_saved.status, 201)
        local_no_csrf = await self.client.post("/api/text", json={"text": "rejected"}, headers={
            "Host": local, "Origin": f"http://{local}",
        })
        self.assertEqual(local_no_csrf.status, 403)

    async def test_remote_host_requires_exactly_one_owner_identity_everywhere(self):
        # Absent header is also what a tagged device (no user identity) sends through Serve.
        wrong = [
            [],
            [(IDENTITY_HEADER, "someone-else@example.com")],
            [(IDENTITY_HEADER, "Owner@example.com")],
            [(IDENTITY_HEADER, "")],
            [(IDENTITY_HEADER, f"{OWNER}, someone-else@example.com")],
            [(IDENTITY_HEADER, OWNER), (IDENTITY_HEADER, OWNER)],
            [(IDENTITY_HEADER, OWNER), (IDENTITY_HEADER, "someone-else@example.com")],
            [("X-Forwarded-User", OWNER), ("Tailscale-User-Name", OWNER)],
        ]
        base = [(key, value) for key, value in self.headers.items() if key != IDENTITY_HEADER]
        for identity in wrong:
            headers = CIMultiDict(base + identity)
            with self.subTest(identity=identity):
                for path in ("/", "/static/app.js", "/static/style.css", "/api/sessions", "/api/instructions.json"):
                    response = await self.client.get(path, headers=headers)
                    self.assertEqual(response.status, 403, path)
                    self.assertNotIn(CSRF_COOKIE, response.cookies)
                saved = await self.client.post("/api/text", json={"text": "rejected"}, headers=headers)
                self.assertEqual(saved.status, 403)
        self.assertEqual(self.app[STORE_KEY].list_sessions(), [])

        # The owner still passes identity but not CSRF without the token.
        no_csrf = await self.client.post("/api/text", json={"text": "rejected"}, headers={
            **self.owner, "Origin": f"https://{PRIVATE_HOST}",
        })
        self.assertEqual(no_csrf.status, 403)
        self.assertEqual(self.app[STORE_KEY].list_sessions(), [])

    async def test_host_without_owner_at_runtime_fails_closed(self):
        app = create_app(Path(self.temp.name) / "other", https_host=PRIVATE_HOST, owner_login=OWNER,
                         dashboard_root=Path(self.temp.name))
        del app[OWNER_LOGIN_KEY]
        client = TestClient(TestServer(app, host="127.0.0.1"))
        await client.start_server()
        try:
            response = await client.get("/", headers=self.owner)
            self.assertEqual(response.status, 403)
        finally:
            await client.close()

    async def test_unpaired_or_unconfigured_origins_and_csrf_failures_are_refused(self):
        local = f"127.0.0.1:{self.port}"
        cases = [
            ({"Host": "other-pc.example-tailnet.ts.net", "Origin": "https://other-pc.example-tailnet.ts.net"}, 421),
            ({"Host": "attacker.example", "X-Forwarded-Host": PRIVATE_HOST, "X-Forwarded-Proto": "https"}, 421),
            ({"Origin": f"http://{PRIVATE_HOST}"}, 403),
            ({"Origin": f"http://{local}"}, 403),
            ({"Host": local}, 403),
            ({"Origin": "https://attacker.example"}, 403),
            ({"Origin": "null"}, 403),
            ({"Sec-Fetch-Site": "cross-site"}, 403),
            ({"X-CSRF-Token": "wrong-token"}, 403),
        ]
        for override, status in cases:
            with self.subTest(override=override):
                response = await self.client.post("/api/text", json={"text": "rejected"},
                                                  headers={**self.headers, **override})
                self.assertEqual(response.status, status)
        for missing in ("Origin", "Cookie", "X-CSRF-Token"):
            with self.subTest(missing=missing):
                headers = {key: value for key, value in self.headers.items() if key != missing}
                response = await self.client.post("/api/text", json={"text": "rejected"}, headers=headers)
                self.assertEqual(response.status, 403)
        self.assertEqual(self.app[STORE_KEY].list_sessions(), [])

    def test_only_one_exact_magicdns_host_can_be_configured(self):
        for value in ("", "*.example-tailnet.ts.net", f"https://{PRIVATE_HOST}", f"{PRIVATE_HOST}:443",
                      f"{PRIVATE_HOST}.", f"{PRIVATE_HOST}/", "example-tailnet.ts.net", "attacker.example",
                      "100.64.0.1", "127.0.0.1", f"a.{PRIVATE_HOST} b.{PRIVATE_HOST}"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                create_app(Path(self.temp.name), https_host=value, owner_login=OWNER)

    def test_host_and_one_owner_login_must_be_configured_together(self):
        for kwargs in ({"https_host": PRIVATE_HOST}, {"owner_login": OWNER},
                       {"https_host": PRIVATE_HOST, "owner_login": ""},
                       {"https_host": PRIVATE_HOST, "owner_login": "owner"},
                       {"https_host": PRIVATE_HOST, "owner_login": "*@example.com"},
                       {"https_host": PRIVATE_HOST, "owner_login": f"{OWNER},other@example.com"},
                       {"https_host": PRIVATE_HOST, "owner_login": f"{OWNER} "},
                       {"https_host": PRIVATE_HOST, "owner_login": ["owner@example.com"]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                create_app(Path(self.temp.name), **kwargs)


class PrivateConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / PRIVATE_CONFIG

    def tearDown(self):
        self.temp.cleanup()

    def _app(self, env_host=None):
        # Same composition as main(); temporary data only.
        return create_app(self.root, dashboard_root=self.root, **(load_private_config(self.root, env_host) or {}))

    def test_missing_config_keeps_localhost_defaults(self):
        self.assertIsNone(load_private_config(self.root))
        self.assertIsNone(load_private_config(self.root, "  "))
        app = self._app()
        self.assertNotIn(OWNER_LOGIN_KEY, app)
        with self.assertRaises(ValueError):
            load_private_config(self.root, PRIVATE_HOST)  # host alone is incomplete remote config

    def test_valid_config_configures_exact_host_and_owner(self):
        self.path.write_text(json.dumps({"https_host": PRIVATE_HOST, "owner_login": OWNER}), encoding="utf-8")
        self.assertEqual(load_private_config(self.root), {"https_host": PRIVATE_HOST, "owner_login": OWNER})
        self.assertEqual(self._app()[OWNER_LOGIN_KEY], OWNER)
        self.assertEqual(self._app(PRIVATE_HOST.upper())[OWNER_LOGIN_KEY], OWNER)
        # Windows PowerShell 5.1 writes a UTF-8 BOM; accept it rather than mis-parse.
        self.path.write_bytes(b"\xef\xbb\xbf" + self.path.read_bytes())
        self.assertEqual(self._app()[OWNER_LOGIN_KEY], OWNER)

    def test_malformed_or_incomplete_config_fails_closed(self):
        bodies = [
            "", "not json", "[]", "null", '"owner@example.com"',
            json.dumps({"https_host": PRIVATE_HOST}),
            json.dumps({"owner_login": OWNER}),
            json.dumps({"https_host": PRIVATE_HOST, "owner_login": OWNER, "port": 8765}),
            json.dumps({"https_host": PRIVATE_HOST, "owner_login": [OWNER]}),
            json.dumps({"https_host": PRIVATE_HOST, "owner_login": None}),
            json.dumps({"https_host": PRIVATE_HOST, "owner_login": ""}),
            json.dumps({"https_host": "", "owner_login": OWNER}),
            json.dumps({"https_host": "attacker.example", "owner_login": OWNER}),
            json.dumps({"https_host": "*.example-tailnet.ts.net", "owner_login": OWNER}),
            json.dumps({"https_host": PRIVATE_HOST, "owner_login": f"{OWNER},other@example.com"}),
            f'{{"https_host": "{PRIVATE_HOST}", "owner_login": "{OWNER}", "owner_login": "other@example.com"}}',
            json.dumps({"https_host": PRIVATE_HOST, "owner_login": OWNER, "pad": "x" * 5000}),
        ]
        for body in bodies:
            with self.subTest(body=body[:80]), self.assertRaises(ValueError):
                self.path.write_text(body, encoding="utf-8")
                self._app()
        self.path.write_bytes(b"\xff\xfe{\x00}\x00")
        with self.assertRaises(ValueError):
            self._app()
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises(ValueError):
            self._app()

    def test_environment_host_must_agree_with_config(self):
        self.path.write_text(json.dumps({"https_host": PRIVATE_HOST, "owner_login": OWNER}), encoding="utf-8")
        with self.assertRaises(ValueError):
            self._app("other-pc.example-tailnet.ts.net")


if __name__ == "__main__":
    unittest.main()
