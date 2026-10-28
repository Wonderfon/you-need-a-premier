# 你需要一个总理 (You need a premier)

**For people whose ideas outrun their capacity to execute.** You say what you intend. A coordinator (the "premier", 总理) takes that intent, hands work to AI agents, and pushes it forward relentlessly, pacing itself against whatever subscription usage you have left.

![Usage against the pace line](docs/images/pace-line.png)

*One week of usage (fictional data). The straight line is the pace the premier allows itself. Work pauses when usage gets ahead of the line, and the share you keep for yourself is never touched.*

## What makes it different

- **Setup is a conversation.** The premier interviews you one question at a time and saves every answer as a file.
- **The task board is one text file.** You and the agents edit the same Markdown rows.
- **Workers can't grade their own work.** A separate reviewer passes each task or sends it back.
- **It runs on your real usage, not a timer.** Readings come from the official Claude Code CLI. An unknown reading stops work; it is never treated as zero.
- **Your words stay attached.** Every task cites what you actually said.
- **Hard limits.** No API key, no spending, no outside contact. These are fixed in code, not settings.

## Try it offline

No model, nothing sent. Work on a copy of the demo:

```
cp -r examples/demo /tmp/demo        # Windows: Copy-Item -Recurse examples\demo $env:TEMP\demo
python -m orgcore rehearse /tmp/demo
```

## Set it up

```
python bootstrap.py
```

The premier interviews you inside the official Claude Code CLI, on your own subscription. Python 3.11+, standard library only.

## Enable real workers

Real workers stay off until you type `yes`, and then they run for one project and one task at a time. The sessions count against your own plan's limits. Steps: [docs/real-workers.md](docs/real-workers.md). Read the [FAQ](docs/faq.md) on billing and plan terms first.

---

Status: v0.1, one real task per command. See a [real run](docs/real-run/README.md) · [file map](docs/layout.md) · tests: `python -m unittest` · MIT license
