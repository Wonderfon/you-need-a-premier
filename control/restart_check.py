"""Read-only lock/process snapshot before a coordinator supervisor restart."""
import re
import subprocess
import sys
from datetime import datetime

import health_check
import tick


def active_locks() -> list[dict]:
    if not tick.DATA.is_dir():
        raise OSError(f"锁目录不存在或不可读：{tick.DATA}")
    rows = []
    paths = []
    for path in sorted(tick.DATA.glob("lane-*.lock")):
        role, _, project = path.stem.removeprefix("lane-").partition("-")
        paths.append((tick.lane_lock(role, project or None), "lane", role, project or "—"))
    paths.extend((tick.codex_lock(i), "Codex", "未知", "未知") for i in range(tick.CODEX_LOCKS))
    for path, kind, role, project in paths:
        if not tick.lock_live(path):
            continue
        rows.append({"kind": kind, "file": path.name, "role": role, "project": project,
                     "pid": int(path.read_text(encoding="utf-8").strip()),
                     "started": datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")})
    for row in rows:
        if row["kind"] == "Codex":
            lanes = [r for r in rows if r["kind"] == "lane" and r["pid"] == row["pid"]]
            if lanes:
                row["role"] = ",".join(r["role"] for r in lanes)
                row["project"] = ",".join(r["project"] for r in lanes)
    return rows


def supervisor_pids() -> list[int]:
    if sys.platform != "win32":
        raise OSError("Windows 进程查询不可用，请在 Windows 运行")
    # Reuse the repository's UTF-8, windowless PowerShell/CIM query helper.
    raw = health_check.ps_json(
        "$ErrorActionPreference='Stop'; "
        "@{processes=@(Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" -ErrorAction Stop | "
        "Select-Object ProcessId,Name,CommandLine)} | ConvertTo-Json -Depth 3 -Compress", timeout=20)
    if not isinstance(raw, dict) or not isinstance(raw.get("processes"), list):
        raise ValueError("进程查询没有返回完整列表")
    pids = set()
    for process in raw["processes"]:
        command = process["CommandLine"]
        if not command:
            raise ValueError(f"pythonw PID {process['ProcessId']} 的命令行不可读")
        if (process["Name"].casefold() == "pythonw.exe"
                and re.search(r'\\control\\tick\.py"?\s*$', command, re.I)):
            pid = int(process["ProcessId"])
            if pid <= 0:
                raise ValueError("supervisor PID 无效")
            pids.add(pid)
    return sorted(pids)


def main() -> int:
    print("只读快照：仅检查活动锁和 supervisor；实际重启仍按 10-26 B 执行。")
    print("开始时间取锁文件修改时间；Codex 的角色、项目按同 PID lane 关联，无法关联时显示未知。")
    reasons = []
    try:
        rows = active_locks()
        for kind in ("lane", "Codex"):
            found = [r for r in rows if r["kind"] == kind]
            print(f"活动 {kind} 锁：{len(found)}")
            for row in found:
                print(f"  {row['file']} | 角色 {row['role']} | 项目 {row['project']} | "
                      f"开始 {row['started']} | PID {row['pid']}")
            if found:
                reasons.append(f"有 {len(found)} 个活动 {kind} 锁")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"活动锁：查询失败（{exc}）")
        reasons.append("活动锁查询失败")
    try:
        pids = supervisor_pids()
        print("待停的 supervisor pythonw PID：" + (", ".join(map(str, pids)) or "未找到"))
        if len(pids) != 1:
            reasons.append(f"supervisor 应唯一，实际找到 {len(pids)} 个")
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError, AttributeError) as exc:
        print(f"supervisor：查询失败（{exc}）")
        reasons.append("supervisor 进程查询失败")
    print("可以重启：否，原因" + "；".join(reasons) if reasons else "可以重启：是")
    return 1 if reasons else 0


if __name__ == "__main__":
    sys.exit(main())
