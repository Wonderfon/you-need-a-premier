# Install contract and asset list (Premier gap G6)

What a new machine needs, per component, so the incore runs the way it runs today. Every line says where the value
came from on this machine (read 2026-11-05, nothing on the machine was changed). The per-component package pins are in
`requirements/<component>.txt`; this file holds what pip cannot: interpreters, programs, model weights, hashes.

Paths below are this machine's defaults. On another machine, set `workspace` / `user_home` in `instance/instance.json`
(see `control/instance_config.py`, template `instance/example/instance.json`); the derived paths follow.

## 1. Components and interpreters

| Component | Entry point | Interpreter | Packages | Local source |
|---|---|---|---|---|
| Scheduler | `control/tick.py`, scheduled task "Org Core Coordinator Tick" | Windows Python 3.12.10, per-user install `C:\orghome\AppData\Local\Programs\Python\Python312` (task runs `pythonw.exe` directly, no venv) | none (standard library) | `python --version`; `control/scheduled-tasks/Org Core Coordinator Tick.xml` |
| Input station + dashboard | `input/server.py`, port 8765 | `discord\.venv` (Python 3.12.10) | `requirements/input-dashboard.txt` | `pip freeze` of that venv |
| Discord gateway | `discord/gateway.py` | `discord\.venv` | `requirements/discord-gateway.txt` | same |
| Backup | `backup/backup.py --watch` | `discord\.venv` | none (standard library) | `start-services.ps1:5` runs all three with that interpreter |
| ASR, Windows | `input/nano.py` worker | `.runtime\quant-gpu-venv` (Python 3.12.10) | `requirements/asr-windows.txt` | `pip freeze` of that venv |
| ASR, audio decode in WSL | `input/nano.py` calls `.runtime/asr-venv/bin/python` in Ubuntu | CPython 3.12.14 from uv (`.runtime/python/cpython-3.12-linux-x86_64-gnu`), venv by uv 0.12.19 | `requirements/asr-wsl.txt` | dist-info folders + `.runtime/asr-installed.txt` |
| Usage sampler | `runtime/`, `control/usage_sample.py` | `.runtime\usage-venv` (Python 3.12.14 bundled with the Codex runtime) | `requirements/usage-sampler.txt` | `pip freeze` of that venv |
| Local polish model | `input/llm_postprocess.py` -> llama-server on 127.0.0.1:8086 | no Python | none | `input/data/llm-postprocess.json` |
| Codex side | WSL Ubuntu | python3 3.14.4 (system) | none to run; test venvs are built on demand | `wsl -d Ubuntu` version commands |

Do not reuse an old machine's venv: create each one fresh from its requirements file
(`py -3.12 -m venv <path>` then `pip install -r requirements/<component>.txt`). One service venv serves the input station,
the gateway and the backup, so install `discord-gateway.txt` and `input-dashboard.txt` into the same venv.

## 2. Programs

