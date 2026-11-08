"""Optional LLM post-processing of one transcript through a local OpenAI-compatible endpoint (llama-server).

Settings live in <data>/llm-postprocess.json (absent = everything off). The result is appended as a new revision
(kind postprocess, source llm_postprocess); the original and earlier revisions are never touched. A failure writes nothing.
"""
from __future__ import annotations

import atexit
import base64
import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .store import InputError, InputStore
import sys as _sys, pathlib as _pl  # noqa: E402
_sys.path.append(str(_pl.Path(__file__).resolve().parent.parent / "control"))
import instance_config as ic  # noqa: E402  (this machine's paths; defaults = today's values)

SETTINGS_FILE = "llm-postprocess.json"
DEFAULTS: dict[str, Any] = {
    "enabled": False,  # feature switch; edit the file to turn on
    "endpoint": "http://127.0.0.1:8086/v1/chat/completions",
    "model": "qwen3-0.6b",
    "prompt_path": ic.V["models_dir"] + r"\prompt-v1.txt",
    "timeout_s": 60,
    "auto": False,  # the「新转写默认润色」checkbox
    # Both set = the desk starts llama-server itself on the first「润色」click (CPU, 127.0.0.1, port from endpoint) and stops it on exit.
    # Empty = the endpoint must already be running.
    "server_exe": "",
    "model_path": "",
}
SOURCE = "llm_postprocess"
SERVER_PID_FILE = "llm-server.json"
_server: subprocess.Popen | None = None  # the llama-server this desk started
_server_root: Path | None = None  # data directory holding SERVER_PID_FILE, so the atexit stop can remove it
_server_lock = threading.Lock()


class LLMError(Exception):
    pass


def load_settings(root: Path) -> dict[str, Any]:
    path = Path(root) / SETTINGS_FILE
    out = dict(DEFAULTS)
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return out
    except (OSError, ValueError):
        raise LLMError(f"{SETTINGS_FILE} 无法读取或不是有效 JSON。") from None
    if isinstance(value, dict):
        out.update({k: value[k] for k in DEFAULTS if k in value})
    return out


def set_auto(root: Path, auto: bool) -> dict[str, Any]:
    settings = load_settings(root)
    settings["auto"] = bool(auto)
    (Path(root) / SETTINGS_FILE).write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    return settings


def _healthy(endpoint: str) -> bool:
    parts = urlparse(endpoint)
    try:
        with urllib.request.urlopen(f"{parts.scheme}://{parts.netloc}/health", timeout=2):
            return True
    except (OSError, ValueError):
        return False


def stop_server(root: Path | None = None) -> None:
    global _server, _server_root
    with _server_lock:
        proc, _server = _server, None
        root = root or _server_root
    if proc is not None and proc.poll() is None:
        proc.kill()
    if root is not None:
        (Path(root) / SERVER_PID_FILE).unlink(missing_ok=True)


def _process_args(pid: int) -> list[str] | None:
    if os.name == "nt":
        script = ("$p=Get-CimInstance Win32_Process -Filter 'ProcessId = %d' -ErrorAction SilentlyContinue; "
                  "if ($p) { [Console]::WriteLine([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes([string]$p.CommandLine))) }") % pid
        try:
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                    capture_output=True, encoding="ascii", errors="replace", timeout=5,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            command = base64.b64decode(result.stdout.strip(), validate=True).decode("utf-8") if result.returncode == 0 else ""
            return shlex.split(command, posix=False) if command else None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return [os.fsdecode(arg) for arg in command.split(b"\0") if arg] or None


