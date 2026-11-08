"""Manual integration smoke, run by the bounded Claude executor; no DB writes."""
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from incore.input import nano


def main():
    subprocess.run([sys.executable, '-m', 'unittest', 'incore.input.test_input', '-q'], cwd=ROOT, check=True)
    subprocess.run(['node', '--check', str(ROOT/'incore/input/static/app.js')], check=True)
    started = time.monotonic()
    try:
        ready = nano.preload()
        results = []
        for path in [ROOT/'incore/.runtime/samples/jfk.flac', ROOT/'incore/input/data/sessions/3a5ac8b1-6a0e-49a1-899f-5e02c96101a6/original.webm']:
            call = time.monotonic()
            result = nano.transcribe(path)
            results.append({'elapsed_seconds': round(time.monotonic()-call, 3), 'metadata': result['metadata'], 'text_nonempty': bool(result['text']), 'segments': len(result['segments'])})
        assert all(r['text_nonempty'] for r in results)
        assert all(r['metadata']['decoding_options']['language'] == '\u4e2d\u6587' for r in results)
        assert all(r['metadata']['worker_pid'] == ready['worker_pid'] and r['metadata']['model_load_count'] == 1 for r in results)
        assert results[1]['segments'] == 1 and results[1]['metadata']['duration_seconds'] > 35
        receipt = {'status': 'passed', 'ready': ready, 'requests': results, 'total_seconds': round(time.monotonic()-started,3), 'audio_uploaded':False, 'database_written':False}
        path = ROOT/'incore/audits/nano-integration-smoke.json'
        path.write_text(json.dumps(receipt,indent=2),encoding='utf-8')
        print(json.dumps(receipt))
    finally:
        nano.shutdown()

if __name__ == '__main__':
    main()
