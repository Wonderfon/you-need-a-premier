"""Manual, offline matched Windows CPU/Vulkan Q5 benchmark; never changes production."""
import argparse
from array import array
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '.runtime/q5-gpu-benchmark'

def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('backend', choices=['cpu', 'vulkan'])
    args = parser.parse_args()
    OUT.mkdir(exist_ok=True)
    weights = ROOT/'.runtime/nano-quant-benchmark/Fun-ASR-Nano-2512-Q5_K_M.gguf'
    metadata = json.loads((weights.parent/'upstream-model-metadata.json').read_text(encoding='utf-8'))
    expected = next(x['lfs']['sha256'] for x in metadata['siblings'] if x['rfilename']==weights.name)
    assert sha(weights)==expected
    inventory=json.loads((ROOT/'.runtime/local-candidate-benchmark-all/inventory.json').read_text(encoding='utf-8'))['selected']
    for row in inventory:
        assert sha(ROOT/'input/data/sessions'/row['id']/'original.webm')==row['original_sha256']
    started=time.perf_counter()
    import transcribe_cpp
    report={'backend':args.backend,'platform':sys.platform,'runtime_version':transcribe_cpp.native_version(),
            'runtime_commit':transcribe_cpp.native_commit(),'model_sha256':expected,'threads':8,
            'language':'zh','itn':'on','segmentation':'none','import_seconds':time.perf_counter()-started,
            'native_hashes':{p.name:sha(p) for p in Path(transcribe_cpp.artifact_dir()).glob('*.dll')},'recordings':[]}
    devices=transcribe_cpp.backends()
    selected=next(d for d in devices if d.kind=='vulkan' and 'RTX 5070' in d.description) if args.backend=='vulkan' else next(d for d in devices if d.kind=='cpu')
    report['selected_device']=asdict(selected)
    gpu_log=OUT/(args.backend+'-gpu.csv')
    with gpu_log.open('w') as log:
        monitor=subprocess.Popen(['nvidia-smi','--query-gpu=timestamp,name,memory.used,utilization.gpu','--format=csv,noheader,nounits','-lms','100'],stdout=log,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            time.sleep(.3)
            started=time.perf_counter()
            with transcribe_cpp.Model(weights,device=selected) as model:
                report['load_seconds']=time.perf_counter()-started
                report['actual_device']=asdict(model.device)
                assert model.device==selected
                with model.session(n_threads=8) as session:
                    report['session_ready_seconds']=time.perf_counter()-started
                    for row in inventory:
                        wav=ROOT/'.runtime/local-candidate-benchmark-all'/(row['id']+'.wav')
                        with wave.open(str(wav),'rb') as audio:
                            assert (audio.getframerate(),audio.getnchannels(),audio.getsampwidth())==(16000,1,2)
                            pcm=array('f',(v/32768 for v in array('h',audio.readframes(audio.getnframes()))))
                        result={'id':row['id'],'original_sha256':row['original_sha256'],'wav_sha256':sha(wav),'duration_seconds':len(pcm)/16000,'passes':[]}
                        for case in ('first','warm1','warm2','warm3'):
                            tick=time.perf_counter()
                            decoded=session.run(pcm,language='zh',itn='on',timestamps='none')
                            result['passes'].append({'case':case,'seconds':time.perf_counter()-tick,'text':decoded.text,'native_timings':asdict(decoded.timings)})
                        result['warm_median_seconds']=statistics.median(p['seconds'] for p in result['passes'][1:])
                        report['recordings'].append(result)
                        (OUT/(args.backend+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        finally:
            monitor.terminate()
            monitor.wait(timeout=5)
    samples=[float(line.split(',')[2].strip()) for line in gpu_log.read_text(encoding='utf-8').splitlines() if 'RTX 5070' in line]
    report['gpu_device_memory_mib']=({'baseline':samples[0],'peak':max(samples),'peak_delta':max(samples)-samples[0],'sample_interval_ms':100,'scope':'whole GPU; concurrent production and desktop allocation included, not process VRAM'} if samples else {'status':'unavailable'})
    report['originals_unchanged']=all(sha(ROOT/'input/data/sessions'/r['id']/'original.webm')==r['original_sha256'] for r in inventory)
    (OUT/(args.backend+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))

if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
