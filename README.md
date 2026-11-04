# You Need a Premier

English | [Chinese](README.zh-CN.md)

Let AI's execution catch your creativity. Align intent with the AI once, and the Premier keeps your own AI organization running your projects.

## When to use it

Use it when you have more ideas and projects than you can execute yourself, and you work with AI agents.

1. **It runs for the long term on its own.** After one intent-alignment session, it keeps running autonomously.
2. **It catches your new ideas.** It picks up and executes a new idea of yours at any time, and schedules resources sensibly while doing so.
3. **It reports at the key points.** When a task reaches a key node, the AI reports to the Owner (you) on its own and asks for your feedback.
4. **Your data is not wasted.** Your verbatim words, the information you provide and the private data you produce are all archived. This data is valuable.

## Org structure

![Org structure](docs/org-structure.svg)

- **Top layer:** the Owner (you), the Premier, the resource pool and the Pacer. The Premier aligns intent with the Owner, manages the resource pool and the Pacer, and has its own Executor team.
- The Premier talks with the Deputies, and coordinates the work of every line through the to-do list, scheduling and resource allocation.
- Each Deputy manages its own Executor team, reviews deliverables and reports. A Deputy can talk with the Premier and can report directly to the Owner.
- Executors normally do not report to the Owner directly. A deliverable is reviewed by a Deputy or the Premier first, and only then reported to you.
- You can open any Executor session at any time and talk to it directly. You start that channel yourself (the dashed line).

## Key designs

Each design answers one bottleneck that appears when a person and AI work together.

| Design | What it does | Bottleneck it addresses |
|---|---|---|
| **Premier** | Resource coordination and intent alignment | Planning and resource management |
| **Deputy** | Reviews deliverables and reports to the Owner (you) | Review: a person lacks the time and energy to review everything the AI produces |
| **Pacer** | Starts tasks according to token usage over a window; speeds up or slows down | Pace adjustment: watching how much quota is left and adjusting your own speed is a mental burden |
| **Executor** | The agent that does the task. One agent = one session, one dedicated agent per project | Managing and isolating context and memory |
| **Org board** | One place to see the whole organization | Finding the entry point to information: in a complex system with many files, just knowing where to look is hard |
| **Voice input desk** | Rules plus a small model post-process and polish what you say; it adapts to your language habits and accent. Built on open-source models, see below | Entering instructions, especially long ones |
| **History Keeper** | Records everything you input and every piece of information you produce | Valuable for a personal benchmark and for training personal models |

### The Pacer

The Pacer spends quota on a time rhythm and keeps a share for you, so the system does not burn everything the moment quota is available. You can step in at any time. Even near the end of a quota window, when little is left, you only wait a little: once the window resets there is quota again.

![Five-hour usage against the Pacer baseline](docs/images/pace-chart-en.png)

*A real five-hour window: observed usage against the Pacer's linear baseline (the straight line). Work pauses when usage gets ahead of the line, and the reserve kept for you is never touched. Personal text removed.*

### Voice input desk: built on open-source models

The voice input desk runs locally on open-source models. Speech to text: [Fun-ASR-Nano-2512](https://huggingface.co/FunAudioLLM/Fun-ASR-Nano-2512) from [FunASR](https://github.com/modelscope/FunASR), the Q5_K_M quantization in GGUF format. Post-processing and polish: [Qwen3-0.6B](https://huggingface.co/Qwen/Qwen3-0.6B), served by a local [llama.cpp](https://github.com/ggml-org/llama.cpp) `llama-server`. Thanks to these projects.

![The local voice input desk](docs/images/input-desk-en.png)

*The voice input desk, a real screenshot. Personal information removed.*

## Optional modules

| Module | Use |
|---|---|
| **Discord** | A cross-platform channel. Configure it to work with the system from your phone. Can be swapped for Slack or Teams. |
| **Email** | Similar to Discord. |
| **Tailscale** | Connects your devices, like a personal VPN. For example, view the org board from another computer. |
| **Colab CLI** | GPU access. If your local GPU is not enough, light tasks can run on Colab's GPU. |
| **Google Drive** | Backup, and file transfer between devices. |

## Recommended models (not required)

| Role | Model |
|---|---|
| Premier and Deputy | Opus 5.5 High |
| Executor | Sonnet 5.5 High |

## Quick start

1. Configure the modules you need.
2. Do one intent alignment with the Premier. This repo's entry point is `python bootstrap.py`, which starts the Premier's interview in the official Claude Code CLI (Python 3.11+, standard library only; see [guided setup](docs/guided-setup.md)).
3. Put your existing files in.
4. The system starts working automatically.

## Status

This repo is v0.1. What it ships today: the guided intent-alignment setup, the plain-text task board, a separate reviewer step, the Pacer's usage gate, and an offline rehearsal. The org board, voice input desk, History Keeper, the optional-module wiring and restoring a whole organization from a backup on a new computer are the design described above and are not in this package yet.

More: [offline demo](docs/offline-demo.md) · [real workers](docs/real-workers.md) · [file map](docs/layout.md) · MIT license
