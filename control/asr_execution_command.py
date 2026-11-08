"""Fixed Claude tool entrypoint: no shell, arbitrary arguments, or network."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import instance_config as ic  # noqa: E402  (this machine's paths and owner identity; defaults = today's values)

ROOT = Path(__file__).resolve().parents[2]
INCUBATOR = ROOT / 'incore'
LINUX_ROOT = ic.wsl(ic.V["workspace"])

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['status', 'whisper', 'SenseVoiceSmall', 'Fun-ASR-Nano-2512', 'nano-integration', 'quant-Q4_K_M', 'quant-Q5_K_M', 'quant-Q8_0', 'q5-cpu', 'q5-vulkan', 'q5-integration'])
    args = parser.parse_args()
    if args.action == 'q5-integration':
        return subprocess.run([sys.executable, str(INCUBATOR/'input/q5_integration_check.py')], cwd=ROOT, timeout=300).returncode
    if args.action in ('q5-cpu', 'q5-vulkan'):
        out = INCUBATOR/'.runtime/q5-gpu-benchmark'
        out.mkdir(exist_ok=True)
        result = subprocess.run([str(INCUBATOR/'.runtime/quant-gpu-venv/Scripts/python.exe'), str(INCUBATOR/'input/q5_gpu_benchmark.py'), args.action[3:]], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=240)
        (out/(args.action+'.log')).write_text(result.stdout+'\n'+result.stderr, encoding='utf-8')
        print(result.stdout[-18000:]); print(result.stderr[-4000:])
        return result.returncode
    if args.action.startswith('quant-'):
        out = INCUBATOR/'.runtime/nano-quant-benchmark'
        command = ['wsl.exe','-d','Ubuntu','--','/usr/bin/timeout','--kill-after=5s','240',LINUX_ROOT+'/incore/.runtime/quant-bench-venv/bin/python',LINUX_ROOT+'/incore/input/nano_quant_benchmark.py',args.action[6:]]
        result = subprocess.run(command,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=260)
        (out/(args.action+'.log')).write_text(result.stdout+'\n'+result.stderr,encoding='utf-8')
        print(result.stdout[-18000:]);print(result.stderr[-2000:])
        return result.returncode
    if args.action == 'nano-integration':
        return subprocess.run([sys.executable, str(INCUBATOR/'input/nano_integration_check.py')], cwd=ROOT, timeout=300).returncode
    out = INCUBATOR / '.runtime/local-candidate-benchmark-all'
    if args.action == 'status':
        print(json.dumps({'dependencies_ready':(INCUBATOR/'.runtime/funasr-installed.json').exists(),
                          'models':{name:(INCUBATOR/'.runtime/models'/name/'download-manifest.json').exists() for name in ['SenseVoiceSmall','Fun-ASR-Nano-2512']},
                          'results':{p.stem:json.loads(p.read_text(encoding='utf-8')) for p in out.glob('*.json')}},ensure_ascii=False))
        return 0
    if not (INCUBATOR/'.runtime/funasr-installed.json').exists():
        print('Dependency installation is not finished. Stop and report blocker; do not loop.'); return 2
    runtime='asr-venv' if args.action=='whisper' else 'funasr-venv'
    libs=LINUX_ROOT+'/incore/.runtime/asr-venv/lib/python3.12/site-packages/nvidia/'
    command=['wsl.exe','-d','Ubuntu','--','/usr/bin/timeout','--kill-after=5s','240',
             '/usr/bin/env','HF_HUB_OFFLINE=1','HF_HUB_DISABLE_TELEMETRY=1','TRANSFORMERS_OFFLINE=1',
             'LD_LIBRARY_PATH='+libs+'cublas/lib:'+libs+'cudnn/lib:/usr/lib/wsl/lib' if args.action=='whisper' else 'ASR_BENCHMARK_LOCAL=1',
             'PYTHONPATH='+LINUX_ROOT,LINUX_ROOT+'/incore/.runtime/'+runtime+'/bin/python',
             '-m','incore.input.local_candidate_benchmark',args.action]
    result=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=260)
    out.mkdir(exist_ok=True)
    (out/(args.action+'.log')).write_text(result.stdout+'\n'+result.stderr,encoding='utf-8')
    print(result.stdout[-6000:])
    if result.returncode:
        print(result.stderr[-8000:]); return result.returncode
    print((out/(args.action+'.json')).read_text(encoding='utf-8'))
    return 0

if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
