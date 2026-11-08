"""Offline manual CPU GGUF comparison on three fixed saved microphone recordings."""
import argparse
from array import array
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import resource
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '.runtime/nano-quant-benchmark'

def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('quant', choices=['Q4_K_M', 'Q5_K_M', 'Q8_0'])
    args = parser.parse_args()
    inventory = json.loads((ROOT/'.runtime/local-candidate-benchmark-all/inventory.json').read_text())['selected']
    metadata = json.loads((OUT/'upstream-model-metadata.json').read_text())
    weights = OUT/f'Fun-ASR-Nano-2512-{args.quant}.gguf'
    expected = next(x['lfs']['sha256'] for x in metadata['siblings'] if x['rfilename'] == weights.name)
    assert sha(weights) == expected
    for row in inventory:
        assert sha(ROOT/'input/data/sessions'/row['id']/'original.webm') == row['original_sha256']
    start = time.perf_counter()
    import transcribe_cpp
    import_seconds = time.perf_counter() - start
    start = time.perf_counter()
    report = {'quant':args.quant,'backend':'cpu','threads':8,'model_bytes':weights.stat().st_size,
              'model_sha256':expected,'model_revision':metadata['sha'],'runtime_version':'0.2.4',
              'import_seconds':import_seconds,'language':'zh','itn':'on','segmentation':'none',
              'gpu_memory':'CPU explicitly selected; no model GPU allocation, not a GPU benchmark','recordings':[]}
    with transcribe_cpp.Model(weights, backend='cpu') as model:
        report['load_seconds'] = time.perf_counter()-start
        report['device'] = repr(model.device)
        with model.session(n_threads=8) as session:
            report['session_ready_seconds'] = time.perf_counter()-start
            for row in inventory:
                wav = ROOT/'.runtime/local-candidate-benchmark-all'/(row['id']+'.wav')
                with wave.open(str(wav),'rb') as stream:
                    assert stream.getframerate()==16000 and stream.getnchannels()==1 and stream.getsampwidth()==2
                    pcm16=array('h',stream.readframes(stream.getnframes()))
                pcm=array('f',(x/32768 for x in pcm16))
                result={'id':row['id'],'original_sha256':row['original_sha256'],'wav_sha256':sha(wav),'duration_seconds':len(pcm)/16000,'passes':[]}
                for case in ('first','warm'):
                    start_run=time.perf_counter()
                    decoded=session.run(pcm,language='zh',itn='on',timestamps='none')
                    elapsed=time.perf_counter()-start_run
                    result['passes'].append({'case':case,'seconds':elapsed,'text':decoded.text,'native_timings':asdict(decoded.timings)})
                report['recordings'].append(result)
                (OUT/(args.quant+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    report['peak_process_ram_mib']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024
    report['originals_unchanged']=all(sha(ROOT/'input/data/sessions'/r['id']/'original.webm')==r['original_sha256'] for r in inventory)
    (OUT/(args.quant+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))

if __name__=='__main__':
    main()