def cleanup_orphan_server(root: Path) -> None:
    """Stop only the recorded llama-server when its command line still matches its recorded port."""
    path = Path(root) / SERVER_PID_FILE
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        value = None

    pid = value.get("pid") if isinstance(value, dict) else None
    port = value.get("port") if isinstance(value, dict) else None
    if (isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
            and isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535):
        args = _process_args(pid)
        if args:
            executable = re.split(r"[\\/]", args[0].strip("\"'"))[-1].casefold()
            command_ports = []
            for index, arg in enumerate(args):
                if arg == "--port" and index + 1 < len(args):
                    command_ports.append(args[index + 1])
                elif arg.startswith("--port="):
                    command_ports.append(arg.partition("=")[2])
            if executable in {"llama-server", "llama-server.exe"} and command_ports and command_ports[-1] == str(port):
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except OSError:
                    return  # Keep the marker so a later startup can retry.
    path.unlink(missing_ok=True)


def ensure_server(root: Path, settings: dict[str, Any]) -> None:
    """Start llama-server (CPU, loopback only) if the settings name one and the endpoint is not already answering."""
    global _server, _server_root
    exe, model = str(settings["server_exe"]), str(settings["model_path"])
    if not exe or not model or _healthy(str(settings["endpoint"])):
        return
    parts = urlparse(str(settings["endpoint"]))
    if parts.hostname not in ("127.0.0.1", "localhost") or not parts.port:
        raise LLMError("润色端点必须是本机 127.0.0.1 上的端口，输入台才会自己启动模型。")
    if not Path(exe).is_file() or not Path(model).is_file():
        raise LLMError(f"模型或 llama-server 文件不存在：{model}；没有写入新版本。")
    with _server_lock:
        if _server is None or _server.poll() is not None:
            _server = subprocess.Popen(
                [exe, "-m", model, "-ngl", "0", "-t", str(min(12, os.cpu_count() or 4)), "-c", "4096", "--host", "127.0.0.1",
                 "--port", str(parts.port), "--no-webui"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(Path(exe).parent),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            atexit.register(stop_server)  # a hard kill skips this; startup reaps the recorded process next time
            _server_root = Path(root)
            (Path(root) / SERVER_PID_FILE).write_text(json.dumps({"pid": _server.pid, "port": parts.port, "model": model}), encoding="utf-8")
        proc = _server
    for _ in range(120):
        if _healthy(str(settings["endpoint"])):
            return
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    stop_server(root)
    raise LLMError("本地模型没有启动成功；没有写入新版本。")


CHUNK_CHARS = 1200  # owner\x2011-02: a 17-min transcript (4030 chars) came back cut at max_tokens; send long text in pieces
MAX_REPEAT_RETRIES = 2
REPEAT_RETRY_SETTINGS = ((1.1, 64), (1.2, 128))


def split_text(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    """Pieces of at most `limit` characters, cut after a sentence end; one over-long sentence stays whole."""
    pieces, current = [], ""
    for sentence in re.findall(r"[^。！？!?]*[。！？!?]+|[^。！？!?]+$", text):
        if current and len(current) + len(sentence) > limit:
            pieces.append(current)
            current = ""
        current += sentence
    return pieces + [current] if current else pieces


def clean_text(text: str, settings: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Polish each piece, splitting length-limited text and marking only persistent repetition fallbacks."""
    prompt_path = Path(str(settings["prompt_path"]))
    try:
        prompt = prompt_path.read_text(encoding="utf-8").strip()
    except OSError:
        raise LLMError(f"提示词文件不存在或无法读取：{prompt_path}") from None
    if not prompt:
        raise LLMError(f"提示词文件是空的：{prompt_path}")
    pieces = split_text(text) or [text]
    cleaned, unpolished = [], []
    repeat_count = retry_recovered = length_splits = sentence_splits = 0
    for index, piece in enumerate(pieces, 1):
        polished, fallback, repeats, recovered, piece_splits, piece_sentence_splits = _clean_piece_with_splits(
            piece, prompt, settings, seam=index > 1)
        cleaned.append(polished)
        unpolished.extend(fallback)
        repeat_count += repeats
        retry_recovered += recovered
        length_splits += piece_splits
        sentence_splits += piece_sentence_splits
    return "".join(cleaned), {
        "polish_unpolished_sentences": unpolished,
        "polish_unpolished_sentence_count": len(unpolished),
        "polish_repeat_count": repeat_count,
        "polish_retry_recovered": retry_recovered,
        "polish_length_split_count": length_splits,
        "polish_repeat_sentence_split_count": sentence_splits,
        "polish_piece_count": len(pieces),
    }


def _split_piece_at_boundary(text: str) -> tuple[str, str] | None:
    """Bisect at the nearest boundary, falling back to the character midpoint."""
    if len(text) < 2:
        return None
    first = next((index for index, char in enumerate(text) if not char.isspace()), len(text))
    last = next((index for index in range(len(text) - 1, -1, -1) if not text[index].isspace()), -1)
    positions = [match.end() for match in re.finditer(r"[。！？!?；;，,、：:.!…]+|\r?\n", text)
                 if first < match.end() <= last]
    split_at = min(positions, key=lambda position: abs(position - len(text) / 2)) if positions else len(text) // 2
    if split_at <= 0 or split_at >= len(text):
        return None
    return text[:split_at], text[split_at:]


def _split_piece_at_sentence_boundary(text: str) -> tuple[str, str] | None:
    positions = [match.end() for match in re.finditer(r"[。！？!?]+", text) if text[match.end():].strip()]
    if not positions:
        return None
    split_at = min(positions, key=lambda position: abs(position - len(text) / 2))
    return text[:split_at], text[split_at:]


def _clean_piece_with_splits(
    text: str, prompt: str, settings: dict[str, Any], seam: bool = False
) -> tuple[str, list[str], int, int, int, int]:
    repeat_count = 0
    for retry in range(MAX_REPEAT_RETRIES + 1):
        out, cut = _clean_piece(text, prompt, settings, retry)
        if _is_repeated_output(text, out):
            repeat_count += 1
            if retry < MAX_REPEAT_RETRIES:
                continue
            halves = _split_piece_at_sentence_boundary(text)
            if halves:
                left, left_fallback, left_repeats, left_recovered, left_splits, left_sentence_splits = _clean_piece_with_splits(
                    halves[0], prompt, settings, seam)
                right, right_fallback, right_repeats, right_recovered, right_splits, right_sentence_splits = _clean_piece_with_splits(
                    halves[1], prompt, settings, True)
                return (left + right, left_fallback + right_fallback,
                        repeat_count + left_repeats + right_repeats, left_recovered + right_recovered,
                        left_splits + right_splits, 1 + left_sentence_splits + right_sentence_splits)
            return text, [text], repeat_count, 0, 0, 0
        if cut:
            halves = _split_piece_at_boundary(text)
            if halves:
                left, left_fallback, left_repeats, left_recovered, left_splits, left_sentence_splits = _clean_piece_with_splits(
                    halves[0], prompt, settings, seam)
                right, right_fallback, right_repeats, right_recovered, right_splits, right_sentence_splits = _clean_piece_with_splits(
                    halves[1], prompt, settings, True)
                return (left + right, left_fallback + right_fallback,
                        repeat_count + left_repeats + right_repeats, left_recovered + right_recovered,
                        1 + left_splits + right_splits, left_sentence_splits + right_sentence_splits)
            if out:
                return out, [], repeat_count, 0, 0, 0
            raise LLMError("本地模型返回了空文字；没有写入新版本。")
        leading_space = text[:len(text) - len(text.lstrip(" \t"))] if seam else ""
        return leading_space + out, [], repeat_count, int(repeat_count > 0 and retry > 0), 0, 0
    raise AssertionError("retry loop must return or split")


def _is_repeated_output(original: str, output: str) -> bool:
    compact_original = "".join(char for char in original if not char.isspace())
    compact_output = "".join(char for char in output if not char.isspace())
    if len(compact_output) > max(len(compact_original) * 2, len(compact_original) + 200):
        return True
    if compact_output == compact_original:
        return False
    for width in range(min(128, len(compact_output) // 2), 11, -1):
        if any(compact_output[index:index + width] == compact_output[index + width:index + 2 * width]
               for index in range(len(compact_output) - 2 * width + 1)):
            return True
    return False


def _clean_piece(text: str, prompt: str, settings: dict[str, Any], retry: int = 0) -> tuple[str, bool]:
    body_data = {
        "model": settings["model"],
        "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": text}],
        "temperature": 0,
        "max_tokens": 1500,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if retry:
        body_data["repeat_penalty"], body_data["repeat_last_n"] = REPEAT_RETRY_SETTINGS[retry - 1]
    body = json.dumps(body_data).encode("utf-8")
    request = urllib.request.Request(str(settings["endpoint"]), data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=float(settings["timeout_s"])) as response:
            payload = json.loads(response.read().decode("utf-8"))
        out = payload["choices"][0]["message"]["content"]
        cut = payload["choices"][0].get("finish_reason") == "length"
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        raise LLMError("本地模型没有应答（超时或端点未启动）；没有写入新版本。") from None
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise LLMError("本地模型返回的格式不对；没有写入新版本。") from None
    out = re.sub(r"<think>.*?</think>", "", str(out), flags=re.S).strip()
    if not out and not cut:
        raise LLMError("本地模型返回了空文字；没有写入新版本。")
    return out, cut


def coverage_info(original: str, polished: str) -> dict[str, Any]:
    """Cheap hints, not a semantic check: count non-space chars and look for a final-sentence fragment."""
    original_chars = sum(not char.isspace() for char in original)
    ratio = sum(not char.isspace() for char in polished) / original_chars if original_chars else 1.0
    normalize = lambda text: "".join(char for char in text.casefold() if char.isalnum())
    sentences = [normalize(part) for part in re.split(r"[。！？.!?]+", original)]
    # ponytail: a repeated phrase can hide a missing ending; these are warning signals, not proof of completeness.
    last = next((part for part in reversed(sentences) if part), "")[-40:]
    tail = normalize(polished[-300:])
    size = min(4, len(last))
    found = not last or any(last[i:i + size] in tail for i in range(len(last) - size + 1))
    return {"polish_length_ratio": ratio, "polish_tail_found": found}


def run(store: InputStore, session_id: str, settings: dict[str, Any] | None = None, revision_id: str | None = None) -> dict[str, Any]:
    """Blocking. Returns the new revision, or raises InputError (nothing written).

    revision_id = the version the editor is showing (already saved): that text goes to the model. Without it (the automatic run
    after a transcription) the source is the rules version of the transcript. A verified record can be polished again: the result is a
    new unverified version and the old verification stays."""
    settings = settings or load_settings(store.root)
    if not settings["enabled"]:
        raise InputError(409, f"本地润色未启用：把 {SETTINGS_FILE} 里的 enabled 设为 true。")
    detail = store.session_detail(session_id)
    if revision_id:
        source = next((rev for rev in detail["revisions"] if rev["id"] == revision_id), None)
        if source is None or source["kind"] == "instruction":
            raise InputError(409, "这个版本不属于这条记录，或不是文字版本，不能润色。")
    else:
        source = next((rev for rev in detail["revisions"] if rev["kind"] in ("asr", "original_text")), None)
        if source is None:
            raise InputError(409, "还没有可处理的转写文字。")
        # The model gets the same text the desk shows as the rules version (the asr_postprocess revision); no rules revision = the raw transcript.
        source = next((rev for rev in detail["revisions"] if rev["source"] == "asr_postprocess" and rev["parent_id"] == source["id"]), source)
    try:
        ensure_server(store.root, settings)
        text, piece_info = clean_text(source["content"], settings)
    except LLMError as exc:
        raise InputError(502, str(exc)) from None
    return store.append_revision(session_id, "postprocess", text, source["id"], SOURCE,
                                 {"model": settings["model"], "endpoint": settings["endpoint"],
                                  "prompt_path": settings["prompt_path"], "human_verified": False, "review_required": True,
                                  **coverage_info(source["content"], text), **piece_info})
