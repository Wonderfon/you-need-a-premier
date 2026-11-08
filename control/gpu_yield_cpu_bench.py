"""One-off measurement: pure-CPU local models (llama.cpp -ngl 0, GPU hidden) at 8 and 16 threads, one run each.

Run: <incore>\\discord\\.venv\\Scripts\\python.exe control\\gpu_yield_cpu_bench.py       (stdlib only; 23:00-07:00 US Eastern only)
Per config: generation speed, time to read an 8k-token context, process peak memory (peak working set; models are memory-mapped, so it includes
the mapped weights), whole-machine CPU use and Intel RAPL package power (Windows counters, 1 s samples) and GPU temperature/power as a chassis proxy.
CPU temperature and whole-system power have no Windows counter on this machine (checked); they are reported as unreadable.
The first 35B 16-thread run also runs the control/ unit tests next to it to see how much a loaded CPU slows ordinary work.
"""
import csv
import ctypes
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gpu_yield  # noqa: E402
import instance_config as ic  # noqa: E402  (this machine's paths; defaults = today's values)

# ---- CONFIG ----
LLAMA_BENCH = ic.V["tools_dir"] + r"\llama.cpp\llama-bench.exe"
MODELS = {"Qwen3.5-4B": ic.V["models_dir"] + r"\Qwen3.5-4B-Q4_K_M.gguf",
          "Qwen3.5-9B": ic.V["models_dir"] + r"\Qwen3.5-9B-Q4_K_M.gguf",
          "Qwen3.6-35B-A3B": ic.V["models_dir"] + r"\Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"}
THREADS = (8, 16)
PROMPT_TOKENS = 8192
GEN_TOKENS = 64
BASELINE_SECONDS = 8
PY = ic.V["workspace"] + r"\incore\discord\.venv\Scripts\python.exe"
CONTROL_DIR = Path(__file__).resolve().parent
LOAD_TEST_CONFIG = ("Qwen3.6-35B-A3B", 16)       # run the control tests next to this config
MIN_FREE_RAM_GB = 30
OUT = Path(ic.V["wt_root"] + r"\gpu-yield-asr\out\cpu-bench.json")
# ----------------

COUNTERS = [r"\Energy Meter(RAPL_Package0_PKG)\Power", r"\Processor Information(_Total)\% Processor Utility", r"\Processor Information(_Total)\Processor Frequency"]


