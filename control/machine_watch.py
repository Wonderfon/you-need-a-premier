"""Sustained machine-anomaly watch (owner\x2011-01: the twice-daily snapshot cannot see dwm / a driver service spinning for days).

The scheduler's supervisor loop calls step() each pass; every SAMPLE_EVERY_S it takes one cheap sample (per-process CPU delta
between two samples, dwm / System / WmiPrvSE, GPU, free memory, free C:, minutes since the last keyboard/mouse input) and
appends a row to data/machine-health.jsonl. Anomalies are judged over consecutive rows, so only a sustained state is reported.
Read-only: nothing is stopped or changed. Stdlib only (the input dashboard loads this file by path). Windows only; elsewhere
sample() returns None.
"""
from __future__ import annotations

import ctypes
import json
import re
import shutil
import subprocess
import time
from ctypes import wintypes
from datetime import datetime, timedelta
from pathlib import Path

# ---- CONFIG ----
SAMPLE_EVERY_S = 600              # one sample per 10 minutes
MAX_GAP_S = 1200                  # rows further apart than this are not "consecutive"
CORE_PCT = 90.0                   # one process >= this % of one core ...
CORE_RUNS = 3                     # ... in this many consecutive samples (30 minutes)
AVG_PCT = 75.0                    # or: one process averaging >= this % of one core ...
AVG_RUNS = 6                      # ... over its last this many consecutive samples (1 hour); wobbling 76-99% still counts
DWM_PCT = 40.0                    # dwm.exe (summed) >= this % of one core ...
DWM_RUNS = 3                      # ... in this many consecutive samples
GPU_BUSY_PCT = 80.0               # GPU load while the owner is at the machine ...
INPUT_RECENT_MIN = 10             # ... "at the machine" = input within this many minutes
MODEL_NAME_RE = r"llama|ollama|vllm|koboldcpp|lm-?studio|text-generation|whisper|comfy"
MODEL_PYTHON_CMD_RE = r"gen_local|llama|torch|transformers|vllm|--gpu|finetune|train"
MEM_FREE_MIN_GB = 8.0             # same lines as daily_health.report_line
C_FREE_MIN_GB = 100.0
LOW_RUNS = 2                      # free memory / free C: below the line in this many consecutive samples
TOP_N = 10
REPORT_HOURS = 12
STALE_AFTER_S = 1800              # a newest row older than this means the sampler is not running
MAX_BYTES = 4_000_000             # trim the file to the last KEEP_ROWS rows beyond this
KEEP_ROWS = 2000
ALERT_REPEAT_H = 6                # discord alert for the same anomaly at most this often (function only, not wired)
GPU_QUERY = "utilization.gpu,memory.used,memory.total,temperature.gpu,pstate"
# ----------------

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_MIB = 1024 ** 2
_GB = 1024 ** 3


# ---------- sampling (Windows) ----------

class _ProcessEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


