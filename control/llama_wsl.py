"""WSL CUDA llama.cpp service: python3 control/llama_wsl.py start|bench|stop."""
import argparse
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen
import instance_config as ic  # noqa: E402  (this machine's paths and owner identity; defaults = today's values)

# ---- CONFIG ----
CONFIG = {
    "server": Path("~/tools/llama.cpp-b11815/llama-b11815/llama-server").expanduser(),
    "library_dirs": [Path("~/tools/llama.cpp-b11815").expanduser() / name for name in
                     ("llama-b11815", "cudart-llama-b11815-bin-ubuntu-cuda-13.4-x64", "extra-libs")],
    "model": Path(ic.wsl(ic.V["models_dir"]) + "/Qwen3.5-9B-Q4_K_M.gguf"),
    "port": 8199,
    "context": 2048,
    "ngl": 99,
    "vram_margin_mib": 512,
    "state_dir": Path("~/.cache/llama-wsl").expanduser(),
    "health_timeout": 120,
    "nvidia_smi": "/usr/lib/wsl/lib/nvidia-smi",
}
# ----------------


def gpu_memory():
    result = subprocess.run(
        [CONFIG["nvidia_smi"], "-i", "0", "--query-gpu=memory.free,memory.used",
         "--format=csv,noheader,nounits"],
        check=True, capture_output=True, text=True, encoding="utf-8", timeout=10)
    free, used = (float(value.strip()) for value in result.stdout.strip().split(","))
    if not all(math.isfinite(value) and value >= 0 for value in (free, used)):
        raise ValueError("nvidia-smi returned invalid memory readings")
    return {"free_mib": free, "used_mib": used}


