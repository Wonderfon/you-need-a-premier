import io
import json
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llama_wsl as w


class LlamaWslTests(unittest.TestCase):
    def setUp(self):
        # Keep temporary files in this checkout on both WSL and Windows.
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent.parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.server = self.root / "llama-server"
        self.server.write_text("stub", encoding="utf-8")
        self.model = self.root / "model.gguf"
        self.model.write_bytes(b"model")
        self.libs = [self.root / name for name in ("llama", "cuda", "extra-libs")]
        config = {"server": self.server, "model": self.model,
                  "library_dirs": self.libs, "state_dir": self.root}
        self.config = patch.dict(w.CONFIG, config)
        self.config.start()
        self.addCleanup(self.config.stop)
        self.port = 8199
        self.identity = {"pid": 12345, "start_ticks": "123", "boot_id": "boot",
                         "exe": str(self.server.resolve())}

    def save_pid(self, identity=None):
        w.pid_path(self.port).write_text(json.dumps(identity or self.identity), encoding="utf-8")

    def start_stubs(self, free=9000):
        run = self.enterContext(patch.object(w.subprocess, "run", return_value=
                                             Mock(stdout=f"{free}, 1000\n")))
        popen = self.enterContext(patch.object(w.subprocess, "Popen"))
        popen.return_value.pid = self.identity["pid"]
        popen.return_value.poll.return_value = None
        self.enterContext(patch.object(w, "process_identity", return_value=self.identity))
        self.enterContext(patch.object(w.socket, "socket"))
        health = self.enterContext(patch.object(w, "urlopen"))
        health.return_value.__enter__.return_value = io.BytesIO(b'{"status":"ok"}')
        return run, popen, health

    def test_start_uses_config_libraries_gpu_parameters_and_pid(self):
        run, popen, health = self.start_stubs(free=513)
        with patch.dict(w.os.environ, {"LD_LIBRARY_PATH": "/existing/lib"}):
            w.start(self.port)
        command = popen.call_args.args[0]
        options = dict(zip(command[1::2], command[2::2]))
        self.assertEqual(options["-m"], str(self.model))
        self.assertEqual(options["-ngl"], "99")
        self.assertEqual(options["-c"], "2048")
        self.assertEqual(options["-np"], "1")
        self.assertEqual(options["--host"], "127.0.0.1")
        self.assertEqual(options["--device"], "CUDA0")
        self.assertEqual(options["--port"], str(self.port))
        env = popen.call_args.kwargs["env"]
        self.assertEqual(env["LD_LIBRARY_PATH"], ":".join(map(str, self.libs)) + ":/existing/lib")
        self.assertEqual(env["CUDA_VISIBLE_DEVICES"], "0")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertIn("--query-gpu=memory.free,memory.used", run.call_args.args[0])
        self.assertEqual(json.loads(w.pid_path(self.port).read_text(encoding="utf-8")), self.identity)
        self.assertIn("/health", health.call_args.args[0])

    def test_insufficient_vram_refuses_before_spawning(self):
        self.assertEqual(w.CONFIG["vram_margin_mib"], 512)
        _, popen, _ = self.start_stubs(free=512)
        with self.assertRaisesRegex(RuntimeError, r"显存不足.*模型大小 \+ 512 MiB = 513 MiB"):
            w.start(self.port)
        popen.assert_not_called()
        self.assertFalse(w.pid_path(self.port).exists())

    def test_unknown_or_invalid_vram_never_starts(self):
        run, popen, _ = self.start_stubs()
        for value in ("N/A, 1000", "nan, 1000", "-1, 1000"):
            run.return_value.stdout = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                w.start(self.port)
        run.side_effect = subprocess.TimeoutExpired("nvidia-smi", 10)
        with self.assertRaises(subprocess.TimeoutExpired):
            w.start(self.port)
        popen.assert_not_called()

    def test_occupied_port_refuses_before_spawning_or_querying_gpu(self):
        run, popen, _ = self.start_stubs()
        w.socket.socket.return_value.__enter__.return_value.bind.side_effect = OSError("in use")
        with self.assertRaisesRegex(RuntimeError, "端口.*已占用"):
            w.start(self.port)
        popen.assert_not_called()
        run.assert_not_called()

    def test_existing_pid_file_is_never_overwritten(self):
        run, popen, _ = self.start_stubs()
        self.save_pid()
        with self.assertRaisesRegex(RuntimeError, "已有 PID 文件"):
            w.start(self.port)
        run.assert_not_called()
        popen.assert_not_called()

    def test_health_timeout_cleans_up_only_its_own_child(self):
        _, popen, _ = self.start_stubs()
        with patch.dict(w.CONFIG, health_timeout=0):
            with self.assertRaisesRegex(RuntimeError, "超时"):
                w.start(self.port)
        popen.return_value.terminate.assert_called_once_with()
        popen.return_value.wait.assert_called_once_with(timeout=5)
        self.assertFalse(w.pid_path(self.port).exists())

    def test_early_exit_clears_pid_and_reports_log(self):
        _, popen, _ = self.start_stubs()
        popen.return_value.poll.return_value = 1
        popen.return_value.returncode = 1
        with self.assertRaisesRegex(RuntimeError, "已退出.*日志"):
            w.start(self.port)
        popen.return_value.terminate.assert_not_called()
        self.assertFalse(w.pid_path(self.port).exists())

    def test_loading_health_is_retried_until_ready(self):
        _, popen, health = self.start_stubs()
        health.side_effect = [HTTPError("http://127.0.0.1/health", 503, "Loading", {}, None),
                              io.BytesIO(b'{"status":"ok"}')]
        with patch.object(w.time, "sleep"):
            w.start(self.port)
        self.assertEqual(health.call_count, 2)
        popen.return_value.terminate.assert_not_called()

    def stop_stubs(self):
        self.save_pid()
        opened = self.enterContext(patch.object(w.os, "pidfd_open", return_value=42, create=True))
        sent = self.enterContext(patch.object(w.signal, "pidfd_send_signal", create=True))
        closed = self.enterContext(patch.object(w.os, "close"))
        selected = self.enterContext(patch.object(w.select, "select", return_value=([42], [], [])))
        identity = self.enterContext(patch.object(w, "process_identity", return_value=self.identity))
        return opened, sent, closed, selected, identity

    def test_stop_without_pid_never_inspects_or_signals_any_process(self):
        with patch.object(w.os, "pidfd_open", create=True) as opened, \
                patch.object(w.signal, "pidfd_send_signal", create=True) as sent, \
                patch.object(w, "process_identity") as identity:
            w.stop(self.port)
        opened.assert_not_called()
        sent.assert_not_called()
        identity.assert_not_called()

    def test_stop_signals_only_the_process_recorded_in_pid_file(self):
        opened, sent, closed, _, identity = self.stop_stubs()
        w.stop(self.port)
        opened.assert_called_once_with(12345)
        identity.assert_called_once_with(12345)
        sent.assert_called_once_with(42, signal.SIGTERM)
        closed.assert_called_once_with(42)
        self.assertFalse(w.pid_path(self.port).exists())

    def test_stale_reused_pid_or_different_executable_never_receives_signal(self):
        _, sent, _, _, identity = self.stop_stubs()
        for changed in ({"start_ticks": "124"}, {"boot_id": "another-boot"},
                        {"exe": str(self.root / "other-process")}):
            identity.return_value = dict(self.identity, **changed)
            with self.subTest(changed=changed), self.assertRaisesRegex(RuntimeError, "身份不符"):
                w.stop(self.port)
        sent.assert_not_called()
        self.assertTrue(w.pid_path(self.port).exists())

    def test_nonexistent_pid_only_clears_pid_file(self):
        opened, sent, _, _, _ = self.stop_stubs()
        opened.side_effect = ProcessLookupError()
        w.stop(self.port)
        sent.assert_not_called()
        self.assertFalse(w.pid_path(self.port).exists())

    def test_invalid_pid_never_opens_or_signals_process(self):
        opened, sent, _, _, _ = self.stop_stubs()
        for pid in (0, -1, 1, "12345"):
            self.save_pid(dict(self.identity, pid=pid))
            with self.subTest(pid=pid), self.assertRaises(ValueError):
                w.stop(self.port)
        opened.assert_not_called()
        sent.assert_not_called()

    def test_bench_counts_streamed_characters_and_times_first_content(self):
        text = "秋夜散步令人心静" * 25
        self.assertEqual(len(text), 200)
        events = [{"content": ""}, {"content": text[:4]}, {"content": text[4:]},
                  {"stop": True, "timings": {"predicted_per_second": 44}}]
        stream = b"".join(b"data: " + json.dumps(event).encode("utf-8") + b"\n\n" for event in events)
        with patch.object(w, "gpu_memory", return_value={"free_mib": 1000, "used_mib": 7000}), \
                patch.object(w, "urlopen") as response, \
                patch.object(w.time, "perf_counter", side_effect=[0, 0.1, 0.2, 0.5, 1, 2, 3, 4, 5, 6]):
            response.return_value.__enter__.return_value = io.BytesIO(stream)
            result = w.bench(self.port)
        self.assertEqual(result["characters"], 200)
        self.assertEqual(result["first_char_seconds"], 1)
        self.assertEqual(result["total_seconds"], 6)
        self.assertEqual(result["chars_per_second"], 33.33)
        body = json.loads(response.call_args.args[0].data)
        self.assertTrue(body["stream"])
        self.assertFalse(body["cache_prompt"])
        self.assertEqual(body["grammar"].count("char") - 1, 200)
        self.assertEqual(result["llama_tokens_per_second"], 44)

    def test_bench_rejects_truncated_output(self):
        with patch.object(w, "gpu_memory"), patch.object(w, "urlopen") as response:
            response.return_value.__enter__.return_value = io.BytesIO(b'data: {"content":"short"}\n\n')
            with self.assertRaisesRegex(RuntimeError, "未完成 200 字"):
                w.bench(self.port)

    @unittest.skipUnless(sys.platform == "linux" and hasattr(signal, "pidfd_send_signal"),
                         "Linux /proc and pidfd required")
    def test_real_pidfd_stops_our_child_and_leaves_other_child_alive(self):
        children = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
                    for _ in range(2)]
        try:
            identity = w.process_identity(children[0].pid)
            self.assertIsNotNone(identity)
            self.save_pid(identity)
            with patch.dict(w.CONFIG, server=Path(sys.executable).resolve()):
                w.stop(self.port)
            self.assertEqual(children[0].wait(timeout=5), -signal.SIGTERM)
            self.assertIsNone(children[1].poll())
            self.assertFalse(w.pid_path(self.port).exists())
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=5)

    @unittest.skipUnless(sys.platform == "linux", "Linux flock required")
    def test_real_port_lock_rejects_concurrent_lifecycle_command(self):
        with w.port_lock(self.port):
            with self.assertRaisesRegex(RuntimeError, "另一个起停命令"):
                with w.port_lock(self.port):
                    self.fail("second lock unexpectedly succeeded")

    def test_real_occupied_socket_refuses_start(self):
        with socket.socket() as listener, patch.object(w.subprocess, "Popen") as popen:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            with self.assertRaisesRegex(RuntimeError, "端口.*已占用"):
                w.start(listener.getsockname()[1])
            popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
