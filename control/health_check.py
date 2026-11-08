"""Weekly machine health check (read-only, no admin): prints one markdown section, 🔴 on over-threshold items.

Run: python control/health_check.py   (stdlib only; stdout is the section the weekly review pastes into the report)
Only side effect: appends a snapshot to control/data/health-history.json (this script's own state, used for week-over-week).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
import instance_config as ic  # noqa: E402  (this machine's paths and owner identity; defaults = today's values)

# ---- CONFIG ----
INCUBATOR = Path(ic.V["workspace"] + r"\incore")
WT = Path(ic.V["wt_root"])
HISTORY = INCUBATOR / "control" / "data" / "health-history.json"
SAVE_HISTORY = True
HISTORY_KEEP = 12
BASELINE_DAYS = 7              # compare with the snapshot closest to this age ...
BASELINE_TOLERANCE_DAYS = 3    # ... within +/- this many days, else "no baseline"
EVENT_DAYS = 7
SIZE_DIRS = {"control/data": INCUBATOR / "control" / "data", "incore/data": INCUBATOR / "data", "wt": WT}
LXSS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Lxss"
INPUT_PORT = 8765
# role -> PID state file (None: found another way)
PID_FILES = {
    "gateway": (INCUBATOR / "discord" / "data" / "gateway-process.json", "ProcessId"),
    "backup": (INCUBATOR / "data" / "backups" / "watcher-process.json", "ProcessId"),
    "supervisor": (INCUBATOR / "control" / "data" / "tick.lock", None),
}
INPUT_PID_FILE = INCUBATOR / "input" / "data" / "server-process.json"  # only a fallback: it goes stale
# Thresholds (red when reached)
RED_STORAGE_RESETS = 3         # iaStorVD 129 in EVENT_DAYS
RED_PORT_EXHAUSTION = 3        # Tcpip 4231/4266 in EVENT_DAYS
RED_KERNEL_POWER_41 = 1
RED_MEMORY_GROWTH = 0.5        # private memory vs last week
RED_C_FREE_GB = 200
TOP_TCP = 5
TOP_WT_DIRS = 5
RED_WSL_TMP_PCT = 80           # WSL /tmp (16G tmpfs) usage; at 100 % every Codex run fails to start
WEBSITE_HEALTH_URL = "https://www.arbitrosai.com/api/health"  # anything but HTTP 200 is red (S4 of the Arbitros server plan)
WEBSITE_TIMEOUT_S = 15
# ----------------

EVENT_SPECS = {  # label -> (provider, ids or None for all)
    "storage_reset": ("iaStorVD", [129]),
    "port_exhaustion": ("Tcpip", [4231, 4266]),
    "whea": ("Microsoft-Windows-WHEA-Logger", None),
    "kernel_power_41": ("Microsoft-Windows-Kernel-Power", [41]),
}
GB = 1024 ** 3
MB = 1024 ** 2


def ps_json(script: str, timeout: int = 180):
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + script],
                         capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
    return json.loads(out) if out else None


def _as_list(value):
    return [] if value is None else value if isinstance(value, list) else [value]


def collect_events(days: int = EVENT_DAYS) -> dict:
    """label -> {"count", "times": [ISO local, newest first], "by_id": {id: n}}; None when the log can't be read."""
    parts = []
    for label, (provider, ids) in EVENT_SPECS.items():
        idf = f"; Id=@({','.join(map(str, ids))})" if ids else ""
        parts.append(f"$r['{label}']=@(Get-WinEvent -FilterHashtable @{{LogName='System'; ProviderName='{provider}'{idf}; "
                     f"StartTime=$s}} -ErrorAction SilentlyContinue | ForEach-Object {{ @{{t=$_.TimeCreated.ToString('s');i=$_.Id}} }})")
    script = (f"$s=(Get-Date).AddDays(-{days}); $r=@{{}}; " + "; ".join(parts) + "; $r | ConvertTo-Json -Depth 4 -Compress")
    try:
        raw = ps_json(script) or {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {label: None for label in EVENT_SPECS}
    result = {}
    for label in EVENT_SPECS:
        items = sorted(_as_list(raw.get(label)), key=lambda e: e["t"], reverse=True)
        by_id: dict = {}
        for e in items:
            by_id[str(e["i"])] = by_id.get(str(e["i"]), 0) + 1
        result[label] = {"count": len(items), "times": [e["t"] for e in items], "by_id": by_id}
    return result


def dir_size(path: Path) -> int | None:
    if not path.exists():
        return None
    total, stack = 0, [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def wsl_vhdx() -> Path | None:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, LXSS_KEY) as root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                with winreg.OpenKey(root, winreg.EnumKey(root, i)) as sub:
                    try:
                        vhdx = Path(winreg.QueryValueEx(sub, "BasePath")[0].replace("\\\\?\\", "")) / "ext4.vhdx"
                    except OSError:
                        continue
                    if vhdx.exists():
                        return vhdx
    except OSError:
        pass
    return None


def wt_top_dirs(n: int = TOP_WT_DIRS) -> list[dict]:
    """The n biggest direct subdirectories of wt, to see who is growing."""
    try:
        subs = [p for p in WT.iterdir() if p.is_dir() and not p.is_symlink()]
    except OSError:
        return []
    rows = sorted(({"name": p.name, "size": dir_size(p) or 0} for p in subs), key=lambda r: -r["size"])
    return rows[:n]


def parse_df(text: str) -> dict | None:
    """Last line of `df -P`: Filesystem 1024-blocks Used Available Capacity Mounted-on."""
    parts = (text.strip().splitlines() or [""])[-1].split()
    if len(parts) < 6 or not parts[4].endswith("%"):
        return None
    try:
        return {"size": int(parts[1]) * 1024, "used": int(parts[2]) * 1024, "pct": int(parts[4][:-1])}
    except ValueError:
        return None


def wsl_tmp() -> dict | None:
    try:
        done = subprocess.run(["wsl", "-e", "df", "-P", "/tmp"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_df(done.stdout) if done.returncode == 0 else None


def collect_storage() -> dict:
    usage = shutil.disk_usage("C:\\")
    sizes = {name: dir_size(path) for name, path in SIZE_DIRS.items()}
    vhdx = wsl_vhdx()
    sizes["WSL ext4.vhdx"] = vhdx.stat().st_size if vhdx else None
    return {"c_free": usage.free, "c_total": usage.total, "sizes": sizes, "wt_top": wt_top_dirs(), "wsl_tmp": wsl_tmp()}


def _pid_from(path: Path, key: str | None) -> int | None:
    try:
        text = path.read_text(encoding="utf-8-sig").strip()
        return int(json.loads(text)[key]) if key else int(text)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def process_table() -> dict[int, dict]:
    script = ("$now=[datetime]::UtcNow; $p=@(Get-CimInstance Win32_Process | ForEach-Object { @{pid=[int]$_.ProcessId;ppid=[int]$_.ParentProcessId;"
              "name=$_.Name;command=[string]$_.CommandLine;age_s=[int](($now-$_.CreationDate.ToUniversalTime()).TotalSeconds);"
              "priv=[int64]$_.PrivatePageCount;handles=[int]$_.HandleCount} }); "
              "$l=@(Get-NetTCPConnection -State Listen -LocalPort %d -ErrorAction SilentlyContinue | ForEach-Object { [int]$_.OwningProcess }); "
              "$c=@(Get-NetTCPConnection -ErrorAction SilentlyContinue); "
              "$d=@(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | ForEach-Object { @{pid=[int]$_.OwningProcess;address=[string]$_.LocalAddress;port=[int]$_.LocalPort} }); "
              "$t=@($c | Group-Object OwningProcess | ForEach-Object { @{pid=[int]$_.Name;n=$_.Count} }); "
              "$s=@($c | Group-Object State | ForEach-Object { @{state=[string]$_.Name;n=$_.Count} }); "
              "@{procs=$p;listener=$l;tcp=$t;states=$s;debug_listeners=$d} | ConvertTo-Json -Depth 4 -Compress" % INPUT_PORT)
    raw = ps_json(script) or {}
    table = {p["pid"]: p for p in _as_list(raw.get("procs"))}
    table["_listener"] = _as_list(raw.get("listener"))
    table["_tcp"] = _as_list(raw.get("tcp"))
    table["_states"] = _as_list(raw.get("states"))
    table["_debug_listeners"] = _as_list(raw.get("debug_listeners"))
    return table


def headless_browser_alerts(table: dict) -> list[str]:
    alerts = []
    listeners = table.get("_debug_listeners", [])
    for proc in table.values():
        if not isinstance(proc, dict) or str(proc.get("name", "")).casefold() not in {"msedge.exe", "chrome.exe"}:
            continue
        command = str(proc.get("command", ""))
        if not re.search(r"(?:^|\s)--headless(?:[=\s]|$)", command, re.I) or re.search(r"(?:^|\s)--type(?:=|\s)", command, re.I):
            continue
        reasons = []
        try:
            if float(proc.get("age_s", 0)) > 2 * 60 * 60:
                reasons.append("运行超过 2 小时")
        except (TypeError, ValueError):
            pass
        port = re.search(r"(?:^|\s)--remote-debugging-port(?:=|\s+)(\d+)", command, re.I)
        if port and any(listener.get("pid") == proc.get("pid") and listener.get("address") == "0.0.0.0"
                        and (int(port[1]) == 0 or listener.get("port") == int(port[1]))
                        for listener in listeners if isinstance(listener, dict)):
            reasons.append("调试端口监听 0.0.0.0")
        if reasons:
            alerts.append(f"{proc['name']} PID {proc['pid']}：{'、'.join(reasons)}")
    return alerts


def _descendants(table: dict, pid: int) -> list[int]:
    found, frontier = [], [pid]
    while frontier:
        parent = frontier.pop()
        for p in table.values():
            if isinstance(p, dict) and p["ppid"] == parent and p["pid"] != parent and p["pid"] not in found:
                frontier.append(p["pid"])
                if p["name"].lower() != "conhost.exe":
                    found.append(p["pid"])
    return found


def role_pids(table: dict) -> dict[str, list[int]]:
    """role -> PIDs. Input: whoever listens on the input port (the PID file goes stale) + its python launcher; ASR: descendants of the listener."""
    roles: dict[str, list[int]] = {}
    listener = next((p for p in table["_listener"] if p in table), None)
    if listener is None:
        stale = _pid_from(INPUT_PID_FILE, "ProcessId")
        listener = stale if stale in table else None
    if listener is not None:
        parent = table[listener]["ppid"]
        launcher = [parent] if parent in table and table[parent]["name"].lower().startswith("python") else []
        roles["input"] = launcher + [listener]
        roles["asr"] = _descendants(table, listener)
    for role, (path, key) in PID_FILES.items():
        pid = _pid_from(path, key)
        if pid in table:
            roles[role] = [pid] + (_descendants(table, pid) if role != "supervisor" else [])
    return roles


def collect_processes(table: dict) -> dict:
    result, roles = {}, role_pids(table)
    for role in ("input", "asr", "gateway", "backup", "supervisor"):
        pids = roles.get(role)
        if pids:
            result[role] = {"pids": pids, "priv": sum(table[p]["priv"] for p in pids), "handles": sum(table[p]["handles"] for p in pids)}
        else:
            result[role] = None
    return result


def top_tcp(table: dict, n: int = TOP_TCP) -> list[dict]:
    rows = sorted(table["_tcp"], key=lambda r: -r["n"])[:n]
    return [{"pid": r["pid"], "name": table.get(r["pid"], {}).get("name", "?"), "n": r["n"]} for r in rows]


def tcp_states(table: dict) -> dict[str, int]:
    """Connections per state; TimeWait has no owning process, so the top-process list cannot show a pile of them."""
    return {r["state"]: r["n"] for r in sorted(table["_states"], key=lambda r: -r["n"])}


def collect_battery() -> dict | None:
    tmp = Path(tempfile.mkdtemp(prefix="health-battery-"))
    try:
        out = tmp / "battery.xml"
        subprocess.run(["powercfg", "/batteryreport", "/xml", "/output", str(out)], capture_output=True, timeout=60,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if not out.exists():
            return None
        return parse_battery(out.read_text(encoding="utf-8-sig"))
    except (OSError, subprocess.SubprocessError, ET.ParseError):
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def parse_battery(xml_text: str) -> dict | None:
    root = ET.fromstring(xml_text)
    for el in root.iter():
        if el.tag.endswith("}Battery") or el.tag == "Battery":
            vals = {c.tag.split("}")[-1]: (c.text or "").strip() for c in el}
            if vals.get("DesignCapacity", "").isdigit():
                return {k: int(vals[k]) for k in ("DesignCapacity", "FullChargeCapacity", "CycleCount") if vals.get(k, "").isdigit()}
    return None


def collect_website(url: str = WEBSITE_HEALTH_URL) -> dict:
    """GET the site's health endpoint: {"url", "status"} or {"url", "status": None, "error"}. Redirects are not followed:
    a 301/302 is not a healthy answer."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    try:
        with urllib.request.build_opener(NoRedirect).open(url, timeout=WEBSITE_TIMEOUT_S) as resp:
            return {"url": url, "status": resp.status}
    except urllib.error.HTTPError as exc:
        return {"url": url, "status": exc.code}
    except (OSError, ValueError) as exc:  # URLError, timeout, TLS, DNS
        return {"url": url, "status": None, "error": str(getattr(exc, "reason", exc))[:120]}


def find_baseline(history: list[dict], now: datetime) -> dict | None:
    best, best_gap = None, None
    for snap in history:
        gap = abs((now - datetime.fromisoformat(snap["ts"])).total_seconds() / 86400 - BASELINE_DAYS)
        if gap <= BASELINE_TOLERANCE_DAYS and (best_gap is None or gap < best_gap):
            best, best_gap = snap, gap
    return best


def _gb(n) -> str:
    return "—" if n is None else f"{n / GB:.2f} GB"


def _mb(n) -> str:
    return "—" if n is None else f"{n / MB:.0f} MB"


def _delta(now, before) -> str:
    return "无基线" if before is None or now is None else f"{(now - before) / MB:+.0f} MB"


def render(snap: dict, base: dict | None, now: datetime) -> tuple[str, list[str]]:
    red: list[str] = []
    ev = snap["events"]

    def flag(text: str, over: bool, shown: str | None = None) -> str:
        """text goes into the red list; shown (default text) is the table cell."""
        if over:
            red.append(text)
        return f"🔴 {shown or text}" if over else (shown or text)

    lines = [f"## 机器健康检查（{now:%Y-%m-%d %H:%M}，过去 {EVENT_DAYS} 天）", ""]
    base_note = f"对照 {base['ts'][:16]} 的快照" if base else "无上周快照，增长和内存对比从下次起有"
    lines += [f"_{base_note}。🔴 = 超阈值。_", "", "### 系统事件", "| 事件 | 次数 | 阈值 | 最近几次 |", "|---|---|---|---|"]
    rules = [("storage_reset", "iaStorVD 129（存储复位）", RED_STORAGE_RESETS),
             ("port_exhaustion", "Tcpip 4231/4266（端口耗尽）", RED_PORT_EXHAUSTION),
             ("whea", "WHEA-Logger", None),
             ("kernel_power_41", "Kernel-Power 41", RED_KERNEL_POWER_41)]
    for key, label, limit in rules:
        e = ev.get(key)
        if e is None:
            lines.append(f"| {label} | 读取失败 | | |")
            continue
        ids = f"（{', '.join(f'{k}×{v}' for k, v in sorted(e['by_id'].items()))}）" if len(e["by_id"]) > 1 else ""
        over = limit is not None and e["count"] >= limit
        cell = flag(f"{label} {e['count']} 次", over, f"{e['count']} 次{ids}")
        recent = "; ".join(t.replace("T", " ")[:16] for t in e["times"][:5]) or "—"
        lines.append(f"| {label} | {cell} | {'≥ ' + str(limit) if limit else '—'} | {recent} |")

    st = snap["storage"]
    free = flag(f"C: 剩余 {_gb(st['c_free'])}", st["c_free"] / GB < RED_C_FREE_GB, f"{_gb(st['c_free'])} / {_gb(st['c_total'])}")
    lines += ["", "### 存储", f"- C: 剩余 {free}（阈值 < {RED_C_FREE_GB} GB）", "",
              "| 项目 | 大小 | 比上周 |", "|---|---|---|"]
    for name, size in st["sizes"].items():
        before = base["storage"]["sizes"].get(name) if base else None
        lines.append(f"| {name} | {_gb(size)} | {_delta(size, before)} |")
    tmp = st.get("wsl_tmp")
    if tmp:
        pct = tmp["pct"]
        shown = f"{_gb(tmp['used'])} / {_gb(tmp['size'])}（{pct}%）"
        lines.append(f"- WSL /tmp：{flag(f'WSL /tmp 用量 {pct}%', pct >= RED_WSL_TMP_PCT, shown)}（阈值 ≥ {RED_WSL_TMP_PCT}%）")
    else:
        lines.append("- WSL /tmp：读取失败（wsl -e df -P /tmp 无输出）")
    if st.get("wt_top"):
        before = {r["name"]: r["size"] for r in ((base or {}).get("storage") or {}).get("wt_top") or []}
        lines += ["", f"### wt 最大的 {TOP_WT_DIRS} 个子目录", "| 目录 | 大小 | 比上周 |", "|---|---|---|"]
        lines += [f"| {r['name']} | {_gb(r['size'])} | {_delta(r['size'], before.get(r['name']))} |" for r in st["wt_top"]]

    lines += ["", "### 常驻进程（私有内存 / 句柄数，各角色含子进程）", "| 角色 | PID | 私有内存 | 比上周 | 句柄 | 比上周 |", "|---|---|---|---|---|---|"]
    for role, p in snap["processes"].items():
        if p is None:
            lines.append(f"| {role} | 未找到 | | | | |")
            continue
        b = (base or {}).get("processes", {}).get(role)
        grew = b is not None and b["priv"] > 0 and p["priv"] / b["priv"] - 1 >= RED_MEMORY_GROWTH
        mem = flag(f"{role} 内存 {_mb(b['priv'])}→{_mb(p['priv'])}" if b else "", grew, _mb(p["priv"]))
        mem_delta = "无基线" if b is None else f"{p['priv'] / b['priv'] - 1:+.0%}" if b["priv"] else "—"
        hdelta = "无基线" if b is None else f"{p['handles'] - b['handles']:+d}"
        lines.append(f"| {role} | {','.join(map(str, p['pids']))} | {mem} | {mem_delta} | {p['handles']} | {hdelta} |")
    lines.append(f"- 内存阈值：比上周涨 ≥ {RED_MEMORY_GROWTH:.0%}。")

    lines += ["", f"### 当前 TCP 连接最多的前 {TOP_TCP} 个进程（端口耗尽只能事后查，留作下次对照）",
              "- 本次：" + "，".join(f"{t['name']}({t['pid']}) {t['n']}" for t in snap["top_tcp"])]
    if base and base.get("top_tcp"):
        lines.append("- 上周：" + "，".join(f"{t['name']}({t['pid']}) {t['n']}" for t in base["top_tcp"]))

    states = snap.get("tcp_states")
    if states:
        lines.append("- 按状态：" + "，".join(f"{k} {v}" for k, v in states.items()) + f"，合计 {sum(states.values())}")
        if base and base.get("tcp_states"):
            lines.append("- 上周：" + "，".join(f"{k} {v}" for k, v in base["tcp_states"].items()) + f"，合计 {sum(base['tcp_states'].values())}")

    site = snap.get("website")
    if site:
        ok = site["status"] == 200
        shown = f"HTTP {site['status']}" if site["status"] is not None else f"无响应（{site.get('error', '')}）"
        lines += ["", "### 官网在线", f"- {site['url']}：{flag(f'官网 {shown}', not ok, shown)}（要求 HTTP 200）"]

    browser_alerts = snap.get("headless_browser_alerts") or []
    browser_text = "；".join(browser_alerts)
    lines += ["", "### 后台无头浏览器",
              f"- {flag('无头浏览器：' + browser_text, bool(browser_alerts), browser_text or '无异常')}（只报告，不结束进程）"]

    lines += ["", "### 电池"]
    bat = snap["battery"]
    if bat:
        pct = f" ({bat['FullChargeCapacity'] / bat['DesignCapacity']:.0%})" if bat.get("FullChargeCapacity") and bat.get("DesignCapacity") else ""
        lines.append(f"- 设计容量 {bat.get('DesignCapacity', '—')} mWh，满充容量 {bat.get('FullChargeCapacity', '—')} mWh{pct}，循环次数 {bat.get('CycleCount', '—')}")
    else:
        lines.append("- 读取失败（powercfg /batteryreport 无输出）")

    lines += ["", "**标红项：** " + ("；".join(red) if red else "无")]
    return "\n".join(lines) + "\n", red


def load_history() -> list[dict]:
    try:
        return json.loads(HISTORY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def main() -> None:
    now = datetime.now()
    history = load_history()
    base = find_baseline(history, now)
    table = process_table()
    snap = {"ts": now.isoformat(timespec="seconds"), "events": collect_events(), "storage": collect_storage(),
            "processes": collect_processes(table), "top_tcp": top_tcp(table), "tcp_states": tcp_states(table), "battery": collect_battery(),
            "website": collect_website(), "headless_browser_alerts": headless_browser_alerts(table)}
    text, _ = render(snap, base, now)
    print(text)
    if SAVE_HISTORY:
        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        HISTORY.write_text(json.dumps((history + [snap])[-HISTORY_KEEP:], ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