| Program | Version on this machine | Where from | Local source |
|---|---|---|---|
| Python for Windows | 3.12.10 | not recorded; the per-user folder layout matches the python.org installer (assumption), and the installer file is not kept here | `pyvenv.cfg` of the venvs |
| Git for Windows | the copy bundled with the Codex runtime: `C:\orghome\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\git\cmd\git.exe` (no `git` on PATH) | taken from the Codex runtime folder (how it got there is not recorded) | `control/deployments.py`, `backup/backup.py` fall back to this path (`git_exe` in instance config) |
| Claude Code | `C:\orghome\.local\bin\claude.exe` | owner installs and logs in | `tick.py` `CLAUDE` (`claude_exe`) |
| Google Drive for Desktop | installer kept at `.runtime/installers/GoogleDriveSetup.exe`, 291,173,528 bytes | Google; owner signs in; the backup chain lives in `G:\My Drive\Org Core — Private Input Archive` | directory listing |
| WSL2 Ubuntu | Ubuntu 26.04.1 LTS; python3 3.14.4, git 2.53.0 | not recorded (`wsl --install` is the usual way) | `wsl -d Ubuntu` |
| Codex CLI (WSL) | codex-cli 0.160.0 at `~/.local/bin/codex` (the comment in `tick.py` still says 0.157.0) | owner installs and logs in | `codex --version` |
| uv (WSL) | 0.12.19 at `~/.local/bin/uv` | not recorded (uv's own installer is the usual way) | `uv --version` |
| llama.cpp | release b11384 (`llama-server --version`: 0.5.0-dev, build 11384, commit 0faee5004, Clang 20.1.8) in `C:\orghome\tools\llama.cpp`; the CUDA 12.4 Windows build | github.com/ggml-org/llama.cpp releases, assets named below | see hashes in section 3 |
| GPU driver / Vulkan | NVIDIA GeForce RTX 5070 Laptop GPU, driver 32.0.15.9608 (Vulkan driverInfo 596.08, API 1.4.329); Intel Graphics driver 32.0.101.8860; Vulkan instance 1.4.321 | the vendors' drivers; `vulkaninfo.exe` is the one in `C:\Windows\System32` | `vulkaninfo --summary` |
| ffmpeg | not found on the Windows PATH or in WSL; audio decoding is PyAV 18.1.0 in the WSL venv | - | `Get-Command ffmpeg`, `ffmpeg` in WSL both fail |
| Node.js, GitHub CLI, opencode | present in `C:\orghome\tools` (`node`, `gh`, `opencode`); not used by the incore code listed here | - | directory listing |

## 3. Binaries and model weights (name, size, SHA-256)

| Asset | Size (bytes) | SHA-256 | Source |
|---|---|---|---|
| `tools\llama.cpp\llama-b11384-bin-win-cuda-12.4-x64.zip` (llama-server.exe, llama-bench.exe and the DLLs come from it) | 263,809,590 | `dcb36d4390b64e041d5dbb96123c1ef22054824b7de29717cc22f3d3d60c71e1` | GitHub release b11384 of ggml-org/llama.cpp, asset of that name; the local zip is the authority |
| `tools\llama.cpp\cudart-llama-bin-win-cuda-12.4-x64.zip` (CUDA runtime DLLs) | 391,443,627 | `8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6` | same release page, asset of that name |
| `tools\llama.cpp\llama-server.exe` (launcher stub; the code is in the DLLs next to it, so verify the zip, not this file) | 9,216 | `a7674cf9cf5156cdcc40553148067d6144140661ee500d26627609c19542abc2` | extracted from the zip above |
| `.runtime\nano-quant-benchmark\Fun-ASR-Nano-2512-Q5_K_M.gguf` (ASR weights) | 631,128,832 | `68de7b48ba46b739254fb57486edf7023943cb0af63767ab795d58d6e482096c` (equals `MODEL_SHA256` in `input/nano.py:16`, which refuses to load a different file) | Hugging Face `handy-computer/Fun-ASR-Nano-2512-gguf`, revision `1b60240dda8f895d14b5d5d59a90d1509e27f662` (from `audits/nano-quant-benchmark.json:96`; the exact file within that revision was not re-checked) |
| `models\r2-q06-ep2-Q4_K_M.gguf` (polish model, 0.6B fine-tune) | 396,704,608 | `03fc9082eab602cfc91aa70c0c105f9cbd979e30b6988368e7a2b33812bf1bd2` | **owner's own fine-tune, not downloadable.** Trained on Colab (`audits/2026-10-30-asr-postprocess-finetune-round2.md`), exported and quantised to Q4_K_M (`audits/2026-10-30-asr-postprocess-round2-q4.md`); working copies were in `C:\orghome\wt\asr-postprocess-data\export\`. The weights are not in any backup ZIP (gap, see the result file) |
| `models\prompt-v1.txt` (polish prompt) | 1,228 | `cc13c073e1f06fee3045c0f5d3b5608c769cfa6c2563b1b93e1a5b16292f794b` | owner's text; the daily ZIP carries it as `external/llm-prompt/prompt-v1.txt` |

`input/data/llm-postprocess.json` names the polish pieces: model id `r2-q06-ep2`, endpoint `http://127.0.0.1:8086/v1/chat/completions`,
`server_exe`, `model_path`, `prompt_path`. Restore the file, then fix the three paths if the user folder differs.

Models under `models\` that only the benchmark scripts use (`Qwen3.5-4B`, `Qwen3.5-9B`, `Qwen3.6-35B-A3B` gguf) are not
needed to run the incore, so they are not listed with hashes.

## 4. Order on a new machine

1. Install WSL2 Ubuntu, Python 3.12, Claude Code, Google Drive for Desktop, the NVIDIA driver; owner logs in to each.
2. Restore the code and state from the backup ZIPs (`backup/RESTORE.md`), then write `instance/instance.json`.
3. Create the venvs fresh from `requirements/`; check `pip freeze` against the files.
4. Put llama.cpp b11384 and the weights in place; compare the SHA-256 values in section 3.
5. In WSL: install uv and Codex CLI, create `.runtime/asr-venv` from `requirements/asr-wsl.txt`.
6. Run the control and input test suites in the new service venv before starting any service.

## 5. What this file does not promise

- Nothing here registers scheduled tasks or starts services; that stays a separate, owner-approved step.
- The requirement files record what is installed, not a minimal set. `asr-wsl.txt` carries the whole faster-whisper stack that earlier
  benchmarks installed, although `input/nano.py` needs only `av` and `numpy` there.
- A new machine with a different GPU may need a different llama.cpp build or Vulkan driver; the contract is the build that works here.
