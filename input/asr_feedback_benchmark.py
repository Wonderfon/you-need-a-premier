"""Bounded local A/B diagnosis of a saved recording; never writes to input DB.

Run with the isolated WSL ASR Python and its CUDA library paths. Private comparison
outputs stay under .runtime; no audio is uploaded and no model is downloaded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import time

import av
import numpy as np
from faster_whisper import WhisperModel
from faster_whisper.audio import decode_audio

ROOT = Path(__file__).resolve().parents[1]
GLOSSARY_PROMPT = "简体中文与 English 混合口述。Claude Code、ChatGPT、Codex、Opus、Sonnet、Whisper；额度、订阅、重置。"


def checksum(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def inspect_audio(path):
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        codec = stream.codec_context
        stats = {"container": container.format.name, "codec": codec.name,
                 "source_sample_rate": codec.sample_rate, "channels": len(codec.layout.channels)}
        previous_end = None
        gaps = []
        frame_count = 0
        first_time = last_end = None
        for frame in container.decode(stream):
            frame_count += 1
            if frame.time is not None:
                start = float(frame.time)
                end = start + frame.samples / frame.sample_rate
                first_time = start if first_time is None else first_time
                if previous_end is not None and start - previous_end > 0.04:
                    gaps.append({"start": previous_end, "end": start})
                previous_end = last_end = end
        stats.update({"decoded_frames": frame_count, "first_frame_seconds": first_time,
                      "last_frame_end_seconds": last_end, "timestamp_gaps_over_40ms": gaps})
    samples = decode_audio(str(path), sampling_rate=16000)
    window = 320
    frames = samples[:len(samples) // window * window].reshape(-1, window)
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
    stats.update({
        "asr_sample_count": len(samples), "asr_duration_seconds": len(samples) / 16000,
        "peak_abs": float(np.max(np.abs(samples))),
        "whole_rms_dbfs": float(20 * np.log10(max(1e-12, np.sqrt(np.mean(samples.astype(np.float64) ** 2))))),
        "near_full_scale_fraction": float(np.mean(np.abs(samples) >= 0.999)),
        "frames_below_minus60db_fraction": float(np.mean(rms < 0.001)),
        "frame_rms_dbfs_percentiles_10_50_90": [float(x) for x in 20 * np.log10(np.maximum(np.percentile(rms, [10, 50, 90]), 1e-12))],
        "microphone_identity": None, "browser_audio_processing_settings": None,
        "note": "Signal levels and timestamp gaps are objective diagnostics, not speech accuracy or SNR scores.",
    })
    return stats


def run(path: Path, output: Path):
    path = path.resolve(strict=True)
    output = output.resolve()
    if not path.is_relative_to(ROOT / "input/data/sessions"):
        raise ValueError("Expected a saved original inside input/data/sessions.")
    if not output.is_relative_to(ROOT / ".runtime"):
        raise ValueError("Private diagnostics must stay under .runtime.")
    original_hash = checksum(path)
    output.mkdir(parents=True, exist_ok=False)
    audio = inspect_audio(path)
    (output / "audio-diagnostics.json").write_text(json.dumps(audio, indent=2), encoding="utf-8")
    model = WhisperModel(str(ROOT / ".runtime/models/faster-whisper-large-v3-turbo"),
                         device="cuda", compute_type="float16", local_files_only=True,
                         cpu_threads=8, num_workers=1)
    groups = [
        ("A_original_auto", {"multilingual": False}),
        ("B_segment_language", {"multilingual": True}),
        ("C_segment_language_glossary", {"multilingual": True, "initial_prompt": GLOSSARY_PROMPT}),
    ]
    summary = []
    for name, extra in groups:
        started = time.monotonic()
        options = {"task": "transcribe", "language": None, "beam_size": 5, "temperature": 0.0,
                   "vad_filter": False, "condition_on_previous_text": False, "word_timestamps": False,
                   **extra}
        generated, info = model.transcribe(str(path), **options)
        segments = [{"start": float(s.start), "end": float(s.end), "text": s.text,
                     "avg_logprob": float(s.avg_logprob), "no_speech_prob": float(s.no_speech_prob),
                     "compression_ratio": float(s.compression_ratio), "seek_frame": int(s.seek)}
                    for s in generated]
        text = "".join(s["text"] for s in segments).strip()
        result = {"group": name, "source_sha256": original_hash, "options": options,
                  "text": text, "segments": segments, "language": info.language,
                  "language_probability": float(info.language_probability),
                  "top_language_probabilities": info.all_language_probs[:5] if info.all_language_probs else [],
                  "duration_seconds": float(info.duration), "wall_seconds": round(time.monotonic() - started, 3),
                  "human_reference_available": False, "wer": None, "cer": None,
                  "audio_uploaded": False}
        (output / (name + ".json")).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        summary.append({"group": name, "language": info.language,
                        "language_probability": result["language_probability"],
                        "characters": len(text), "chinese_characters": len(re.findall(r"[\u4e00-\u9fff]", text)),
                        "latin_words": re.findall(r"[A-Za-z]+", text),
                        "segments": len(segments), "wall_seconds": result["wall_seconds"],
                        "mean_segment_logprob": float(np.mean([s["avg_logprob"] for s in segments])) if segments else None})
        print(json.dumps({"group": name, "completed": True, "wall_seconds": result["wall_seconds"]}), flush=True)
    assert checksum(path) == original_hash, "Original changed during diagnostics"
    (output / "summary.json").write_text(json.dumps({"audio": audio, "comparisons": summary,
                                                    "original_sha256": original_hash}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"audio": audio, "comparisons": summary}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    run(args.audio, args.output)
