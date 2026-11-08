"""One-off measurement: how much does a generating local 35B slow the input station's ASR model?

Run in the ASR GPU venv (read-only use of it):  <incore>\\.runtime\\quant-gpu-venv\\Scripts\\python.exe control\\gpu_yield_contention.py
Runs only inside the 23:00-07:00 US Eastern window. Never touches the production ASR worker or the input data: it loads its own copy of the
same Fun-ASR-Nano Q5 model on the same GPU, transcribes a public clip (JFK speech, .runtime/samples) and starts its own llama-server.
The guard thread asks control/gpu_yield.py every few seconds and stops the 35B the moment it says no (VRAM below 1536 MiB, input station in use).
"""
import json
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
import wave
from array import array
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gpu_yield  # noqa: E402
import instance_config as ic  # noqa: E402  (this machine's paths; defaults = today's values)

# ---- CONFIG ----
INCUBATOR = Path(ic.V["workspace"] + r"\incore")
NANO_MODEL = INCUBATOR / ".runtime/nano-quant-benchmark/Fun-ASR-Nano-2512-Q5_K_M.gguf"
CLIP = INCUBATOR / ".runtime/samples/jfk-long-smoke.wav"      # public domain speech, 16 kHz mono
CLIP_SECONDS = 120                                            # first N seconds = 4 windows of 30 s, the size the real worker uses
RUNS = 5
WARMUP_RUNS = 2
LANGUAGE = "en"
LLAMA = ic.V["tools_dir"] + r"\llama.cpp\llama-server.exe"
LLAMA_MODEL = ic.V["models_dir"] + r"\Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"
LLAMA_PORT = 8793                                             # not the FA bench's 8792
LLAMA_ARGS = ["-ngl", "99", "--n-cpu-moe", "38", "-c", "16384", "-ctk", "q8_0", "-ctv", "q8_0", "-fa", "on", "-b", "512", "-ub", "512", "-np", "1", "--jinja"]
GEN_ALONE_REQUESTS = 3
GEN_MAX_TOKENS = 400
HEALTH_WAIT_S = 480
GUARD_SECONDS = 5
OUT = Path(ic.V["wt_root"] + r"\gpu-yield-asr\out\contention.json")
# ----------------

PROMPT = ("Write a long, detailed technical essay about how a factory floor scheduler should handle machine breakdowns, rush orders and "
          "operator shortages at the same time. Cover data needs, heuristics, rescheduling triggers and how to explain decisions to supervisors.")
report = {"started": datetime.now(timezone.utc).isoformat(), "clip_seconds": CLIP_SECONDS, "runs": RUNS, "language": LANGUAGE}
stop_gen, guard_msg = threading.Event(), []
vram = {"min_free": 10 ** 9, "max_used": 0}


def sample_vram():
    while not stop_gen.is_set() or not report.get("done"):
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.free", "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout
        used, free = (int(x) for x in out.split(","))
        vram["min_free"], vram["max_used"] = min(vram["min_free"], free), max(vram["max_used"], used)
        time.sleep(1)


def load_clip():
    with wave.open(str(CLIP), "rb") as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        pcm = array("h", w.readframes(16000 * CLIP_SECONDS))
    samples = array("f", (v / 32768 for v in pcm))
    return [samples[i:i + 30 * 16000] for i in range(0, len(samples), 30 * 16000)]


def timed_runs(session, windows, label, n):
    times, text = [], ""
    for i in range(n):
        t0 = time.perf_counter()
        text = "".join(session.run(w, language=LANGUAGE, itn="on", timestamps="none").text for w in windows)
        times.append(round(time.perf_counter() - t0, 3))
        print(f"{label} run {i + 1}: {times[-1]} s", flush=True)
    return {"seconds": times, "median": statistics.median(times), "slowest": max(times), "fastest": min(times), "text_head": text[:80], "text_chars": len(text)}