class MemCounters(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def peak_working_set_gib(pid):
    k32, psapi = ctypes.WinDLL("kernel32", use_last_error=True), ctypes.WinDLL("psapi", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    handle = k32.OpenProcess(0x1000 | 0x0400, False, pid)   # QUERY_LIMITED_INFORMATION | QUERY_INFORMATION
    if not handle:
        return None
    info = MemCounters()
    info.cb = ctypes.sizeof(info)
    ok = psapi.GetProcessMemoryInfo(handle, ctypes.byref(info), info.cb)
    k32.CloseHandle(handle)
    return round(info.PeakWorkingSetSize / 2 ** 30, 2) if ok else None


def gpu_state():
    out = subprocess.run(["nvidia-smi", "--query-gpu=temperature.gpu,power.draw,memory.used", "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout
    t, p, m = (float(x) for x in out.split(","))
    return {"gpu_temp_c": t, "gpu_power_w": p, "gpu_mem_used_mib": m}


def counters_for(seconds_or_none, stop=None):
    """Run typeperf at 1 s until `stop` is set (or for N samples); returns list of (power_w, cpu_util_pct, mhz)."""
    cmd = ["typeperf", *COUNTERS, "-si", "1"] + (["-sc", str(seconds_or_none)] if seconds_or_none else [])
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    rows = []

    def reader():
        for line in proc.stdout:
            parts = next(csv.reader([line]), [])
            try:
                rows.append((float(parts[1]) / 1000, float(parts[2]), float(parts[3])))
            except (ValueError, IndexError):
                continue
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    if seconds_or_none:
        proc.wait()
    else:
        stop.wait()
        proc.terminate()
        proc.wait()
    thread.join(5)
    return rows


def summarize(rows):
    if not rows:
        return {}
    return {"cpu_pct_mean": round(statistics.mean(r[1] for r in rows), 1), "cpu_pct_max": round(max(r[1] for r in rows), 1),
            "pkg_power_w_mean": round(statistics.mean(r[0] for r in rows), 1), "pkg_power_w_max": round(max(r[0] for r in rows), 1),
            "mhz_mean": round(statistics.mean(r[2] for r in rows)), "samples": len(rows)}


def run_control_tests():
    t0 = time.perf_counter()
    p = subprocess.run([PY, "-m", "unittest", "discover", "-s", str(CONTROL_DIR), "-p", "test_*.py"], capture_output=True, text=True, cwd=CONTROL_DIR)
    tail = [ln for ln in p.stderr.strip().splitlines() if ln][-1:]
    return {"seconds": round(time.perf_counter() - t0, 1), "returncode": p.returncode, "last_line": tail}


def free_ram_gb():
    out = subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory"], capture_output=True, text=True).stdout
    return round(int(out.strip()) / 2 ** 20, 1)


def bench(name, model, threads, with_tests):
    free = free_ram_gb()
    if free < MIN_FREE_RAM_GB and name.startswith("Qwen3.6"):
        return {"skipped": f"only {free} GB free RAM"}
    env = dict(os.environ, CUDA_VISIBLE_DEVICES="-1")      # hide the GPU so nothing is offloaded
    gpu_before = gpu_state()
    base = summarize(counters_for(BASELINE_SECONDS))
    cmd = [LLAMA_BENCH, "-m", model, "-ngl", "0", "-t", str(threads), "-p", str(PROMPT_TOKENS), "-n", str(GEN_TOKENS), "-r", "1", "-o", "json"]
    stop = threading.Event()
    box = {}
    sampler = threading.Thread(target=lambda: box.update(rows=counters_for(None, stop)), daemon=True)
    sampler.start()
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
    peak, tests = 0, None
    test_thread = None
    if with_tests:
        time.sleep(20)
        test_thread = threading.Thread(target=lambda: box.update(tests=run_control_tests()), daemon=True)
        test_thread.start()
    while proc.poll() is None:
        peak = max(peak, peak_working_set_gib(proc.pid) or 0)
        time.sleep(1)
    out = proc.stdout.read()
    wall = round(time.perf_counter() - t0, 1)
    gpu_after = gpu_state()
    if test_thread:
        test_thread.join()
    stop.set()
    sampler.join(10)
    result = {"model": name, "threads": threads, "wall_seconds": wall, "baseline": base, "during": summarize(box.get("rows", [])),
              "peak_working_set_gib": peak, "gpu_before": gpu_before, "gpu_after": gpu_after, "cpu_temperature": "unreadable (no Windows thermal zone instance; no admin sensor tool)"}
    try:
        for row in json.loads(out):
            if row.get("n_prompt"):
                result["prompt_tok_per_s"] = round(row["avg_ts"], 2)
                result["read_8k_seconds"] = round(PROMPT_TOKENS / row["avg_ts"], 1)
            if row.get("n_gen"):
                result["gen_tok_per_s"] = round(row["avg_ts"], 2)
    except ValueError:
        result["error"] = out[-300:]
    if with_tests:
        result["control_tests_during"] = box.get("tests")
    return result


def main():
    if not gpu_yield.in_night_window(datetime.now(timezone.utc)):
        sys.exit("outside 23:00-07:00 US Eastern; refusing to measure")
    report = {"started": datetime.now(timezone.utc).isoformat(), "control_tests_idle": [run_control_tests(), run_control_tests()], "configs": []}
    print("control tests idle:", report["control_tests_idle"], flush=True)
    for name, model in MODELS.items():
        for threads in THREADS:
            r = bench(name, model, threads, (name, threads) == LOAD_TEST_CONFIG)
            report["configs"].append(r)
            print(json.dumps(r), flush=True)
            OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["finished"] = datetime.now(timezone.utc).isoformat()
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    main()
