"""Local-only ASR. Windows callers use an isolated WSL worker, never a shell."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PureWindowsPath
import subprocess
import sys
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
MODEL_PATH = RUNTIME / "models" / "faster-whisper-large-v3-turbo"
MODEL_ID = "dropbox-dash/faster-whisper-large-v3-turbo"
VENV = RUNTIME / "asr-venv"
MAX_SECONDS = 1800
MAX_AUDIO_BYTES = 2 * 1024**3
_LOCK = threading.Lock()
DECODE_OPTIONS = {
    "task": "transcribe", "language": None, "multilingual": True,
    "beam_size": 5, "temperature": 0.0, "vad_filter": False,
    "condition_on_previous_text": False, "word_timestamps": False,
    "initial_prompt": None,
}


class ASRUnavailable(RuntimeError):
    """Local runtime or model is unavailable; original audio is unaffected."""


class ASRTranscriptionError(RuntimeError):
    """ASR did not complete. Never store its partial output as a final run."""


def _validate_audio(audio_path: Path) -> Path:
    path = Path(audio_path)
    if not path.is_absolute():
        raise ValueError("ASR requires an absolute path to a saved audio file.")
    path = path.resolve(strict=True)
    if not path.is_relative_to(ROOT) or not path.is_file():
        raise ValueError("ASR input must be a saved file inside incore.")
    size = path.stat().st_size
    if not 0 < size <= MAX_AUDIO_BYTES:
        raise ValueError("ASR accepts a nonempty saved audio file up to 2 GiB.")
    return path


def _wsl_path(path: Path) -> str:
    parsed = PureWindowsPath(str(path))
    if not parsed.drive or parsed.drive.startswith("\\\\") or len(parsed.drive) != 2:
        raise ASRUnavailable("ASR WSL bridge requires a local Windows drive path.")
    return "/mnt/" + parsed.drive[0].lower() + "/" + "/".join(parsed.parts[1:])


def whisper_runtime_status() -> dict:
    """Cheap readiness snapshot; never loads a model or connects to the network."""
    missing = [name for name in ("model.bin", "config.json", "tokenizer.json")
               if not (MODEL_PATH / name).is_file()]
    runtime_present = (VENV / "pyvenv.cfg").is_file()
    verified = (RUNTIME / "asr-verified.json").is_file()
    return {
        "available": runtime_present and not missing and verified,
        "runtime_present": runtime_present,
        "inference_verified": verified,
        "missing_model_files": missing,
        "model": MODEL_ID,
        "model_path": str(MODEL_PATH),
        "backend": "WSL Ubuntu / faster-whisper",
        "message": "Local ASR ready; no audio leaves this computer."
        if runtime_present and not missing and verified
        else "Local ASR is not verified yet; original audio remains saved.",
    }


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _coverage_gaps(segments: list[dict], duration: float) -> list[dict]:
    """Expose untranscribed spans as unreviewed, never infer they are silence."""
    gaps = []
    cursor = 0.0
    for segment in segments:
        start, end = max(0.0, segment["start"]), min(duration, segment["end"])
        if start - cursor > 0.5:
            gaps.append({"start": round(cursor, 3), "end": round(start, 3),
                         "status": "unreviewed"})
        cursor = max(cursor, end)
    if duration - cursor > 0.5:
        gaps.append({"start": round(cursor, 3), "end": round(duration, 3),
                     "status": "unreviewed"})
    return gaps


def _local_transcribe(audio_path: Path, on_progress=None) -> dict:
    try:
        import ctranslate2
        from faster_whisper import WhisperModel
    except (ImportError, OSError) as exc:
        raise ASRUnavailable("Local ASR dependencies unavailable in this interpreter.") from exc
    if any(not (MODEL_PATH / name).is_file()
           for name in ("model.bin", "config.json", "tokenizer.json")):
        raise ASRUnavailable("Local turbo model is not fully downloaded.")

    started = time.monotonic()
    source_hash = _sha256(audio_path)
    fallback_reason = None
    device, compute_type = "cuda", "float16"
    try:
        if "float16" not in ctranslate2.get_supported_compute_types("cuda"):
            raise RuntimeError("CUDA float16 is unavailable.")
        model = WhisperModel(str(MODEL_PATH), device=device, compute_type=compute_type,
                             local_files_only=True, cpu_threads=8, num_workers=1)
    except (RuntimeError, ValueError) as exc:
        fallback_reason = str(exc)[:500]
        device, compute_type = "cpu", "int8"
        try:
            model = WhisperModel(str(MODEL_PATH), device=device, compute_type=compute_type,
                                 local_files_only=True, cpu_threads=8, num_workers=1)
        except (RuntimeError, ValueError) as cpu_exc:
            raise ASRUnavailable("Neither local GPU nor CPU ASR could load the model.") from cpu_exc

    def decode():
        # ponytail: sequential library windows; no custom VAD or stitching engine.
        generated, info = model.transcribe(str(audio_path), **DECODE_OPTIONS)
        segments = []
        for segment in generated:
            if time.monotonic() - started > MAX_SECONDS:
                raise ASRTranscriptionError("Local ASR exceeded its 30-minute time limit.")
            segments.append({"start": round(float(segment.start), 3),
                             "end": round(float(segment.end), 3), "text": segment.text,
                             "seek_frame": int(segment.seek),
                             "avg_logprob": float(segment.avg_logprob),
                             "no_speech_prob": float(segment.no_speech_prob)})
            if on_progress:
                on_progress({"stage": "transcribing", "audio_seconds": segment.end,
                             "duration_seconds": info.duration, "device": device})
        return segments, info

    try:
        segments, info = decode()
    except RuntimeError as exc:
        # CUDA libraries may first be loaded lazily during actual inference.
        gpu_failure = any(term in str(exc).lower()
                          for term in ("cuda", "cublas", "cudnn", "out of memory"))
        if device != "cuda" or not gpu_failure:
            raise ASRTranscriptionError("Local ASR failed; original audio is unchanged.") from exc
        fallback_reason = str(exc)[:500]
        del model
        device, compute_type = "cpu", "int8"
        model = WhisperModel(str(MODEL_PATH), device=device, compute_type=compute_type,
                             local_files_only=True, cpu_threads=8, num_workers=1)
        segments, info = decode()
    except (ValueError, OSError) as exc:
        raise ASRTranscriptionError("Audio could not be decoded locally.") from exc

    if _sha256(audio_path) != source_hash:
        raise ASRTranscriptionError("Source audio changed during transcription; result rejected.")
    duration = float(info.duration)
    gaps = _coverage_gaps(segments, duration)
    model_record = {}
    if (MODEL_PATH / "download-manifest.json").is_file():
        model_record = json.loads((MODEL_PATH / "download-manifest.json").read_text())
    return {
        "text": "".join(segment["text"] for segment in segments).strip(),
        "segments": segments,
        "model": MODEL_ID,
        "version": model_record.get("revision", "local-unversioned"),
        "metadata": {
            "engine": "faster-whisper", "device": device, "compute_type": compute_type,
            "cpu_fallback_reason": fallback_reason,
            "faster_whisper_version": importlib.metadata.version("faster-whisper"),
            "ctranslate2_version": importlib.metadata.version("ctranslate2"),
            "duration_seconds": duration, "wall_seconds": round(time.monotonic() - started, 3),
            "language": info.language, "language_probability": info.language_probability,
            "language_probability_scope": "initial language detection, not transcript accuracy",
            "initial_language_candidates": info.all_language_probs[:5] if info.all_language_probs else [],
            "decoding_profile": "mixed-language-auto-v2",
            "decoding_options": dict(DECODE_OPTIONS),
            "source_sha256": source_hash, "model_sha256": model_record.get("model_sha256"),
            "task": "transcribe", "vad_filter": False, "beam_size": 5,
            "window_seconds": 30, "coverage_gaps": gaps,
            "emitted_decoder_seek_frames": sorted({s["seek_frame"] for s in segments}),
            "review_required": True, "audio_uploaded": False,
            "warning": "ASR is an unreviewed draft. Check missing spans, numbers and names.",
        },
    }


def whisper_transcribe(audio_path: Path, on_progress=None) -> dict:
    """Transcribe a saved original, using local GPU or an explicit local CPU fallback.

    Returns JSON-compatible text/segments/model/version/metadata. No original or
    correction is overwritten. Storage and immutable ASR-run persistence belong
    to the caller. At most one job runs per server process.
    """
    path = _validate_audio(audio_path)
    with _LOCK:
        if on_progress:
            on_progress({"stage": "loading", "model": MODEL_ID})
        if os.name != "nt":
            return _local_transcribe(path, on_progress)
        if not (VENV / "pyvenv.cfg").is_file():
            raise ASRUnavailable("Isolated WSL ASR runtime is not installed.")
        venv = _wsl_path(VENV)
        libraries = ":".join(venv + "/lib/python3.12/site-packages/nvidia/" + part + "/lib"
                             for part in ("cublas", "cudnn")) + ":/usr/lib/wsl/lib"
        command = ["wsl.exe", "-d", "Ubuntu", "--", "/usr/bin/timeout", "--kill-after=10s",
                   str(MAX_SECONDS), "/usr/bin/env", "HF_HUB_OFFLINE=1",
                   "HF_HUB_DISABLE_IMPLICIT_TOKEN=1", "HF_HUB_DISABLE_TELEMETRY=1",
                   "LD_LIBRARY_PATH=" + libraries, venv + "/bin/python",
                   _wsl_path(Path(__file__).resolve()), "--worker", _wsl_path(path)]
        try:
            completed = subprocess.run(command, capture_output=True, encoding="utf-8",
                                       errors="replace", timeout=MAX_SECONDS + 20,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except FileNotFoundError as exc:
            raise ASRUnavailable("WSL is not available on this computer.") from exc
        except subprocess.TimeoutExpired as exc:
            raise ASRTranscriptionError("Local ASR timed out; original audio remains saved.") from exc
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            message = "Local ASR worker failed; original audio remains saved."
            if completed.returncode in (124, 137):
                message = "Local ASR exceeded its 30-minute time limit."
            raise ASRTranscriptionError(message) from exc
        if completed.returncode or "error" in result:
            error_class = ASRUnavailable if result.get("kind") == "unavailable" else ASRTranscriptionError
            raise error_class(result.get("error", "Local ASR worker failed."))
        if not isinstance(result.get("text"), str) or not isinstance(result.get("segments"), list):
            raise ASRTranscriptionError("Local ASR returned an invalid result.")
        if on_progress:
            on_progress({"stage": "complete", "duration_seconds": result["metadata"]["duration_seconds"]})
        return result


def runtime_status() -> dict:
    from .nano import status
    return status()


def transcribe(audio_path: Path, on_progress=None) -> dict:
    from .nano import transcribe as nano_transcribe
    return nano_transcribe(audio_path, on_progress)


def _self_test():
    assert _coverage_gaps([{"start": 2, "end": 4}, {"start": 3, "end": 5}], 7) == [
        {"start": 0.0, "end": 2, "status": "unreviewed"},
        {"start": 5, "end": 7, "status": "unreviewed"}]
    assert _wsl_path(Path(r"C:\audio folder\test.wav")) == "/mnt\x2fc/audio folder/test.wav"
    try:
        _validate_audio(Path("relative.wav"))
    except ValueError:
        pass
    else:
        raise AssertionError("relative input accepted")
    print("ASR offline self-check passed")


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        _self_test()
    elif sys.argv[1:] == ["--status"]:
        print(json.dumps(runtime_status(), ensure_ascii=False))
    elif len(sys.argv) == 3 and sys.argv[1] == "--worker":
        try:
            print(json.dumps(transcribe(Path(sys.argv[2])), ensure_ascii=False))
        except Exception as error:
            print(json.dumps({"error": str(error), "kind": "unavailable"
                              if isinstance(error, ASRUnavailable) else "transcription"}))
            sys.exit(1)
    else:
        raise SystemExit("Use --status, --self-test, or --worker ABSOLUTE_SAVED_AUDIO_PATH")
