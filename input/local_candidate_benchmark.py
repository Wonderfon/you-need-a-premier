"""Manual WSL benchmark; no database writes or private audio network transfer."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import time
import wave

ROOT = Path(__file__).resolve().parents[1]

def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def main():
    p = argparse.ArgumentParser()
    p.add_argument('model', choices=['whisper', 'SenseVoiceSmall', 'Fun-ASR-Nano-2512'])
    args = p.parse_args()
    os.environ.update(HF_HUB_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1', TRANSFORMERS_OFFLINE='1')
    ids = ['e9dabf3f-cc03-4334-ba5e-df75c08daecc', '3a5ac8b1-6a0e-49a1-899f-5e02c96101a6', '7f5c7b38-2004-4e0c-a99a-68619f63489b']
    originals = [ROOT/'input/data/sessions'/sid/'original.webm' for sid in ids]
    output = ROOT/'.runtime/local-candidate-benchmark-all'
    output.mkdir(exist_ok=True)
    original_hashes = [sha(path) for path in originals]
    import numpy as np
    if args.model == 'whisper':
        from faster_whisper.audio import decode_audio
        for sid, original in zip(ids, originals):
            audio = decode_audio(str(original), sampling_rate=16000)
            with wave.open(str(output/(sid+'.wav')), 'wb') as f:
                f.setnchannels(1); f.setsampwidth(2); f.setframerate(16000)
                f.writeframes((np.clip(audio, -1, 1)*32767).astype('<i2').tobytes())
    audios = []
    for sid in ids:
        with wave.open(str(output/(sid+'.wav')), 'rb') as f:
            assert f.getframerate() == 16000 and f.getnchannels() == 1
            audios.append(np.frombuffer(f.readframes(f.getnframes()), dtype='<i2').astype(np.float32)/32768)
    t = time.monotonic()
    if args.model == 'whisper':
        from faster_whisper import WhisperModel
        from incore.input.transcribe import DECODE_OPTIONS
        modeldir=ROOT/'.runtime/models/faster-whisper-large-v3-turbo'
        model=WhisperModel(str(modeldir), device='cuda', compute_type='float16',local_files_only=True,cpu_threads=8)
        def run(samples):
            segments,_=model.transcribe(samples,**DECODE_OPTIONS)
            return ''.join(s.text for s in segments).strip()
        packages={'faster-whisper':importlib.metadata.version('faster-whisper')}
        sync=lambda: None
    else:
        import torch
        from funasr import AutoModel
        modeldir=ROOT/'.runtime/models'/args.model
        model=AutoModel(model=str(modeldir), device='cuda:0',disable_update=True,trust_remote_code=False)
        def run(samples):
            options={'language':'auto','use_itn':True} if args.model=='SenseVoiceSmall' else {'language':'中文','itn':True,'hotwords':[]}
            data = torch.from_numpy(samples) if args.model=='Fun-ASR-Nano-2512' else samples
            return model.generate(input=data,cache={},batch_size=1,**options)
        packages={x:importlib.metadata.version(x) for x in ['torch','torchaudio','funasr']}
        sync=torch.cuda.synchronize
    sync()
    load=time.monotonic()-t
    results=[]
    for sid, audio in zip(ids, audios):
        passes = []
        for case in ('first_pass', 'warm_pass'):
            # SenseVoice's documented short-input limit is 30s; retain every window raw.
            windows = [audio[i:i+480000] for i in range(0,len(audio),480000)] if args.model=='SenseVoiceSmall' else [audio]
            rows=[]
            for index, samples in enumerate(windows):
                t=time.monotonic(); raw=run(samples); sync(); elapsed=time.monotonic()-t
                rows.append({'window':index,'start_seconds':index*30 if args.model=='SenseVoiceSmall' else 0,'duration_seconds':len(samples)/16000,'seconds':elapsed,'raw_output':raw})
            passes.append({'case':case,'seconds':sum(r['seconds'] for r in rows),'windows':rows})
            print(json.dumps({'model':args.model,'session_id':sid,'case':case,'seconds':passes[-1]['seconds']}),flush=True)
        results.append({'session_id':sid,'duration_seconds':len(audio)/16000,'source_sha256':sha(originals[ids.index(sid)]),'wav_sha256':sha(output/(sid+'.wav')),'passes':passes})
    t=time.monotonic(); silence=run(np.zeros(48000,dtype=np.float32)); sync()
    silence_result={'seconds':time.monotonic()-t,'raw_output':silence}
    peak=None if args.model=='whisper' else {'allocated_bytes':torch.cuda.max_memory_allocated(),'reserved_bytes':torch.cuda.max_memory_reserved()}
    assert original_hashes == [sha(path) for path in originals]
    result={'model':args.model,'manifest':json.loads((modeldir/'download-manifest.json').read_text()),'packages':packages,'load_seconds':load,'results':results,'silence_3s':silence_result,'torch_peak_vram':peak,'human_reference_available':False,'cer':None,'audio_uploaded':False,'application_latency_measured':False}
    (output/(args.model+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'saved':str(output/(args.model+'.json'))}),flush=True)

if __name__=='__main__':
    main()
