# orgcore (working name)

A small, text-first core for running an organization of AI agents: configuration with a strict schema, a plain-text task board with safe concurrent writes, and a checker that keeps private details out of the repository. Python 3.11+, standard library only.

Status: v0.1 in progress. This repository contains the configuration layer, the task board, the scheduler with a usage gate, fake workers and an offline rehearsal, owner intent pages, a rating summary, local reports, and a guided first-time setup run by a coordinator in the official Claude Code CLI.

## First-time setup

```
python bootstrap.py
```

It checks this machine, asks for your consent, and starts the official Claude Code CLI with the coordinator's setup prompt. The interview happens there, on your own subscription sign-in; this repository never asks for a credential and never calls a model itself. Answers are saved as files in the directory you choose and the setup can be stopped and continued at any time. The menu also offers an offline rehearsal that needs no CLI. Details: `docs/guided-setup.md`.

## Run the tests

```
python -m unittest
```

Run from the repository root, on Windows (`py -3.12 -m unittest`) or Linux.

## Try the demo

```
python -m orgcore check examples/demo
```

Everything in `examples/` is fictional. Walk one row through dispatch, review, rework and an owner rating, offline, on a copy:

```
cp -r examples/demo /tmp/demo-instance        # Windows: Copy-Item -Recurse examples\demo $env:TEMP\demo-instance
python -m orgcore rehearse /tmp/demo-instance              # stops at the result so you can read it first
python -m orgcore rate /tmp/demo-instance useful           # then record your verdict (rehearse --rating does both at once)
python -m orgcore report /tmp/demo-instance                # writes reports/<date>-<HHMM>.md when a slot is due
```

The demo uses the time zone `UTC`, which works everywhere. If you set a named zone such as `America/New_York` in your own `reporting.json`, Windows needs the `tzdata` package (`python -m pip install tzdata`); without it `check` prints a warning and `report` fails with a clear message.

## Layout

| Path | What |
|---|---|
| `orgcore/config.py` | schema validation and loading of the instance files |
| `orgcore/backlog.py` | parse, claim, transition, recover the task board |
| `orgcore/fsio.py` | cross-process lock and atomic write |
| `orgcore/scheduler.py`, `usage.py` | one-row dispatch, usage observations and the gate |
| `orgcore/workflow.py`, `fake.py`, `rehearsal.py` | executor/reviewer roles, fake workers, offline rehearsal |
| `orgcore/intent.py`, `feedback.py`, `report.py` | owner intent pages, rating summary, local reports (`docs/intent-feedback-reports.md`) |
| `orgcore/adapter.py`, `statusline.py`, `prompts/executor.md`, `prompts/reviewer.md` | real CLI workers with their guards, usage readings from the CLI's status data, role templates (`docs/real-workers.md`) |
| `orgcore/check.py` | one-pass validation of an instance |
| `bootstrap.py`, `prompts/`, `orgcore/setup.py`, `doctor.py` | guided setup: entry point, coordinator prompt, answer writer, machine checks (`docs/guided-setup.md`) |
| `docs/` | file formats |
| `examples/` | fictional demo instance |
| `tools/check_private.py` | scan the tree and history against a private ban list kept outside the repo |

Your real instance directory lives outside this repository; see `docs/config.md`.
