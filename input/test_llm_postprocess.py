from __future__ import annotations

import asyncio
import hashlib
import json
import re
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from . import llm_postprocess
from .server import CSRF_COOKIE, create_app
from .store import InputError, InputStore

RAW = "嗯 明天下午三点开会 嗯"
CLEAN = "明天下午三点开会"
# Synthetic 17-minute / ~4000-character, 37-paragraph shape; no production records.
LONG_TRANSCRIPT = "\n\n".join(
    f"第{i}段讨论课程设计和实验安排。"
    "课堂先讲理论，再用visualization展示实验结果，学生记录观察并解释自己的选择。"
    "每周保留一次讨论，老师整理问题，下一周接着验证；模型只负责标点和语气整理，具体内容需要完整保留。"
    for i in range(1, 38)
) + "最后的核对暗号是青瓷白鹭，明早携带深蓝文件夹！"


class CoverageTests(unittest.TestCase):
    def test_long_complete_missing_500_and_half_length(self):
        for text, short, tail_found in (
            (LONG_TRANSCRIPT, False, True),
            (LONG_TRANSCRIPT[:-500], False, False),
            (LONG_TRANSCRIPT[:len(LONG_TRANSCRIPT) // 2] + LONG_TRANSCRIPT[-80:], True, True),
        ):
            with self.subTest(short=short, tail_found=tail_found):
                info = llm_postprocess.coverage_info(LONG_TRANSCRIPT, text)
                self.assertEqual(info["polish_length_ratio"] < 0.6, short)
                self.assertEqual(info["polish_tail_found"], tail_found)

    def test_tail_ignores_punctuation_case_and_minor_polishing(self):
        for original, polished in (
            ("前一件事。嗯，明天根据进度去做一些调整！", "明天根据进度调整。"),
            ("First part? Final check is DeepBlue!", "Final check is DEEPBLUE."),
            ("前文。收尾？", "收尾！"),
            ("没有结束标点的青瓷白鹭", "核对青瓷白鹭。"),
            ("末句超过三百字：" + "青瓷白鹭" * 100 + "。", "青瓷白鹭" * 100),
        ):
            with self.subTest(original=original[:30]):
                self.assertTrue(llm_postprocess.coverage_info(original, polished)["polish_tail_found"])
        self.assertEqual(llm_postprocess.coverage_info("空 格\n。", "空格。对" )["polish_length_ratio"], 4 / 3)
        self.assertEqual(llm_postprocess.coverage_info("", ""),
                         {"polish_length_ratio": 1.0, "polish_tail_found": True})

    def test_last_sentence_must_be_in_the_final_300_characters(self):
        info = llm_postprocess.coverage_info("前文。收尾暗号青瓷白鹭。", "收尾暗号青瓷白鹭。" + "别" * 301)
        self.assertFalse(info["polish_tail_found"])


class FakeEndpoint:
    """Stdlib stand-in for llama-server: no network beyond loopback, no paid API."""

    def __init__(self, mode: str = "ok", delay: float = 0.0):
        self.mode, self.delay, self.requests = mode, delay, []
        self.replies = None
        self.finish_reasons = None
        self.delays = None
        self.payloads = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                time.sleep(next(outer.delays) if outer.delays is not None else outer.delay)
                reply = body["messages"][-1]["content"] if outer.mode == "echo" else CLEAN
                if outer.replies is not None:
                    reply = next(outer.replies)
                reason = next(outer.finish_reasons) if outer.finish_reasons is not None else "length" if outer.mode == "length" else "stop"
                payload = {"choices": [{"message": {"content": "<think></think>" + reply},
                                        "finish_reason": reason}]}
                data = json.dumps(next(outer.payloads) if outer.payloads is not None else payload).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass  # client gave up (timeout test)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/chat/completions"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("Clean the text.", encoding="utf-8")
        self.endpoint = FakeEndpoint()
        self.store = InputStore(self.root)

    def tearDown(self):
        self.endpoint.close()
        self.temp.cleanup()

    def settings(self, **extra):
        value = {"enabled": True, "endpoint": self.endpoint.url, "model": "fake", "prompt_path": str(self.prompt),
                 "timeout_s": 5, **extra}
        (self.root / llm_postprocess.SETTINGS_FILE).write_text(json.dumps(value), encoding="utf-8")
        return value

    def transcript(self):
        sid = self.store.create_text_session("t", RAW)[0]["id"]
        return sid


class BackendTests(Base):
    def test_default_endpoint_uses_unoccupied_port(self):
        self.assertEqual(llm_postprocess.DEFAULTS["endpoint"], "http://127.0.0.1:8086/v1/chat/completions")

    def test_startup_reaps_recorded_llama_server_on_matching_port(self):
        marker = self.root / llm_postprocess.SERVER_PID_FILE
        marker.write_text(json.dumps({"pid": 4242, "port": 18086}), encoding="utf-8")
        args = [r"C:\models\llama-server.exe", "-m", "m.gguf", "--host", "127.0.0.1", "--port", "18086"]
        with patch.object(llm_postprocess, "_process_args", return_value=args), \
                patch.object(llm_postprocess.os, "kill") as kill:
            llm_postprocess.cleanup_orphan_server(self.root)
        kill.assert_called_once_with(4242, llm_postprocess.signal.SIGTERM)
        self.assertFalse(marker.exists())

    def test_startup_does_not_kill_a_different_process(self):
        marker = self.root / llm_postprocess.SERVER_PID_FILE
        marker.write_text(json.dumps({"pid": 4242, "port": 18086}), encoding="utf-8")
        args = [r"C:\Python\python.exe", "-c", "llama-server --port 18086"]
        with patch.object(llm_postprocess, "_process_args", return_value=args), \
                patch.object(llm_postprocess.os, "kill") as kill:
            llm_postprocess.cleanup_orphan_server(self.root)
        kill.assert_not_called()
        self.assertFalse(marker.exists())

    def test_startup_does_not_kill_llama_server_on_a_different_port(self):
        marker = self.root / llm_postprocess.SERVER_PID_FILE
        marker.write_text(json.dumps({"pid": 4242, "port": 18086}), encoding="utf-8")
        args = [r"C:\models\llama-server.exe", "-m", "m.gguf", "--port", "8081"]
        with patch.object(llm_postprocess, "_process_args", return_value=args), \
                patch.object(llm_postprocess.os, "kill") as kill:
            llm_postprocess.cleanup_orphan_server(self.root)
        kill.assert_not_called()
        self.assertFalse(marker.exists())

    def test_success_appends_llm_revision_and_keeps_original(self):
        self.settings()
        sid = self.transcript()
        before = self.store.session_detail(sid)["revisions"]
        rev = llm_postprocess.run(self.store, sid)
        self.assertEqual((rev["kind"], rev["source"], rev["content"]), ("postprocess", "llm_postprocess", CLEAN))
        self.assertEqual(rev["parent_id"], before[0]["id"])
        after = self.store.session_detail(sid)["revisions"]
        self.assertEqual([r["id"] for r in after[:-1]], [r["id"] for r in before])
        self.assertEqual(after[0]["content"], RAW)
        sent = self.endpoint.requests[0]
        self.assertEqual([m["content"] for m in sent["messages"]], ["Clean the text.", RAW])
        self.assertEqual(sent["model"], "fake")

    def test_long_transcript_goes_in_pieces_and_nothing_is_lost(self):
        # owner\x2011-02: a 4030-char transcript came back cut at max_tokens and the end was silently dropped
        self.settings()
        self.endpoint.mode = "echo"
        long = "".join(f"第{i}句话说的是一件事情，后面还有一点补充。" for i in range(200))
        sid = self.store.create_text_session("t", long)[0]["id"]
        rev = llm_postprocess.run(self.store, sid)
        self.assertEqual(rev["content"], long)
        sent = [r["messages"][-1]["content"] for r in self.endpoint.requests]
        self.assertGreater(len(sent), 1)
        self.assertTrue(all(len(s) <= llm_postprocess.CHUNK_CHARS for s in sent))

    def test_repetition_retries_same_piece_with_greedy_repeat_penalty(self):
        self.settings()
        source = "这一句需要润色。"
        repeated = "模型重复输出的整段内容。" * 8
        self.endpoint.replies = iter([repeated, "润色后的完整句子。"])
        self.endpoint.finish_reasons = iter(["stop", "stop"])
        sid = self.store.create_text_session("t", source)[0]["id"]

        rev = llm_postprocess.run(self.store, sid)

        self.assertEqual(rev["content"], "润色后的完整句子。")
        self.assertEqual(rev["data"]["polish_repeat_count"], 1)
        self.assertEqual(rev["data"]["polish_retry_recovered"], 1)
        self.assertEqual(rev["data"]["polish_unpolished_sentences"], [])
        self.assertEqual(len(self.endpoint.requests), 2)
        self.assertEqual(self.endpoint.requests[0]["messages"], self.endpoint.requests[1]["messages"])
        self.assertEqual(self.endpoint.requests[1]["temperature"], 0)
        self.assertEqual(self.endpoint.requests[1]["repeat_penalty"], 1.1)
        self.assertEqual(self.endpoint.requests[1]["repeat_last_n"], 64)

    def test_repetition_after_two_retries_keeps_only_that_sentence(self):
        self.settings()
        source = "这句会被标出来。"
        self.endpoint.replies = iter(["模型重复输出的整段内容。" * 8] * 3)
        self.endpoint.finish_reasons = iter(["stop"] * 3)
        sid = self.store.create_text_session("t", source)[0]["id"]

        rev = llm_postprocess.run(self.store, sid)

        self.assertEqual(rev["content"], source)
        self.assertEqual(rev["data"]["polish_repeat_count"], 3)
        self.assertEqual(rev["data"]["polish_retry_recovered"], 0)
        self.assertEqual(rev["data"]["polish_unpolished_sentences"], [source])
        self.assertEqual(rev["data"]["polish_unpolished_sentence_count"], 1)
        self.assertEqual([body.get("repeat_penalty") for body in self.endpoint.requests], [None, 1.1, 1.2])

    def test_repeated_sentence_restored_from_original_keeps_its_leading_space(self):
        self.settings()
        source = "第一句原文。 第二句复读。"
        repeated = "模型重复输出的整段内容。" * 8
        self.endpoint.replies = iter([repeated] * 3 + ["第一句润色。"] + [repeated] * 3)
        self.endpoint.finish_reasons = iter(["stop"] * 7)
        sid = self.store.create_text_session("t", source)[0]["id"]

        rev = llm_postprocess.run(self.store, sid)

        self.assertEqual(rev["content"], "第一句润色。 第二句复读。")
        self.assertEqual(rev["data"]["polish_unpolished_sentences"], [" 第二句复读。"])
        self.assertEqual(len(self.endpoint.requests), 7)

    def test_seam_space_survives_between_pieces_for_curly_quotes_and_accented_letters(self):
        # sol 11-03: "First sentence! “Next sentence.”" and an "Éclair" opening lost the space where pieces join
        self.settings()
        self.endpoint.mode = "echo"
        split = llm_postprocess.split_text
        for text in ("First sentence! “Next sentence.”", "Is it done? Éclair is ready.", "Done! Next one? Yes! Last."):
            with self.subTest(text=text), patch.object(llm_postprocess, "split_text", lambda t: split(t, 14)):
                sid = self.store.create_text_session("t", text)[0]["id"]
                self.assertEqual(llm_postprocess.run(self.store, sid)["content"], text)
                self.assertGreater(len(self.endpoint.requests), 1)
                self.endpoint.requests.clear()

    def test_length_limited_piece_splits_once_and_polishes_both_halves(self):
        self.settings()
        text = "甲" * 600 + "，" + "乙" * 600 + "。"
        halves = llm_postprocess._split_piece_at_boundary(text)
        self.assertIsNotNone(halves)
        self.endpoint.replies = iter(["截断结果", "甲段润色", "乙段润色"])
        self.endpoint.finish_reasons = iter(["length", "stop", "stop"])
        sid = self.store.create_text_session("t", text)[0]["id"]

        rev = llm_postprocess.run(self.store, sid)

        self.assertEqual(rev["content"], "甲段润色乙段润色")
        self.assertEqual(rev["data"]["polish_unpolished_sentences"], [])
        self.assertEqual([r["messages"][-1]["content"] for r in self.endpoint.requests], [text, *halves])
        self.assertEqual(self.store.get_revision(rev["id"])["content"], rev["content"])

    def test_length_limited_piece_splits_past_old_depth_until_all_leaves_polish(self):
        self.settings()
        text = "，".join(part * 300 for part in "甲乙丙丁") + "。"
        expected_requests, replies, finishes, polished = [], [], [], []

        def schedule(piece, depth):
            expected_requests.append(piece)
            if depth:
                replies.append("截断输出")
                finishes.append("length")
                halves = llm_postprocess._split_piece_at_boundary(piece)
                self.assertIsNotNone(halves)
                return schedule(halves[0], depth - 1) + schedule(halves[1], depth - 1)
            result = f"叶子{len(polished) + 1}润色"
            replies.append(result)
            finishes.append("stop")
            polished.append(result)
            return result

        expected = schedule(text, 3)
        self.endpoint.replies = iter(replies)
        self.endpoint.finish_reasons = iter(finishes)
        sid = self.store.create_text_session("t", text)[0]["id"]

        rev = llm_postprocess.run(self.store, sid)

        self.assertEqual(rev["content"], expected)
        self.assertEqual(rev["data"]["polish_unpolished_sentences"], [])
        self.assertEqual(rev["data"]["polish_length_split_count"], 7)
        self.assertEqual([r["messages"][-1]["content"] for r in self.endpoint.requests], expected_requests)

    def test_length_limit_at_unsplittable_text_uses_the_model_result(self):
        self.settings()
        self.endpoint.replies = iter(["最后一个可用的模型结果"])
        self.endpoint.finish_reasons = iter(["length"])
        sid = self.store.create_text_session("t", "甲")[0]["id"]

        rev = llm_postprocess.run(self.store, sid)

        self.assertEqual(rev["content"], "最后一个可用的模型结果")
        self.assertEqual(rev["data"]["polish_unpolished_sentences"], [])
        self.assertEqual([r["messages"][-1]["content"] for r in self.endpoint.requests], ["甲"])

    def test_polished_revision_persists_coverage_against_the_sent_draft(self):
        self.settings()
        sid = self.transcript()  # deliberately unrelated original; compare the selected draft instead
        original = self.store.session_detail(sid)["revisions"][0]
        draft = self.store.append_revision(sid, "correction", LONG_TRANSCRIPT, original["id"], "user",
                                           {"correction_type": "draft"})
        pieces = llm_postprocess.split_text(LONG_TRANSCRIPT)
        for output in (LONG_TRANSCRIPT, LONG_TRANSCRIPT[:-500], LONG_TRANSCRIPT[:len(LONG_TRANSCRIPT) // 2]):
            with self.subTest(output_chars=len(output)):
                replies = [output[len(output) * i // len(pieces):len(output) * (i + 1) // len(pieces)]
                           for i in range(len(pieces))]
                self.endpoint.replies = iter(replies)
                rev = llm_postprocess.run(self.store, sid, revision_id=draft["id"])
                expected = "".join(part.strip() for part in replies)
                self.assertEqual(rev["content"], expected)
                self.assertEqual(rev["parent_id"], draft["id"])
                for key, value in llm_postprocess.coverage_info(LONG_TRANSCRIPT, expected).items():
                    self.assertEqual(self.store.get_revision(rev["id"])["data"][key], value)
                self.assertEqual("".join(r["messages"][-1]["content"] for r in self.endpoint.requests[-len(pieces):]),
                                 LONG_TRANSCRIPT)
        self.assertEqual(self.store.get_revision(original["id"])["content"], RAW)
        self.assertEqual(self.store.get_revision(draft["id"])["content"], LONG_TRANSCRIPT)

    def test_unsplittable_length_limit_does_not_persist_original_fallback(self):
        self.settings()
        self.endpoint.mode = "length"
        sid = self.store.create_text_session("t", "甲")[0]["id"]
        before = self.store.session_detail(sid)["revisions"]
        rev = llm_postprocess.run(self.store, sid)
        after = self.store.session_detail(sid)["revisions"]
        self.assertEqual(rev["content"], CLEAN)
        self.assertEqual(rev["data"]["polish_unpolished_sentences"], [])
        self.assertEqual(after[:-1], before)
        self.assertEqual(after[-1]["id"], rev["id"])

    def test_three_pieces_polish_all_segments_when_one_needs_splitting(self):
        self.settings()
        pieces = [f"\n第{i}段 " + "样" * 1188 + "  。" for i in range(1, 4)]
        text = "".join(pieces)
        self.assertEqual(llm_postprocess.split_text(text), pieces)
        halves = llm_postprocess._split_piece_at_boundary(pieces[1])
        self.assertIsNotNone(halves)
        self.endpoint.replies = iter(["第一段润色完。", "截断的输出不能写入", "第二段左侧润色。",
                                      "第二段右侧润色。", "第三段润色完。"])
        self.endpoint.finish_reasons = iter(["stop", "length", "stop", "stop", "stop"])
        sid = self.store.create_text_session("t", text)[0]["id"]
        before = self.store.session_detail(sid)["revisions"]
        rev = llm_postprocess.run(self.store, sid)
        expected = "第一段润色完。第二段左侧润色。第二段右侧润色。第三段润色完。"
        self.assertEqual(rev["content"], expected)
        self.assertEqual(rev["data"]["polish_unpolished_sentences"], [])
        after = self.store.session_detail(sid)["revisions"]
        self.assertEqual(after[:-1], before)
        self.assertEqual(after[-1]["id"], rev["id"])
        self.assertEqual(after[-1]["content"], rev["content"])
        self.assertEqual([r["messages"][-1]["content"] for r in self.endpoint.requests],
                         [pieces[0], pieces[1], *halves, pieces[2]])

    def test_three_normal_pieces_save_all_polished_text_without_fallback(self):
        self.settings()
        pieces = [f"第{i}段" + "样" * 1196 + "。" for i in range(1, 4)]
        self.assertEqual(llm_postprocess.split_text("".join(pieces)), pieces)
        replies = [f"第{i}段润色完。" for i in range(1, 4)]
        self.endpoint.replies = iter(replies)
        sid = self.store.create_text_session("t", "".join(pieces))[0]["id"]
        rev = llm_postprocess.run(self.store, sid)
        self.assertEqual(rev["content"], "".join(replies))
        self.assertEqual(rev["data"]["polish_unpolished_sentences"], [])
        self.assertEqual(rev["data"]["polish_piece_count"], 3)
        self.assertEqual(self.store.get_revision(rev["id"]), rev)

    def test_empty_output_after_a_length_split_still_writes_nothing(self):
        self.settings()
        self.endpoint.replies = iter(["截断的输出", ""])
        self.endpoint.finish_reasons = iter(["length", "stop"])
        text = ("样" * 1199 + "。") * 3
        sid = self.store.create_text_session("t", text)[0]["id"]
        before = self.store.session_detail(sid)["revisions"]
        with self.assertRaises(InputError) as caught:
            llm_postprocess.run(self.store, sid)
        self.assertIn("空文字", caught.exception.message)
        self.assertEqual(self.store.session_detail(sid)["revisions"], before)
        self.assertEqual(len(self.endpoint.requests), 2)

    def test_split_keeps_every_character_and_an_overlong_sentence_whole(self):
        text = "短句。" * 5 + "很" * 50 + "。尾巴没有句号"
        pieces = llm_postprocess.split_text(text, limit=20)
        self.assertEqual("".join(pieces), text)
        self.assertIn("很" * 50 + "。", pieces)

    def test_timeout_writes_nothing(self):
        self.endpoint.delay = 1.5
        self.settings(timeout_s=0.3)
        sid = self.transcript()
        with self.assertRaises(InputError) as caught:
            llm_postprocess.run(self.store, sid)
        self.assertEqual(caught.exception.status, 502)
        self.assertIn("没有应答", caught.exception.message)
        self.assertEqual(len(self.store.session_detail(sid)["revisions"]), 1)

    def test_unreachable_endpoint_writes_nothing(self):
        self.settings(endpoint="http://127.0.0.1:1/v1/chat/completions")
        sid = self.transcript()
        with self.assertRaises(InputError):
            llm_postprocess.run(self.store, sid)
        self.assertEqual(len(self.store.session_detail(sid)["revisions"]), 1)

    def test_missing_prompt_file_is_a_clear_error_and_writes_nothing(self):
        self.settings(prompt_path=str(self.root / "nope.txt"))
        sid = self.transcript()
        with self.assertRaises(InputError) as caught:
            llm_postprocess.run(self.store, sid)
        self.assertIn("提示词文件不存在", caught.exception.message)
        self.assertIn("nope.txt", caught.exception.message)
        self.assertEqual(self.endpoint.requests, [])
        self.assertEqual(len(self.store.session_detail(sid)["revisions"]), 1)

    def test_off_by_default(self):
        sid = self.transcript()
        self.assertFalse(llm_postprocess.load_settings(self.root)["enabled"])
        self.assertFalse(llm_postprocess.load_settings(self.root)["auto"])
        with self.assertRaises(InputError) as caught:
            llm_postprocess.run(self.store, sid)
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("润色", caught.exception.message)
        self.assertEqual(self.endpoint.requests, [])

    def verifiable_session(self, text=RAW):
        session = self.store.create_audio_session("audio_capture", "录音", "audio/webm")
        data = b"complete original recording"
        self.store.put_chunk(session["id"], 0, data, hashlib.sha256(data).hexdigest())
        session = self.store.finalize_audio(session["id"], 1, len(data))
        asr = self.store.append_revision(session["id"], "asr", text, source="asr", data={"text": text})
        return session, asr

    def draft(self, sid, parent, text):
        return self.store.append_revision(sid, "correction", text, parent["id"], "user", {"correction_type": "draft"})

    def test_polish_uses_the_revision_the_editor_shows_not_the_original(self):
        self.settings()
        session, asr = self.verifiable_session()
        hand = self.draft(session["id"], asr, "我手改过的稿子")
        rev = llm_postprocess.run(self.store, session["id"], revision_id=hand["id"])
        self.assertEqual(self.endpoint.requests[0]["messages"][1]["content"], "我手改过的稿子")
        self.assertEqual(rev["parent_id"], hand["id"])
        llm_postprocess.run(self.store, session["id"])      # no id (old caller / automatic run): still the transcript
        self.assertEqual(self.endpoint.requests[1]["messages"][1]["content"], RAW)

    def test_revision_of_another_record_is_refused(self):
        self.settings()
        _, other = self.verifiable_session()
        session, _ = self.verifiable_session()
        for bad in (other["id"], "00000000-0000-0000-0000-000000000000"):
            with self.assertRaises(InputError) as caught:
                llm_postprocess.run(self.store, session["id"], revision_id=bad)
            self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.endpoint.requests, [])

    def test_machine_polish_is_never_marked_human_verified(self):
        self.settings()
        session, asr = self.verifiable_session()
        rev = llm_postprocess.run(self.store, session["id"], revision_id=asr["id"])
        self.assertIs(rev["data"]["human_verified"], False)
        shown = {r["id"]: r for r in self.store.session_detail(session["id"])["revisions"]}
        self.assertIsNone(shown[rev["id"]]["verification"])
        self.assertEqual(self.store.list_verified_training_rows(), [])

    def test_a_person_can_verify_the_polished_text_as_is_or_after_editing_it(self):
        self.settings()
        session, asr = self.verifiable_session()
        sha = session["original_sha256"]
        rev = llm_postprocess.run(self.store, session["id"], revision_id=asr["id"])
        self.store.verify_revision(session["id"], rev["id"], sha)       # click「确认保存」on the machine text
        shown = {r["id"]: r for r in self.store.session_detail(session["id"])["revisions"]}
        self.assertTrue(shown[rev["id"]]["verification"]["current"])
        self.assertIs(shown[rev["id"]]["data"]["human_verified"], False)    # the machine's own flag stays false
        rev2 = llm_postprocess.run(self.store, session["id"], revision_id=rev["id"])
        fixed = self.store.append_revision(session["id"], "correction", "人改好的", rev2["id"], "user", {"correction_type": "recognition_correction"})
        self.store.verify_revision(session["id"], fixed["id"], sha)     # edit the polished text, then verify
        self.assertEqual([r["text"] for r in self.store.list_verified_training_rows()], ["人改好的"])

    def test_polishing_a_verified_record_adds_an_unverified_version_and_keeps_the_check(self):
        self.settings()
        session, asr = self.verifiable_session()
        self.store.verify_revision(session["id"], asr["id"], session["original_sha256"])
        rev = llm_postprocess.run(self.store, session["id"], revision_id=asr["id"])
        shown = {r["id"]: r for r in self.store.session_detail(session["id"])["revisions"]}
        self.assertTrue(shown[asr["id"]]["verification"]["current"])
        self.assertIsNone(shown[rev["id"]]["verification"])
        self.assertEqual(rev["content"], CLEAN)
        self.assertEqual([r["text"] for r in self.store.list_verified_training_rows()], [RAW])
    def asr_session(self, text):
        sid = self.store.create_audio_session("audio_capture", "录音", "audio/webm")["id"]
        data = b"recording " + text.encode()
        self.store.put_chunk(sid, 0, data, hashlib.sha256(data).hexdigest())
        self.store.finalize_audio(sid, 1, len(data))
        self.store.append_asr_result(sid, {"text": text})
        return sid

    def test_model_gets_the_rules_version_and_parents_it(self):
        self.settings()
        sid = self.asr_session("嗯 我用 cloud code 写代码")
        revs = self.store.session_detail(sid)["revisions"]
        self.assertEqual([r["source"] for r in revs], ["asr", "asr_postprocess"])
        rule_version = revs[1]
        self.assertIn("Claude Code", rule_version["content"])
        rev = llm_postprocess.run(self.store, sid)
        self.assertEqual(self.endpoint.requests[0]["messages"][1]["content"], rule_version["content"])
        self.assertEqual(rev["parent_id"], rule_version["id"])
        self.assertEqual(rev["content"], CLEAN)

    def test_no_rules_revision_means_the_raw_transcript_goes_to_the_model(self):
        self.settings()
        sid = self.asr_session("明天开会")
        revs = self.store.session_detail(sid)["revisions"]
        self.assertEqual([r["source"] for r in revs], ["asr"])
        rev = llm_postprocess.run(self.store, sid)
        self.assertEqual(self.endpoint.requests[0]["messages"][1]["content"], "明天开会")
        self.assertEqual(rev["parent_id"], revs[0]["id"])

    def test_server_that_cannot_start_writes_nothing(self):
        self.settings(endpoint="http://127.0.0.1:1/v1/chat/completions", server_exe=str(self.root / "no-llama.exe"),
                      model_path=str(self.root / "no.gguf"))
        sid = self.asr_session("嗯 我用 cloud code 写代码")
        before = len(self.store.session_detail(sid)["revisions"])
        with self.assertRaises(InputError) as caught:
            llm_postprocess.run(self.store, sid)
        self.assertEqual(caught.exception.status, 502)
        self.assertEqual(len(self.store.session_detail(sid)["revisions"]), before)

    def test_server_is_started_once_on_loopback_cpu_and_stopped_with_the_desk(self):
        exe, model = self.root / "llama-server.exe", self.root / "m.gguf"
        exe.write_bytes(b"x")
        model.write_bytes(b"x")
        self.settings(endpoint="http://127.0.0.1:18777/v1/chat/completions", server_exe=str(exe), model_path=str(model))
        settings = llm_postprocess.load_settings(self.root)
        proc = types.SimpleNamespace(pid=4242, poll=lambda: None, killed=False)
        proc.kill = lambda: setattr(proc, "killed", True)
        up = iter([False, False, True, True])
        with patch.object(llm_postprocess.subprocess, "Popen", return_value=proc) as popen, \
                patch.object(llm_postprocess, "_healthy", side_effect=lambda _e: next(up)), patch.object(llm_postprocess.time, "sleep"):
            llm_postprocess.ensure_server(self.root, settings)
            args = popen.call_args[0][0]
            self.assertEqual(args[args.index("-ngl") + 1], "0")
            self.assertEqual(args[args.index("--host") + 1], "127.0.0.1")
            self.assertEqual(args[args.index("--port") + 1], "18777")
            self.assertEqual(json.loads((self.root / llm_postprocess.SERVER_PID_FILE).read_text(encoding="utf-8"))["pid"], 4242)
            llm_postprocess.stop_server()      # as atexit calls it: no root argument
        self.assertTrue(proc.killed)
        self.assertFalse((self.root / llm_postprocess.SERVER_PID_FILE).exists())

    def test_non_loopback_endpoint_is_never_started(self):
        exe, model = self.root / "llama-server.exe", self.root / "m.gguf"
        exe.write_bytes(b"x")
        model.write_bytes(b"x")
        self.settings(endpoint="http://192.168.1.5:8081/v1/chat/completions", server_exe=str(exe), model_path=str(model))
        with patch.object(llm_postprocess.subprocess, "Popen") as popen, patch.object(llm_postprocess, "_healthy", return_value=False):
            with self.assertRaises(llm_postprocess.LLMError):
                llm_postprocess.ensure_server(self.root, llm_postprocess.load_settings(self.root))
        popen.assert_not_called()


class ServerTests(Base, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        Base.setUp(self)
        self.client = None

    async def asyncTearDown(self):
        if self.client:
            await self.client.close()
        Base.tearDown(self)

    def setUp(self):  # IsolatedAsyncioTestCase calls setUp too; asyncSetUp does the real work
        pass

    def tearDown(self):
        pass

    async def start(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.client = TestClient(TestServer(create_app(self.root, expected_port=port, dashboard_root=self.root),
                                            host="127.0.0.1", port=port))
        await self.client.start_server()
        html = await (await self.client.get("/")).text()
        token = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
        self.headers = {"Origin": f"http://127.0.0.1:{port}", "Cookie": f"{CSRF_COOKIE}={token}", "X-CSRF-Token": token}

    async def test_startup_calls_orphan_cleanup_for_its_data_directory(self):
        with patch.object(llm_postprocess, "cleanup_orphan_server") as cleanup:
            await self.start()
        cleanup.assert_called_once_with(self.root)

    async def test_button_endpoint_success_and_failure(self):
        self.settings()
        sid = self.transcript()
        await self.start()
        ok = await self.client.post(f"/api/sessions/{sid}/llm-postprocess", json={}, headers=self.headers)
        self.assertEqual(ok.status, 201)
        self.assertEqual((await ok.json())["revision"]["content"], CLEAN)
        self.prompt.unlink()
        bad = await self.client.post(f"/api/sessions/{sid}/llm-postprocess", json={}, headers=self.headers)
        self.assertEqual(bad.status, 502)
        self.assertIn("提示词文件不存在", (await bad.json())["error"])
        status = await (await self.client.get("/api/llm-postprocess")).json()
        self.assertIn(sid, status["errors"])
        detail = await (await self.client.get(f"/api/sessions/{sid}")).json()
        self.assertEqual(len(detail["session"]["revisions"]), 2)

    async def test_three_length_limited_pieces_split_and_persist_polished_results(self):
        self.settings()
        pieces = [f"第{i}段：" + "甲" * 300 + "，" + "乙" * 300 + "，"
                  + "丙" * 300 + "，" + "丁" * 300 + "。" for i in range(1, 4)]
        text = "".join(pieces)
        self.assertEqual(llm_postprocess.split_text(text), pieces)
        halves = [llm_postprocess._split_piece_at_boundary(piece) for piece in pieces]
        self.assertTrue(all(part is not None for part in halves))
        replies = []
        expected_parts = []
        for index, part in enumerate(halves, 1):
            replies.extend([f"截断的第{index}段", f"第{index}段左侧润色。", f"第{index}段右侧润色。"])
            expected_parts.extend([f"第{index}段左侧润色。", f"第{index}段右侧润色。"])
        self.endpoint.replies = iter(replies)
        self.endpoint.finish_reasons = iter(["length", "stop", "stop"] * 3)
        sid = self.store.create_text_session("t", text)[0]["id"]
        before = self.store.session_detail(sid)["revisions"]
        await self.start()
        rev = (await self._post(f"/api/sessions/{sid}/llm-postprocess", {}))["revision"]
        self.assertEqual((rev["content"], rev["parent_id"], rev["source"]),
                         ("".join(expected_parts), before[-1]["id"], "llm_postprocess"))
        # Reopen SQLite and fetch the page's detail API: warnings must survive a fresh read.
        reopened = InputStore(self.root)
        self.assertEqual(reopened.get_revision(rev["id"]), rev)
        after = reopened.session_detail(sid)["revisions"]
        self.assertEqual(after[:-1], before)
        self.assertEqual(len(after), len(before) + 1)
        shown = await self._revisions(sid)
        self.assertEqual(shown[rev["id"]], after[-1])
        self.assertEqual(shown[rev["id"]]["content"], "".join(expected_parts))
        self.assertEqual(shown[rev["id"]]["data"]["polish_unpolished_sentences"], [])
        self.assertEqual(shown[rev["id"]]["data"]["polish_repeat_count"], 0)
        self.assertEqual(shown[rev["id"]]["data"]["polish_piece_count"], 3)
        app_js = await (await self.client.get("/static/app.js")).text()
        self.assertIn("模型重试 2 次仍复读，已保留原文", app_js)
        self.assertIn("${sentence}", app_js)
        status = await (await self.client.get("/api/llm-postprocess")).json()
        self.assertNotIn(sid, status["errors"])
        self.assertEqual(status["running"], [])
        requests = [r["messages"][-1]["content"] for r in self.endpoint.requests]
        self.assertEqual(len(requests), 9)
        self.assertEqual([requests[index * 3] for index in range(3)], pieces)

    async def test_repeated_sentence_is_persisted_and_page_marks_that_sentence(self):
        self.settings()
        sentence = "模型仍复读时保留并标记的句子。"
        self.endpoint.replies = iter(["同一段复读输出。" * 10] * 3)
        self.endpoint.finish_reasons = iter(["stop"] * 3)
        sid = self.store.create_text_session("t", sentence)[0]["id"]
        await self.start()

        rev = (await self._post(f"/api/sessions/{sid}/llm-postprocess", {}))["revision"]
        self.assertEqual(rev["content"], sentence)
        self.assertEqual(rev["data"]["polish_unpolished_sentences"], [sentence])
        shown = await self._revisions(sid)
        self.assertEqual(shown[rev["id"]]["data"]["polish_unpolished_sentence_count"], 1)
        app_js = await (await self.client.get("/static/app.js")).text()
        self.assertIn("模型重试 2 次仍复读，已保留原文", app_js)
        self.assertIn("${sentence}", app_js)

    async def test_only_one_of_many_sentences_keeps_repeating_the_others_are_still_polished(self):
        self.settings()
        first, second, third = "这是第一句话。", "这是第二句话。", "这是第三句话。"
        loop = "模型重复输出的整段内容。" * 8
        # whole piece repeats 3x -> split; left half polishes; right half repeats 3x -> split; second polishes; third repeats 3x
        self.endpoint.replies = iter([loop] * 3 + ["第一句润色。"] + [loop] * 3 + ["第二句润色。"] + [loop] * 3)
        self.endpoint.finish_reasons = iter(["stop"] * 11)
        sid = self.store.create_text_session("t", first + second + third)[0]["id"]
        await self.start()

        rev = (await self._post(f"/api/sessions/{sid}/llm-postprocess", {}))["revision"]

        self.assertEqual(rev["content"], "第一句润色。第二句润色。" + third)
        self.assertEqual(rev["data"]["polish_unpolished_sentences"], [third])  # the page lists exactly this array
        self.assertEqual(rev["data"]["polish_unpolished_sentence_count"], 1)
        self.assertEqual(rev["data"]["polish_retry_recovered"], 0)
        sent = [body["messages"][-1]["content"] for body in self.endpoint.requests]
        self.assertEqual(sent.count(first), 1)
        self.assertEqual(sent.count(second), 1)
        self.assertEqual(sent.count(third), 3)
        self.assertEqual([body.get("repeat_penalty") for body in self.endpoint.requests[-3:]], [None, 1.1, 1.2])
    async def test_timeout_or_bad_format_after_length_split_persists_no_partial_version(self):
        await self.start()
        pieces = [f"第{i}段" + "样" * 1196 + "。" for i in range(1, 4)]
        text = "".join(pieces)
        self.assertEqual(llm_postprocess.split_text(text), pieces)
        for failure, delay, payload, message in (
            ("timeout", 1.5, {"choices": [{"message": {"content": "第二段润色完。"}, "finish_reason": "stop"}]},
             "本地模型没有应答（超时或端点未启动）；没有写入新版本。"),
            ("bad-format", 0, {"choices": []}, "本地模型返回的格式不对；没有写入新版本。"),
        ):
            with self.subTest(failure=failure):
                self.settings(timeout_s=0.3)
                self.endpoint.requests.clear()
                self.endpoint.delays = iter([0, delay])
                self.endpoint.payloads = iter([
                    {"choices": [{"message": {"content": "截断输出不能写入"}, "finish_reason": "length"}]},
                    payload,
                ])
                sid = self.store.create_text_session("t", text)[0]["id"]
                before = self.store.session_detail(sid)["revisions"]
                result = await self._post(f"/api/sessions/{sid}/llm-postprocess", {}, status=502)
                self.assertEqual(result["error"], message)
                self.assertEqual(InputStore(self.root).session_detail(sid)["revisions"], before)
                self.assertEqual(list((await self._revisions(sid)).values()), before)
                status = await (await self.client.get("/api/llm-postprocess")).json()
                self.assertEqual(status["errors"][sid], message)  # the page's polling error hint
                self.assertEqual(status["running"], [])
                requests = [r["messages"][-1]["content"] for r in self.endpoint.requests]
                self.assertEqual(requests[0], pieces[0])
                self.assertEqual(requests[1], pieces[0][:len(pieces[0]) // 2])
                # Let the timed-out handler finish before replacing its scripted response iterator.
                if delay:
                    await asyncio.sleep(delay)

    async def _post(self, path, body, status=201):
        resp = await self.client.post(path, json=body, headers=self.headers)
        self.assertEqual(resp.status, status, await resp.text())
        return await resp.json()

    async def _revisions(self, sid):
        detail = await (await self.client.get(f"/api/sessions/{sid}")).json()
        return {r["id"]: r for r in detail["session"]["revisions"]}

    def _audio_with_asr(self):
        session, asr = BackendTests.verifiable_session(self)
        return session, asr

    async def test_http_polish_with_revision_id_polishes_that_draft(self):
        self.settings()
        session, asr = self._audio_with_asr()
        sid = session["id"]
        await self.start()
        draft = (await self._post(f"/api/sessions/{sid}/corrections",
                                  {"parent_revision_id": asr["id"], "text": "我手改过的稿子", "correction_type": "draft"}))["revision"]
        rev = (await self._post(f"/api/sessions/{sid}/llm-postprocess", {"revision_id": draft["id"]}))["revision"]
        self.assertEqual(self.endpoint.requests[0]["messages"][1]["content"], "我手改过的稿子")
        self.assertEqual(rev["parent_id"], draft["id"])
        await self._post(f"/api/sessions/{sid}/llm-postprocess", {})        # old caller: no body id, still the transcript
        self.assertEqual(self.endpoint.requests[1]["messages"][1]["content"], RAW)
        await self._post(f"/api/sessions/{sid}/llm-postprocess", {"revision_id": "no-such-revision"}, status=409)
        self.assertEqual(len(self.endpoint.requests), 2)

    async def test_http_polish_after_confirm_save_keeps_old_check_and_new_draft_is_unverified(self):
        self.settings()
        session, asr = self._audio_with_asr()
        sid, sha = session["id"], session["original_sha256"]
        await self.start()
        await self._post(f"/api/sessions/{sid}/verifications", {"revision_id": asr["id"], "original_sha256": sha})
        # 确认保存 then 润色 straight away, nothing edited since the confirmation
        first = (await self._post(f"/api/sessions/{sid}/llm-postprocess", {"revision_id": asr["id"]}))["revision"]
        # a few characters edited by hand on the confirmed text, then 润色 again
        tiny = (await self._post(f"/api/sessions/{sid}/corrections",
                                 {"parent_revision_id": asr["id"], "text": RAW.replace("三点", "四点"), "correction_type": "draft"}))["revision"]
        second = (await self._post(f"/api/sessions/{sid}/llm-postprocess", {"revision_id": tiny["id"]}))["revision"]
        self.assertEqual(self.endpoint.requests[1]["messages"][1]["content"], RAW.replace("三点", "四点"))
        shown = await self._revisions(sid)
        self.assertTrue(shown[asr["id"]]["verification"]["current"])     # old check stands
        for rev in (first, second):
            self.assertIsNone(shown[rev["id"]]["verification"])          # new machine drafts are unverified
            self.assertIs(shown[rev["id"]]["data"]["human_verified"], False)
        self.assertEqual([r["text"] for r in self.store.list_verified_training_rows()], [RAW])

    async def test_http_autosave_then_immediate_polish_uses_the_just_saved_version(self):
        self.settings()
        session, asr = self._audio_with_asr()
        sid = session["id"]
        await self.start()
        # what the 润色 button does: flushEditor() autosaves a draft, then posts the id it returned
        saved = None
        parent = asr["id"]
        for text in ("改一版", "改一版再加几个字"):          # two quick edits, polish right after the second save
            saved = (await self._post(f"/api/sessions/{sid}/corrections",
                                      {"parent_revision_id": parent, "text": text, "correction_type": "draft"}))["revision"]
            parent = saved["id"]
        rev = (await self._post(f"/api/sessions/{sid}/llm-postprocess", {"revision_id": saved["id"]}))["revision"]
        self.assertEqual(self.endpoint.requests[0]["messages"][1]["content"], "改一版再加几个字")
        self.assertEqual(rev["parent_id"], saved["id"])

    def test_page_posts_the_revision_flushEditor_returned(self):
        js = (Path(__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
        handler = js[js.index('byId("llm-run")'):]
        handler = handler[:handler.index("});")]
        self.assertLess(handler.index("flushEditor()"), handler.index("revision_id"))
        self.assertIn("shownId || baseRevisionId", handler)

    def test_opening_an_older_version_pins_it_against_the_polling_refresh(self):
        # 11-05: the 3 s refreshSelectedSession jumped back to the newest (polished) version after "在编辑区打开".
        js = (Path(__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
        handler = js[js.index('rev.kind === "instruction" ? "查看指令"'):]
        handler = handler[:handler.index("head.append(use)")]
        self.assertLess(handler.index("pinnedLatestId = revisionForEditing(detail.revisions)"), handler.index("showRevision(detail, rev, true)"))
        refresh = js[js.index("async function refreshSelectedSession"):js.index("async function renderArchiveDetail")]
        self.assertIn("nowLatest.id !== pinnedLatestId", refresh)

    async def test_page_has_nonblocking_coverage_hint_above_the_editor(self):
        await self.start()
        html = await (await self.client.get("/")).text()
        js = await (await self.client.get("/static/app.js")).text()
        self.assertIn('id="llm-coverage-warning" class="live-status error" role="status" aria-live="polite" hidden', html)
        self.assertLess(html.index('id="llm-coverage-warning"'), html.index('id="result-editor"'))
        helper = js[js.index("function polishCoverageWarning"):js.index("function detailSignature")]
        self.assertIn("ratio < 0.6", helper)
        self.assertIn("polish_tail_found === false", helper)
        self.assertIn("润色稿可能漏了内容", helper)
        self.assertIn("没找到原稿最后一句", helper)
        self.assertIn("请对照原始转写", helper)
        self.assertNotIn("disabled", helper)
        self.assertIn("const warning = polishCoverageWarning(detail, rev)", js)
        self.assertIn('byId("llm-coverage-warning").hidden = !warning', js)

    async def test_checkbox_persists_and_defaults_off(self):
        self.settings()
        await self.start()
        self.assertFalse((await (await self.client.get("/api/llm-postprocess")).json())["auto"])
        on = await self.client.post("/api/llm-postprocess/auto", json={"auto": True}, headers=self.headers)
        self.assertTrue((await on.json())["auto"])
        self.assertTrue(llm_postprocess.load_settings(self.root)["auto"])
        off = await self.client.post("/api/llm-postprocess/auto", json={"auto": False}, headers=self.headers)
        self.assertFalse((await off.json())["auto"])
        self.assertFalse(llm_postprocess.load_settings(self.root)["auto"])

    async def _new_transcript_flow(self, auto: bool) -> list[dict]:
        self.settings(auto=auto)
        session = self.store.create_audio_session("audio_capture", "录音", "audio/webm")
        data = b"complete original recording"
        self.store.put_chunk(session["id"], 0, data, hashlib.sha256(data).hexdigest())
        self.store.finalize_audio(session["id"], 1, len(data))  # status queued: the server transcribes it at startup
        fake = types.ModuleType("transcribe")
        fake.transcribe = lambda path: {"text": RAW}
        with patch.dict(sys.modules, {f"{__package__}.transcribe": fake}):
            await self.start()
            for _ in range(100):
                detail = self.store.session_detail(session["id"])
                if detail["status"] == "transcribed" and (not auto or len(detail["revisions"]) > 1):
                    break
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.3)
        return self.store.session_detail(session["id"])["revisions"]

    async def test_checkbox_on_queues_postprocess_for_new_transcript(self):
        revisions = await self._new_transcript_flow(True)
        self.assertEqual([r["source"] for r in revisions], ["asr", "llm_postprocess"])
        self.assertEqual(revisions[1]["content"], CLEAN)
        self.assertEqual(len(self.endpoint.requests), 1)

    async def test_checkbox_off_leaves_new_transcript_alone(self):
        revisions = await self._new_transcript_flow(False)
        self.assertEqual([r["source"] for r in revisions], ["asr"])
        self.assertEqual(self.endpoint.requests, [])


if __name__ == "__main__":
    unittest.main()
