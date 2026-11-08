"""One local Nano process per input server; stdin EOF releases the GPU."""
from __future__ import annotations
import atexit
import json
import os
from pathlib import Path
import queue
import struct
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / '.runtime/nano-quant-benchmark/Fun-ASR-Nano-2512-Q5_K_M.gguf'
MODEL_SHA256 = '68de7b48ba46b739254fb57486edf7023943cb0af63767ab795d58d6e482096c'
VENV = ROOT / '.runtime/quant-gpu-venv'
MODEL_ID = 'Fun-ASR-Nano-2512-Q5_K_M.gguf'
_lock = threading.Lock()
_lifecycle = threading.RLock()
_closing = False
_process = None
_responses = None
_state = {'state': 'stopped', 'ready': False}


def status():
    if _process and _process.poll() is not None:
        _state.update(state='failed', ready=False, error='Local worker exited')
    return {'available': MODEL.is_file() and (VENV/'pyvenv.cfg').is_file(),
            'model': MODEL_ID, 'backend': 'Windows / transcribe.cpp Q5 persistent RTX Vulkan',
            'audio_uploaded': False, **_state}


def _stop():
    global _process, _responses
    process, _process = _process, None
    _responses = None
    if process:
        try:
            process.stdin.close()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait(timeout=10)
    _state.update(state='stopped', ready=False)


def shutdown():
    global _closing
    with _lifecycle:
        _closing = True
        _stop()


atexit.register(shutdown)


def _read_lines(process, responses):
    try:
        for line in process.stdout:
            try:
                responses.put(json.loads(line))
            except ValueError:
                responses.put({'error': 'Invalid local ASR protocol'})
    finally:
        responses.put({'error': 'Local ASR process ended'})


def _receive(timeout, responses):
    from .transcribe import ASRTranscriptionError
    try:
        value = responses.get(timeout=timeout)
    except queue.Empty:
        _stop()
        raise ASRTranscriptionError('Local Nano timed out; original audio is saved.')
    if 'error' in value:
        _stop()
        _state.update(state='failed', error=value['error'])
        raise ASRTranscriptionError(value['error'])
    return value


def _ensure():
    global _process, _responses
    from .transcribe import _wsl_path, ASRUnavailable
    if _process and _process.poll() is None and _state.get('ready'):
        return
    _stop()
    if not status()['available']:
        raise ASRUnavailable('Local Nano weights/runtime are missing.')
    _state.update(state='loading', ready=False)
    command = [str(VENV/'Scripts/python.exe'), str(Path(__file__).resolve()), '--worker']
    # Model library diagnostics are discarded, never mixed with private result JSON.
    with _lifecycle:
        if _closing:
            raise ASRUnavailable('Input server is shutting down.')
        _process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        responses = _responses = queue.Queue()
        threading.Thread(target=_read_lines, args=(_process, responses), daemon=True).start()
    ready = _receive(240, responses)
    if ready.get('state') != 'ready':
        _stop()
        raise ASRUnavailable('Nano did not become ready.')
    with _lifecycle:
        if _closing:
            _stop()
            raise ASRUnavailable('Input server is shutting down.')
        _state.clear()
        _state.update(ready, ready=True)


def preload():
    with _lock:
        _ensure()
    return status()


def transcribe(path, on_progress=None):
    from .transcribe import _validate_audio, _wsl_path
    path = _validate_audio(path)
    with _lock:
        _ensure()
        if on_progress:
            on_progress({'stage': 'transcribing', 'model': MODEL_ID})
        with _lifecycle:
            process, responses = _process, _responses
            if _closing or process is None:
                raise RuntimeError('Input server is shutting down.')
        try:
            process.stdin.write(json.dumps({'path': str(path)})+'\n')
            process.stdin.flush()
        except (OSError, ValueError):
            _stop()
            raise RuntimeError('Local Nano disconnected; retry saved audio.')
        return _receive(1800, responses)


VAD_MIN_SILENCE_MS = 200  # shortest Silero pause that may end a window (100-300 ms measured the same)