class _MemoryStatus(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


class _LastInput(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def _ft(value: wintypes.FILETIME) -> int:
    return (value.dwHighDateTime << 32) | value.dwLowDateTime


def process_times() -> dict[int, tuple[str, int, float]]:
    """{pid: (exe name, creation FILETIME, cpu seconds)} for every process we may open (System may be refused)."""
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.OpenProcess.restype = wintypes.HANDLE
    snap = k.CreateToolhelp32Snapshot(0x2, 0)
    out: dict[int, tuple[str, int, float]] = {}
    entry = _ProcessEntry()
    entry.dwSize = ctypes.sizeof(entry)
    more = k.Process32FirstW(snap, ctypes.byref(entry))
    while more:
        pid = entry.th32ProcessID
        handle = k.OpenProcess(0x1000, False, pid) if pid else None  # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
            if k.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
                out[pid] = (entry.szExeFile, _ft(created), (_ft(kernel) + _ft(user)) / 1e7)
            k.CloseHandle(handle)
        more = k.Process32NextW(snap, ctypes.byref(entry))
    k.CloseHandle(snap)
    return out


class _PdhItem(ctypes.Structure):
    _fields_ = [("szName", ctypes.c_void_p), ("CStatus", wintypes.DWORD), ("value", ctypes.c_double)]


def pdh_open() -> tuple | None:
    """Performance-counter query over every process (percent of one core). dwm.exe, System and WmiPrvSE cannot be opened by a
    normal user process (GetProcessTimes fails), the counters can. Kept open between samples; the first read is the baseline."""
    try:
        pdh = ctypes.WinDLL("pdh")
        query, counter = ctypes.c_void_p(), ctypes.c_void_p()
        if pdh.PdhOpenQueryW(None, 0, ctypes.byref(query)) or pdh.PdhAddEnglishCounterW(
                query, r"\Process(*)\% Processor Time", 0, ctypes.byref(counter)) or pdh.PdhCollectQueryData(query):
            return None
        return pdh, query, counter
    except (OSError, AttributeError):
        return None


def pdh_read(handle: tuple) -> dict[str, float]:
    """{lower-case instance name without #n: summed % of one core} since the previous read."""
    pdh, query, counter = handle
    if pdh.PdhCollectQueryData(query):
        return {}
    size, count = wintypes.DWORD(0), wintypes.DWORD(0)
    pdh.PdhGetFormattedCounterArrayW(counter, 0x200, ctypes.byref(size), ctypes.byref(count), None)  # PDH_FMT_DOUBLE; asks the size
    if not size.value:
        return {}
    buffer = ctypes.create_string_buffer(size.value)
    if pdh.PdhGetFormattedCounterArrayW(counter, 0x200, ctypes.byref(size), ctypes.byref(count), buffer):
        return {}
    out: dict[str, float] = {}
    for item in (_PdhItem * count.value).from_buffer(buffer):
        if item.CStatus in (0, 1):
            name = ctypes.wstring_at(item.szName).split("#")[0].lower()
            out[name] = out.get(name, 0.0) + item.value
    return out


def idle_minutes() -> float | None:
    """Minutes since the last keyboard/mouse input of this session (GetLastInputInfo); None if unreadable."""
    try:
        info = _LastInput()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        ticks = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
        return round(((ticks - info.dwTime) & 0xFFFFFFFF) / 60000, 1)
    except (OSError, AttributeError):
        return None


def gpu_reading() -> dict | None:
    try:
        done = subprocess.run(["nvidia-smi", f"--query-gpu={GPU_QUERY}", "--format=csv,noheader,nounits"], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=5, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_gpu(done.stdout) if done.returncode == 0 else None


def parse_gpu(output: str) -> dict | None:
    best = None
    for line in output.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            row = {"util": float(parts[0]), "mem_used_mib": float(parts[1]), "mem_total_mib": float(parts[2]),
                   "temp": float(parts[3]), "pstate": parts[4]}
        except ValueError:
            continue
        if best is None or row["util"] > best["util"]:
            best = row
    return best


def memory_free_gb() -> float | None:
    try:
        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(status)
        ctypes.WinDLL("kernel32").GlobalMemoryStatusEx(ctypes.byref(status))
        return round(status.ullAvailPhys / _GB, 1)
    except (OSError, AttributeError):
        return None


def c_free_gb() -> float | None:
    try:
        return round(shutil.disk_usage("C:\\").free / _GB, 1)
    except OSError:
        return None


def command_lines(pids: list[int]) -> dict[int, str]:
    """Command lines of the given pids (one short PowerShell/CIM call; only used when the GPU rule fires)."""
    if not pids:
        return {}
    flt = " -or ".join(f"ProcessId={int(p)}" for p in pids)
    try:
        done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                               f"Get-CimInstance Win32_Process -Filter '{flt}' | ForEach-Object {{ \"$($_.ProcessId)`t$($_.CommandLine)\" }}"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return {}
    found = {}
    for line in done.stdout.splitlines():
        pid, _, cmd = line.partition("\t")
        if pid.strip().isdigit():
            found[int(pid)] = cmd.strip()[:300]
    return found


def model_candidates(times: dict[int, tuple[str, int, float]]) -> list[int]:
    return [pid for pid, (name, _c, _t) in times.items() if re.search(MODEL_NAME_RE, name, re.I) or name.lower().startswith("python")]


def sample(prev: dict | None, now: datetime | None = None) -> tuple[dict | None, dict]:
    """One sample -> (row, state for the next call). `prev` is the state this function returned last time (or None).
    The first row after a start has no CPU figures (needs two samples)."""
    started = time.perf_counter()
    now = now or datetime.now().astimezone()
    wall = time.time()
    times = process_times()
    top, dwm, system, wmi, interval = [], None, None, None, None
    handle = (prev or {}).get("pdh") or pdh_open()
    if prev:
        interval = wall - prev["wall"]
        cpu = {}
        for pid, (name, created, seconds) in times.items():
            old = prev["procs"].get(pid)
            if old and old[1] == created and interval > 0:
                cpu[pid] = (name, max(0.0, (seconds - old[2]) / interval * 100))
        top = [{"name": n, "pid": p, "cpu": round(c, 1)} for p, (n, c) in sorted(cpu.items(), key=lambda kv: -kv[1][1])[:TOP_N]]
        counters = pdh_read(handle) if handle else {}
        dwm, system, wmi = (round(counters[k], 1) if k in counters else None for k in ("dwm", "system", "wmiprvse"))
    gpu, idle = gpu_reading(), idle_minutes()
    row = {"at": now.isoformat(timespec="seconds"), "interval_s": round(interval) if interval else None, "top": top,
           "dwm": dwm, "system": system, "wmiprvse": wmi, "gpu": gpu, "mem_free_gb": memory_free_gb(),
           "c_free_gb": c_free_gb(), "idle_min": idle, "model_procs": []}
    if gpu and gpu["util"] >= GPU_BUSY_PCT and idle is not None and idle <= INPUT_RECENT_MIN:
        pids = model_candidates(times)
        cmds = command_lines(pids)
        row["model_procs"] = [{"name": times[p][0], "pid": p, "cmd": cmds.get(p, "")} for p in pids
                              if re.search(MODEL_NAME_RE, times[p][0], re.I) or re.search(MODEL_PYTHON_CMD_RE, cmds.get(p, ""), re.I)]
    row["sample_ms"] = round((time.perf_counter() - started) * 1000)
    return row, {"wall": wall, "procs": times, "pdh": handle}


# ---------- file ----------

def append_row(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    if path.stat().st_size > MAX_BYTES:
        keep = read_rows(path)[-KEEP_ROWS:]
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep), encoding="utf-8")


def read_rows(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("at"), str):
            rows.append(row)
    return rows


def _time(row: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(row["at"])
    except (ValueError, TypeError, KeyError):
        return None


def step(state: dict, t: float, path: Path) -> dict | None:
    """Supervisor hook: sample when due (state is the supervisor's dict slot), append, return the row or None."""
    if t - state.get("last", -1e9) < SAMPLE_EVERY_S:
        return None
    state["last"] = t
    row, state["prev"] = sample(state.get("prev"))
    if row is not None:
        append_row(path, row)
    return row


# ---------- judging ----------

def _chain(rows: list[dict], i: int, runs: int) -> list[dict] | None:
    """The last `runs` rows up to i that have CPU figures, if they are consecutive in time, else None.
    Rows without interval_s (the first row after a sampler restart, or a stray empty row) carry no reading: skipped, they do not break the chain."""
    if rows[i].get("interval_s") is None:
        return None
    chain = [r for r in rows[:i + 1] if r.get("interval_s") is not None][-runs:]
    if len(chain) < runs:
        return None
    stamps = [_time(r) for r in chain]
    if any(s is None for s in stamps):
        return None
    if any((b - a).total_seconds() > MAX_GAP_S for a, b in zip(stamps, stamps[1:])):
        return None
    return chain


def anomalies_at(rows: list[dict], i: int) -> list[dict]:
    """Anomalies that hold at row i: [{"kind", "key", "detail"}]."""
    row, found = rows[i], []
    chain = _chain(rows, i, CORE_RUNS)
    if chain:
        for item in row.get("top") or []:
            same = [next((x for x in (r.get("top") or []) if x.get("pid") == item["pid"] and x.get("name") == item["name"]), None) for r in chain]
            if all(x and x["cpu"] >= CORE_PCT for x in same):
                low = min(x["cpu"] for x in same)
                found.append({"kind": "cpu", "key": f"{item['name']}#{item['pid']}",
                              "detail": f"{item['name']}（pid {item['pid']}）连续 {CORE_RUNS} 次占满一个核（最低 {low:.0f}%）"})
    chain = _chain(rows, i, AVG_RUNS)
    if chain:
        for item in row.get("top") or []:
            key = f"{item['name']}#{item['pid']}"
            if any(f["key"] == key for f in found):
                continue
            same = [next((x["cpu"] for x in (r.get("top") or []) if x.get("pid") == item["pid"] and x.get("name") == item["name"]), 0.0) for r in chain]  # not in the top list = counted as 0
            if sum(same) / AVG_RUNS >= AVG_PCT:
                found.append({"kind": "cpu", "key": key, "detail": f"{item['name']}（pid {item['pid']}）最近 {AVG_RUNS} 次平均 {sum(same) / AVG_RUNS:.0f}% 个核（最低 {min(same):.0f}%）"})
    chain = _chain(rows, i, DWM_RUNS)
    if chain and all((r.get("dwm") or 0) >= DWM_PCT for r in chain):
        found.append({"kind": "dwm", "key": "dwm", "detail": f"dwm 连续 {DWM_RUNS} 次 ≥{DWM_PCT:.0f}%（最低 {min(r['dwm'] for r in chain):.0f}%）"})
    gpu, idle = row.get("gpu") or {}, row.get("idle_min")
    if gpu.get("util", 0) >= GPU_BUSY_PCT and idle is not None and idle <= INPUT_RECENT_MIN and row.get("model_procs"):
        who = "；".join(f"{p['name']}（pid {p['pid']}）{(p.get('cmd') or '')[:120]}" for p in row["model_procs"][:3])
        found.append({"kind": "gpu_input", "key": "gpu", "detail": f"你在用电脑时显卡 {gpu['util']:.0f}%，本机模型进程：{who}"})
    for kind, field, line, label in (("memory", "mem_free_gb", MEM_FREE_MIN_GB, "可用内存"), ("disk", "c_free_gb", C_FREE_MIN_GB, "C 盘剩余")):
        if i + 1 >= LOW_RUNS:
            last = rows[i + 1 - LOW_RUNS:i + 1]
            if all(isinstance(r.get(field), (int, float)) and r[field] < line for r in last):
                found.append({"kind": kind, "key": kind, "detail": f"{label} {row[field]:.1f} GB（低于 {line:g} GB）"})
    return found


def episodes(rows: list[dict], since: datetime) -> list[dict]:
    """Anomalies in rows newer than `since`, merged per (kind,key): first/last time and number of samples."""
    merged: dict[tuple, dict] = {}
    for i, row in enumerate(rows):
        stamp = _time(row)
        if stamp is None or stamp < since:
            continue
        for a in anomalies_at(rows, i):
            e = merged.setdefault((a["kind"], a["key"]), {**a, "first": stamp, "last": stamp, "count": 0})
            e["last"], e["count"], e["detail"] = stamp, e["count"] + 1, a["detail"]
    return sorted(merged.values(), key=lambda e: e["first"])


def _when(e: dict) -> str:
    """「11-04 16:32–06:00」 within one day, 「11-04 16:32–11-08 06:00」 across midnight."""
    first, last = e["first"].astimezone(), e["last"].astimezone()
    end = f"{last:%H:%M}" if first.date() == last.date() else f"{last:%m-%d %H:%M}"
    return f"{first:%m-%d %H:%M}–{end}"


def report_part(path: Path, now: datetime | None = None, hours: int = REPORT_HOURS) -> str:
    """One sentence for the 09:00 / 21:00 「机器和备份」 line."""
    now = now or datetime.now().astimezone()
    rows = read_rows(path)
    since = now - timedelta(hours=hours)
    recent = [r for r in rows if (_time(r) or since) > since]
    if not recent:
        return f"近 {hours} 小时没有机器取样记录（取样没在跑，不能当作没有异常）"
    found = episodes(rows, since)
    if not found:
        return f"近 {hours} 小时取样 {len(recent)} 次，无持续异常"
    parts = []
    for e in found:
        days = f"约 {(e['last'] - e['first']).days} 天，" if e["last"] - e["first"] > timedelta(hours=24) else ""
        parts.append(f"{e['detail']}，{_when(e)}（{days}{e['count']} 次取样）")
    return f"近 {hours} 小时持续异常：" + "；".join(parts)


def current(path: Path, now: datetime | None = None) -> dict:
    """For the org dashboard: {"state": "ok"|"stale"|"missing", "anomalies": [...], "at": iso}."""
    now = now or datetime.now().astimezone()
    rows = read_rows(path)
    if not rows:
        return {"state": "missing", "anomalies": []}
    stamp = _time(rows[-1])
    if stamp is None or (now - stamp).total_seconds() > STALE_AFTER_S:
        return {"state": "stale", "anomalies": [], "at": rows[-1]["at"]}
    # judge on the newest row that carries a reading; a trailing empty row must not hide the anomaly
    last = next((i for i in range(len(rows) - 1, -1, -1) if rows[i].get("interval_s") is not None), None)
    found = []
    if last is not None:
        reading_at = _time(rows[last])
        if reading_at is not None and (now - reading_at).total_seconds() <= STALE_AFTER_S:
            found = anomalies_at(rows, last)
    return {"state": "ok", "at": rows[-1]["at"], "anomalies": [{"kind": a["kind"], "detail": a["detail"]} for a in found]}


# ---------- Discord (function only; wiring is sending code, left to the owner's merge) ----------

def alert_text(found: list[dict]) -> str:
    return "机器持续异常：" + "；".join(a["detail"] for a in found) + "。只读观察，没有动任何进程。"


def alert_if_new(post, path: Path, state_path: Path, now: datetime | None = None) -> bool:
    """post(text) -> bool. Sends one message for anomalies standing at the newest row that were not alerted in ALERT_REPEAT_H."""
    now = now or datetime.now().astimezone()
    rows = read_rows(path)
    if not rows:
        return False
    try:
        sent = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        sent = {}
    cutoff = now - timedelta(hours=ALERT_REPEAT_H)
    new = []
    for a in anomalies_at(rows, len(rows) - 1):
        key = f"{a['kind']}:{a['key']}"
        try:
            fresh = datetime.fromisoformat(sent[key]) < cutoff
        except (KeyError, ValueError):
            fresh = True
        if fresh:
            new.append((key, a))
    if not new or not post(alert_text([a for _, a in new])):
        return False
    sent.update({key: now.isoformat(timespec="seconds") for key, _ in new})
    state_path.write_text(json.dumps(sent, ensure_ascii=False, indent=1), encoding="utf-8")
    return True


if __name__ == "__main__":  # quick manual check: one two-sample probe, nothing written
    first, state = sample(None)
    time.sleep(3)
    row, _ = sample(state)
    print(json.dumps(row, ensure_ascii=False, indent=1)[:1800], "\nsample_ms first/second:", first["sample_ms"], row["sample_ms"])