def process_identity(pid):
    """Start time and boot ID prevent a stale PID from stopping a different process."""
    if pid <= 1:
        raise ValueError("invalid service PID")
    proc = Path("/proc") / str(pid)
    try:
        fields = (proc / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return {"pid": pid, "start_ticks": fields[19],
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip(),
                "exe": os.readlink(proc / "exe")}
    except FileNotFoundError:
        return None


def pid_path(port):
    return CONFIG["state_dir"] / f"{port}.pid"


@contextmanager
def port_lock(port):
    import fcntl  # Linux only; importing this module still works on Windows for unit tests.
    CONFIG["state_dir"].mkdir(parents=True, exist_ok=True)
    with (CONFIG["state_dir"] / f"{port}.lock").open("a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"端口 {port} 的另一个起停命令正在运行") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def start(port):
    if pid_path(port).exists():
        raise RuntimeError(f"已有 PID 文件 {pid_path(port)}，请先运行 stop；不会接管已有服务")
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(f"端口 {port} 已占用或无法绑定：{exc}") from exc
    if not CONFIG["server"].is_file() or not CONFIG["model"].is_file():
        raise RuntimeError("llama-server 或模型文件不存在，请检查顶部 CONFIG")
    memory = gpu_memory()
    margin_mib = CONFIG["vram_margin_mib"]
    required_mib = math.ceil(CONFIG["model"].stat().st_size / 1024 ** 2) + margin_mib
    if memory["free_mib"] < required_mib:
        raise RuntimeError(f"显存不足：空闲 {memory['free_mib']:.0f} MiB，"
                           f"需要模型大小 + {margin_mib} MiB = {required_mib} MiB")
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = ":".join(
        [str(path) for path in CONFIG["library_dirs"]] +
        ([env["LD_LIBRARY_PATH"]] if env.get("LD_LIBRARY_PATH") else []))
    env["CUDA_VISIBLE_DEVICES"] = "0"  # Match the GPU measured by nvidia-smi.
    command = [str(CONFIG["server"]), "-m", str(CONFIG["model"]), "-ngl", str(CONFIG["ngl"]),
               "-c", str(CONFIG["context"]), "-np", "1", "--device", "CUDA0",
               "--host", "127.0.0.1", "--port", str(port)]
    CONFIG["state_dir"].mkdir(parents=True, exist_ok=True)
    log = CONFIG["state_dir"] / f"{port}.log"
    with log.open("w", encoding="utf-8") as output:
        child = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                                 stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
    recorded = False
    try:
        identity = process_identity(child.pid)
        if identity is None:
            raise RuntimeError(f"llama-server 已退出，日志：{log}")
        with pid_path(port).open("x", encoding="utf-8") as handle:
            recorded = True
            json.dump(identity, handle)
        deadline = time.monotonic() + CONFIG["health_timeout"]
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError(f"llama-server 已退出（{child.returncode}），日志：{log}")
            try:
                with urlopen(f"http://127.0.0.1:{port}/health",
                             timeout=min(1, max(0.01, deadline - time.monotonic()))) as response:
                    health = json.load(response)
                    if isinstance(health, dict) and health.get("status") == "ok":
                        print(f"已启动 PID {child.pid}，http://127.0.0.1:{port}/health ok；日志：{log}")
                        return
            except (URLError, TimeoutError, ValueError):
                pass
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        raise RuntimeError(f"/health 等待 {CONFIG['health_timeout']} 秒超时，日志：{log}")
    except BaseException:
        # Only this Popen child is eligible for startup failure cleanup.
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        if recorded:
            pid_path(port).unlink(missing_ok=True)
        raise


def stop(port):
    path = pid_path(port)
    if not path.exists():
        print(f"没有 PID 文件 {path}，未停止任何进程")
        return
    saved = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(saved, dict):
        raise ValueError("invalid PID file; expected a process identity object")
    pid = saved["pid"]
    if not isinstance(pid, int) or pid <= 1:
        raise ValueError("invalid service PID")
    try:
        fd = os.pidfd_open(pid)  # Pin this process before checking identity; no PID reuse race.
    except ProcessLookupError:
        path.unlink()
        print(f"PID {pid} 已不存在，已清除陈旧 PID 文件")
        return
    try:
        current = process_identity(pid)
        if current is None:
            path.unlink()
            print(f"PID {pid} 已退出，已清除陈旧 PID 文件")
            return
        if current != saved or saved["exe"] != str(CONFIG["server"].resolve()):
            raise RuntimeError(f"PID {pid} 身份不符，未发送信号；保留 PID 文件供检查")
        signal.pidfd_send_signal(fd, signal.SIGTERM)
        if not select.select([fd], [], [], 10)[0]:
            signal.pidfd_send_signal(fd, signal.SIGKILL)
            if not select.select([fd], [], [], 5)[0]:
                raise RuntimeError(f"PID {pid} 尚未退出，保留 PID 文件")
    finally:
        os.close(fd)
    path.unlink()
    print(f"已停止 PID {pid}；已清除 {path}")


def bench(port):
    before = gpu_memory()
    prompt = ("<|im_start|>user\n写一段关于秋夜散步的中文短文，共200字。只输出正文。"
              "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n")
    # Exactly 200 Unicode characters, including punctuation; tokens are measured separately.
    grammar = "root ::= " + " ".join(["char"] * 200) + "\nchar ::= [一-鿿，。！？；：、]\n"
    request = Request(f"http://127.0.0.1:{port}/completion",
                      data=json.dumps({"prompt": prompt, "grammar": grammar, "n_predict": 512,
                                       "temperature": 0, "seed": 1, "cache_prompt": False,
                                       "stream": True}, ensure_ascii=False).encode("utf-8"),
                      headers={"Content-Type": "application/json"})
    started = time.perf_counter()
    first = None
    text = ""
    timings = {}
    complete = False
    with urlopen(request, timeout=120) as response:
        for line in response:
            if time.perf_counter() - started > 120:
                raise RuntimeError("bench 超过 120 秒")
            if not line.startswith(b"data: "):
                continue
            event = json.loads(line[6:].decode("utf-8"))
            if "error" in event:
                raise RuntimeError(f"bench 请求失败：{event['error']}")
            content = event.get("content", "")
            if content and first is None:
                first = time.perf_counter() - started
            text += content
            if event.get("stop"):
                timings = event.get("timings", {})
                complete = True
                break
    elapsed = time.perf_counter() - started
    if not complete or len(text) != 200 or first is None:
        raise RuntimeError(f"bench 未完成 200 字生成：收到 {len(text)} 字，stop={complete}")
    result = {"characters": len(text), "chars_per_second": round(len(text) / elapsed, 2),
              "first_char_seconds": round(first, 3), "total_seconds": round(elapsed, 3),
              "llama_tokens_per_second": timings.get("predicted_per_second"),
              "gpu_before": before, "gpu_after": gpu_memory(), "text": text}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop", "bench"))
    parser.add_argument("--port", type=int, default=CONFIG["port"])
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    if sys.platform != "linux":
        parser.error("Run this command inside WSL Linux with python3")
    try:
        if args.action == "bench":
            bench(args.port)
        else:
            with port_lock(args.port):
                {"start": start, "stop": stop}[args.action](args.port)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
