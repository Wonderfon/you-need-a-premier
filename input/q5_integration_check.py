"""Fixed offline resident Q5 acceptance; preserves all originals and database."""
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from incore.input import nano

def main():
    subprocess.run([sys.executable,'-m','unittest','incore.input.test_input','-q'],cwd=ROOT,check=True)
    subprocess.run(['node','--check',str(ROOT/'incore/input/static/app.js')],check=True)
    baseline=json.loads((ROOT/'incore/.runtime/q5-gpu-benchmark/vulkan.json').read_text(encoding='utf-8'))
    results=[]
    try:
        ready=nano.preload()
        assert ready['ready'] and ready['device']['kind']=='vulkan' and 'RTX 5070' in ready['device']['description']
        for row in baseline['recordings']:
            path=ROOT/'incore/input/data/sessions'/row['id']/'original.webm'
            started=time.monotonic()
            result=nano.transcribe(path)
            with path.open('rb') as stream:
                assert hashlib.file_digest(stream,'sha256').hexdigest()==row['original_sha256']
            assert result['metadata']['worker_pid']==ready['worker_pid'] and result['metadata']['model_load_count']==1
            results.append({'session_id':row['id'],'wall_seconds':round(time.monotonic()-started,3),'metadata':result['metadata'],'text_matches_gpu_baseline':result['text']==row['passes'][-1]['text'],'actual_text':result['text'],'expected_text':row['passes'][-1]['text']})
        receipt={'status':'passed' if all(r['text_matches_gpu_baseline'] for r in results) else 'output_differences_require_review','ready':ready,'requests':results,'originals_unchanged':True,'database_written':False}
        (ROOT/'incore/.runtime/q5-resident-results.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
        public={**receipt,'requests':[{k:v for k,v in row.items() if k not in ('actual_text','expected_text')} for row in results]}
        public['private_results']='.runtime/q5-resident-results.json'
        (ROOT/'incore/audits/q5-resident-integration.json').write_text(json.dumps(public,indent=2),encoding='utf-8')
        print(json.dumps(receipt,ensure_ascii=True))
        assert all(r['text_matches_gpu_baseline'] for r in results), 'Q5 raw output differences saved for coordinator review'
    finally:
        nano.shutdown()
    assert nano.status()['state']=='stopped'

if __name__=='__main__':
    main()