def _pcm16k(path):
    import av
    import numpy as np
    parts = []
    with av.open(str(path)) as container:
        resampler = av.AudioResampler(format='s16', layout='mono', rate=16000)
        def frames():
            for frame in container.decode(audio=0):
                yield from resampler.resample(frame)
            yield from resampler.resample(None)
        for frame in frames():
            # Match the validated Q5 benchmark's PCM16 WAV normalization exactly.
            samples = frame.to_ndarray().reshape(-1).astype(np.float32)/32768
            parts.append((np.clip(samples, -1, 1)*32767).astype('<i2').astype(np.float32)/32768)
    return np.concatenate(parts) if parts else np.empty(0, dtype=np.float32)


def _pause_cuts(speech, total, limit):
    """Cut points (samples) so no piece exceeds `limit`: each piece ends mid-pause, at the last pause
    between `speech` spans that fits. No pause fits (or no speech) means a hard cut at `limit`."""
    gaps = [((a['end']+b['start'])//2, a['end']) for a, b in zip(speech, speech[1:])]
    cuts, start = [], 0
    while total-start > limit:
        fit = [min(mid, start+limit) for mid, lo in gaps if start < lo <= start+limit]
        cuts.append(fit[-1] if fit else start+limit)
        start = cuts[-1]
    return cuts


def _decoded_windows(path, seconds=30):
    # ponytail: whole recording in memory (~64 KB/s); windows end at Silero pauses, so no audio is dropped.
    pcm = _pcm16k(path)
    speech = []
    if len(pcm) > seconds*16000:
        from faster_whisper.vad import VadOptions, get_speech_timestamps
        speech = get_speech_timestamps(pcm, VadOptions(min_silence_duration_ms=VAD_MIN_SILENCE_MS,
                                                       max_speech_duration_s=seconds))
    cuts = _pause_cuts(speech, len(pcm), seconds*16000)
    for a, b in zip([0, *cuts], [*cuts, len(pcm)]):
        if b > a:
            yield pcm[a:b]


def _audio_windows(path):
    from array import array
    # Reuse installed WSL PyAV, decoding bounded windows; no new dependency.
    def linux(p):
        return '/mnt/' + p.drive[0].lower() + '/' + '/'.join(p.parts[1:])
    decoder = subprocess.Popen(['wsl.exe', '-d', 'Ubuntu', '--', '/usr/bin/timeout', '1800',
        linux(ROOT/'.runtime/asr-venv/bin/python'), linux(Path(__file__).resolve()),
        '--decode', linux(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        if decoder.stdout.read(8) != b'PCM16K2\n':
            raise RuntimeError('Local WSL audio decoder did not start')
        while header := decoder.stdout.read(4):   # each window: uint32 sample count, then float32 samples
            size = struct.unpack('<I', header)[0]*4
            data = decoder.stdout.read(size)
            if len(data) != size:
                raise RuntimeError('Local audio decoding failed')
            samples = array('f')
            samples.frombytes(data)
            yield samples
        if decoder.wait(timeout=10):
            raise RuntimeError('Local audio decoding failed')
    finally:
        if decoder.poll() is None:
            decoder.kill()
            decoder.wait()


def _run_window(run, audio, depth=3):
    """[(audio piece, result)] for one window. A window whose decode hits the Q5 generation cap (OutputTruncated, seen about once
    per 1200 windows) is halved and each half retried, up to `depth` times; any other error, or still truncated, propagates."""
    try:
        return [(audio, run(audio))]
    except Exception as exc:
        if type(exc).__name__ != 'OutputTruncated' or depth <= 0 or len(audio) < 2:
            raise
    mid = len(audio)//2   # ponytail: plain midpoint, not the nearest pause; the halves are separate segments so timing stays exact
    return _run_window(run, audio[:mid], depth-1) + _run_window(run, audio[mid:], depth-1)


def worker():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        stream.reconfigure(encoding='utf-8')
    import hashlib
    from array import array
    from dataclasses import asdict
    requests = queue.Queue()
    def read_requests():
        for line in sys.stdin:
            requests.put(line)
        os._exit(0)  # Parent EOF releases GPU even during native loading/inference.
    threading.Thread(target=read_requests, daemon=True).start()
    protocol = sys.stdout
    sys.stdout = sys.stderr
    def emit(value):
        protocol.write(json.dumps(value, ensure_ascii=False)+'\n')
        protocol.flush()
    def sha(path):
        with path.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest()
    started = time.monotonic()
    if sha(MODEL) != MODEL_SHA256:
        raise RuntimeError('Q5 model checksum mismatch')
    import transcribe_cpp
    device = next(d for d in transcribe_cpp.backends()
                  if d.kind == 'vulkan' and 'RTX 5070' in d.description)
    with transcribe_cpp.Model(MODEL, device=device) as model:
        if model.device != device:
            raise RuntimeError('Q5 did not select the requested RTX GPU')
        with model.session(n_threads=8) as session:
            load_seconds = round(time.monotonic()-started, 3)
            warm_started = time.monotonic()
            # ponytail: representative shapes, not a guarantee every new length is warm.
            for seconds in (2, 20, 30, 40, 60):
                session.run(array('f', [0.0])*(seconds*16000), language='zh', itn='on', timestamps='none')
            warm_seconds = round(time.monotonic()-warm_started, 3)
            identity = {'worker_pid': os.getpid(), 'model_load_count': 1,
                        'load_seconds': load_seconds, 'warmup_seconds': warm_seconds,
                        'warmup_lengths_seconds': [2,20,30,40,60], 'device': asdict(device),
                        'unseen_length_first_inference_may_be_slower': True}
            emit({'state':'ready', **identity})
            while True:
                try:
                    request = json.loads(requests.get())
                    path = Path(request['path']).resolve(strict=True)
                    if not path.is_relative_to(ROOT) or not path.is_file() or not 0 < path.stat().st_size <= 2*1024**3:
                        raise ValueError('Invalid saved audio path')
                    before = sha(path)
                    started = time.monotonic()
                    segments = []
                    offset = 0.0
                    inference_seconds = 0.0
                    for audio in _audio_windows(path):
                        tick = time.monotonic()
                        pieces = _run_window(lambda a: session.run(a, language='zh', itn='on', timestamps='none'), audio)
                        inference_seconds += time.monotonic()-tick
                        for part, raw in pieces:
                            duration = len(part)/16000
                            segments.append({'start':offset, 'end':offset+duration, 'text':raw.text,
                                             'raw_output':raw.text, 'native_timings':asdict(raw.timings)})
                            offset += duration
                        if time.monotonic()-started > 1800:
                            raise RuntimeError('Nano transcription exceeded 30 minutes')
                    if not segments or before != sha(path):
                        raise RuntimeError('Empty decoded audio or original changed; result rejected')
                    emit({'text': ''.join(s['text'] for s in segments), 'segments':segments,
                          'model':MODEL_ID, 'version':transcribe_cpp.native_commit(), 'metadata':{
                              'engine':'transcribe.cpp', 'runtime_version':transcribe_cpp.native_version(),
                              'quantization':'Q5_K_M', 'duration_seconds':offset,
                              'wall_seconds':round(time.monotonic()-started,3),
                              'inference_seconds':round(inference_seconds,3),
                              'source_sha256':before, 'model_sha256':MODEL_SHA256, **identity,
                              'persistent_model':True, 'review_required':True, 'audio_uploaded':False,
                              'window_seconds':30, 'window_cut':'Silero VAD pauses, hard cut when none fits', 'audio_preprocessing':'16k mono PCM16 matching validated benchmark normalization', 'boundary_review_required':len(segments)>1,
                              'segment_timing_scope':'audio window boundaries, not word alignment',
                              'decoding_options':{'language':'zh','itn':'on','timestamps':'none','threads':8}}})
                except Exception as exc:
                    emit({'error':'Local Q5 Nano failed: '+str(exc)[:300]})
                    break


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--decode':
        first = True
        for samples in _decoded_windows(Path(sys.argv[2])):
            if first:
                sys.stdout.buffer.write(b'PCM16K2\n')
                first = False
            sys.stdout.buffer.write(struct.pack('<I', len(samples)) + samples.astype('<f4', copy=False).tobytes())
        raise SystemExit(0)
    try:
        worker()
    except Exception as exc:
        sys.__stdout__.write(json.dumps({'error': 'Nano startup failed: '+str(exc)[:300]})+'\n')
        sys.__stdout__.flush()
        raise SystemExit(1)