def generate_once():
    body = json.dumps({"model": "local", "temperature": 0, "max_tokens": GEN_MAX_TOKENS, "chat_template_kwargs": {"enable_thinking": False},
                       "messages": [{"role": "user", "content": PROMPT}]}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{LLAMA_PORT}/v1/chat/completions", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        j = json.load(r)
    t = j.get("timings", {})
    return {"tokens": t.get("predicted_n"), "tok_per_s": round(t.get("predicted_per_second", 0), 2), "prompt_tok_per_s": round(t.get("prompt_per_second", 0), 2)}


def main():
    if not gpu_yield.in_night_window(datetime.now(timezone.utc)):
        sys.exit("outside 23:00-07:00 US Eastern; refusing to measure")
    first = gpu_yield.can_run()
    report["can_run_at_start"] = [first.allowed, first.reason]
    if not first.allowed:
        sys.exit(f"gpu_yield says no: {first.reason}")
    threading.Thread(target=sample_vram, daemon=True).start()
    import transcribe_cpp
    device = next(d for d in transcribe_cpp.backends() if d.kind == "vulkan" and "RTX 5070" in d.description)
    windows = load_clip()
    server = None
    with transcribe_cpp.Model(NANO_MODEL, device=device) as model, model.session(n_threads=8) as session:
        report["vram_after_nano_load_used_mib"] = int(subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout)
        for _ in range(WARMUP_RUNS):
            for w in windows:
                session.run(w, language=LANGUAGE, itn="on", timestamps="none")
        report["asr_35b_idle"] = timed_runs(session, windows, "35B idle", RUNS)
        try:
            server = subprocess.Popen([LLAMA, "-m", LLAMA_MODEL, "--host", "127.0.0.1", "--port", str(LLAMA_PORT), *LLAMA_ARGS],
                                      stdout=open(OUT.with_name("llama-server.log"), "wb"), stderr=subprocess.STDOUT)

            def guard():
                while not stop_gen.is_set():
                    d = gpu_yield.can_run()
                    if not d.allowed:
                        guard_msg.append(f"{datetime.now(timezone.utc).isoformat()} {d.basis}: {d.reason}")
                        server.terminate()
                        stop_gen.set()
                        return
                    time.sleep(GUARD_SECONDS)

            for _ in range(HEALTH_WAIT_S // 2):
                time.sleep(2)
                if guard_msg or server.poll() is not None:
                    break
                try:
                    if json.load(urllib.request.urlopen(f"http://127.0.0.1:{LLAMA_PORT}/health", timeout=3)).get("status") == "ok":
                        break
                except (OSError, ValueError):
                    pass
            else:
                guard_msg.append("llama-server not healthy in time")
            if not guard_msg and server.poll() is None:
                threading.Thread(target=guard, daemon=True).start()
                report["vram_with_35b_used_mib"] = int(subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout)
                report["gen_alone"] = [generate_once() for _ in range(GEN_ALONE_REQUESTS)]
                print("gen alone:", report["gen_alone"], flush=True)
                gen_results = []

                def generator():
                    while not stop_gen.is_set():
                        try:
                            gen_results.append(generate_once())
                        except Exception as exc:
                            gen_results.append({"error": repr(exc)[:120]})
                            return

                gen_thread = threading.Thread(target=generator, daemon=True)
                gen_thread.start()
                time.sleep(15)       # let generation settle
                gen_results.clear()
                report["asr_35b_generating"] = timed_runs(session, windows, "35B generating", RUNS)
                report["gen_during_asr"] = list(gen_results)
        finally:
            stop_gen.set()
            if server and server.poll() is None:
                server.terminate()
                try:
                    server.wait(30)
                except subprocess.TimeoutExpired:
                    server.kill()
    report["guard_stopped_35b"] = guard_msg
    report["vram_min_free_mib"], report["vram_max_used_mib"] = vram["min_free"], vram["max_used"]
    report["finished"] = datetime.now(timezone.utc).isoformat()
    report["done"] = True
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    main()
