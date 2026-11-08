"""Serve the demo: dashboard (/dashboard), the owner to-do page (/todo) and the input station (/) from synthetic data.

    python demo/run_demo.py          (then open http://127.0.0.1:18765/dashboard)
Uses its own folders under demo/ only: nothing outside this repository is read or written.
Run from the repository root; the repository folder name is the package name (clone it as `incore`)."""
import importlib
import sys
import wave
from pathlib import Path

HERE = Path(__file__).absolute().parent
REPO = HERE.parent
PKG = REPO.name
sys.path.insert(0, str(REPO.parent))
sys.path.insert(0, str(REPO / "control"))
PORT = 18765

QUOTES = ["Remember to compare the two schedules before Friday.",
          "The demo owner wants the daily report on one screen.",
          "Idea: show the restore checklist next to the backup status."]


def seed(store):
    """Three fake quotes as text sessions and one silent one-second recording with a fake transcript."""
    if store.list_sessions():
        return
    for i, q in enumerate(QUOTES, 1):
        store.create_text_session(f"Demo note {i}", q)
    wav = HERE / "silence.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * 16000)
    data = wav.read_bytes()
    s = store.create_audio_session("audio_upload", "Demo recording (silent placeholder)", "audio/wav", "demo.wav")
    store.put_chunk(s["id"], 0, data, importlib.import_module(f"{PKG}.input.store").sha256_bytes(data))
    store.finalize_audio(s["id"], 1, len(data))
    store.append_asr_result(s["id"], {"text": "This is a made-up transcript for the placeholder recording."})
    store.set_status(s["id"], "transcribed")  # nothing is left queued for an ASR worker


def main():
    from aiohttp import web
    server = importlib.import_module(f"{PKG}.input.server")
    for sub in ("data", "wt"):
        (HERE / sub).mkdir(exist_ok=True)
    seed(importlib.import_module(f"{PKG}.input.store").InputStore(HERE / "input-data"))  # before the app: no ASR worker runs on it
    app = server.create_app(HERE / "input-data", expected_port=PORT, dashboard_root=HERE, pending_dir=HERE / "data",
                            wt_root=HERE / "wt")
    web.run_app(app, host="127.0.0.1", port=PORT, access_log=None)


if __name__ == "__main__":
    main()
