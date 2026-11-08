"""Synthetic lock files and stubbed Windows queries; no process is stopped."""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import restart_check as check


class RestartCheck(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for target, name, value in ((check.tick, "DATA", self.root),
                                    (check.tick, "pid_alive", lambda pid: pid in {101, 202, 303}),
                                    (check, "supervisor_pids", lambda: [42])):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def lock(self, path, pid):
        path.write_text(str(pid), encoding="utf-8")
        os.utime(path, (1793739600, 1793739600))
        return path

    def snapshot(self):
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir()}

    def run_check(self):
        before = self.snapshot()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = check.main()
        self.assertEqual(self.snapshot(), before, "check must not write, acquire or delete locks")
        return code, out.getvalue()

    def test_no_locks_and_unique_supervisor_allows_restart(self):
        code, output = self.run_check()
        self.assertEqual(code, 0)
        self.assertIn("活动 lane 锁：0\n活动 Codex 锁：0", output)
        self.assertIn("supervisor pythonw PID：42", output)
        self.assertEqual(output.splitlines()[-1], "可以重启：是")

    def test_live_lanes_and_codex_list_identity_and_block(self):
        path = self.lock(check.tick.lane_lock("coordinator", "incore"), 101)
        self.lock(check.tick.lane_lock("work", "technical-asset-handover"), 202)
        self.lock(check.tick.lane_lock("life"), 303)
        self.lock(check.tick.codex_lock(0), 101)
        self.lock(check.tick.codex_lock(check.tick.CODEX_LOCKS - 1), 202)
        code, output = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("活动 lane 锁：3", output)
        self.assertIn("活动 Codex 锁：2", output)
        self.assertIn("角色 work | 项目 technical-asset-handover", output)
        self.assertIn("角色 life | 项目 —", output)
        self.assertIn("codex.lock | 角色 coordinator | 项目 incore", output)
        stamp = datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
        self.assertIn(f"开始 {stamp} | PID 101", output)
        self.assertTrue(output.splitlines()[-1].startswith("可以重启：否，原因"))

    def test_stale_locks_are_ignored_and_preserved(self):
        self.lock(check.tick.lane_lock("work", "umba"), 999)
        self.lock(check.tick.codex_lock(), 999)
        code, output = self.run_check()
        self.assertEqual(code, 0)
        self.assertNotIn("PID 999", output)
        self.assertEqual(output.splitlines()[-1], "可以重启：是")

    def test_unmatched_codex_still_blocks(self):
        self.lock(check.tick.codex_lock(), 101)
        code, output = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("codex.lock | 角色 未知 | 项目 未知", output)
        self.assertIn("有 1 个活动 Codex 锁", output.splitlines()[-1])

    def test_missing_or_ambiguous_supervisor_blocks(self):
        for pids in ([], [42, 43]):
            with self.subTest(pids=pids), patch.object(check, "supervisor_pids", return_value=pids):
                code, output = self.run_check()
                self.assertEqual(code, 1)
                self.assertIn(f"实际找到 {len(pids)} 个", output.splitlines()[-1])

    def test_query_errors_block_and_keep_final_verdict(self):
        for target, name in ((check.tick, "pid_alive"), (check, "supervisor_pids")):
            for error in (OSError("unavailable"), subprocess.TimeoutExpired("query", 20)):
                with self.subTest(name=name, error=error), patch.object(target, name, side_effect=error):
                    self.lock(check.tick.lane_lock("work", "umba"), 101)
                    code, output = self.run_check()
                    self.assertEqual(code, 1)
                    self.assertIn("查询失败", output.splitlines()[-1])

    def test_missing_lock_directory_blocks(self):
        with patch.object(check.tick, "DATA", self.root / "missing"):
            code, output = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("活动锁查询失败", output.splitlines()[-1])


class SupervisorQuery(unittest.TestCase):
    def query(self, processes):
        with (patch.object(check.sys, "platform", "win32"),
              patch.object(check.health_check, "ps_json", return_value={"processes": processes}) as query):
            pids = check.supervisor_pids()
        self.assertIn("Get-CimInstance Win32_Process", query.call_args.args[0])
        self.assertIn("Name='pythonw.exe'", query.call_args.args[0])
        self.assertEqual(query.call_args.kwargs, {"timeout": 20})
        return pids

    def test_only_pythonw_with_tick_path_at_end_is_selected(self):
        command = r'"C:\Python\pythonw.exe" "C:\工作目录\incore\control\tick.py"'
        processes = [{"ProcessId": 42, "Name": "pythonw.exe", "CommandLine": command}]
        for pid, suffix in enumerate((" lane work", " owner", " report", " codex work umba", " supervise"), 100):
            processes.append({"ProcessId": pid, "Name": "pythonw.exe", "CommandLine": command + suffix})
        processes.extend([
            {"ProcessId": 200, "Name": "python.exe", "CommandLine": command},
            {"ProcessId": 201, "Name": "pythonw.exe", "CommandLine": command.replace("tick.py", "other.py")},
            {"ProcessId": 202, "Name": "PYTHONW.EXE", "CommandLine": r'pythonw.exe C:\x\CONTROL\TICK.PY  '},
        ])
        self.assertEqual(self.query(processes), [42, 202])

    def test_unreadable_or_malformed_response_is_not_an_empty_success(self):
        for raw in (None, {}, {"processes": None}, {"processes": [{"ProcessId": 42, "Name": "pythonw.exe", "CommandLine": ""}]}):
            with (self.subTest(raw=raw), patch.object(check.sys, "platform", "win32"),
                  patch.object(check.health_check, "ps_json", return_value=raw)):
                with self.assertRaises(ValueError):
                    check.supervisor_pids()

    @unittest.skipIf(sys.platform == "win32", "WSL/Linux guard")
    def test_wsl_does_not_attempt_windows_programs(self):
        with patch.object(check.health_check, "ps_json") as query:
            with self.assertRaises(OSError):
                check.supervisor_pids()
        query.assert_not_called()


if __name__ == "__main__":
    unittest.main()
