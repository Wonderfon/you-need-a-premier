# orgcore (working name)

A small, text-first core for running an organization of AI agents: configuration with a strict schema, a plain-text task board with safe concurrent writes, and a checker that keeps private details out of the repository. Python 3.11+, standard library only.

Status: v0.1 in progress. This repository contains the configuration layer, the task board, the scheduler with a usage gate, fake workers and an offline rehearsal, owner intent pages, a rating summary, local reports, and a guided first-time setup run by a coordinator in the official Claude Code CLI.

## First-time setup

```
python bootstrap.py
```

It checks this machine, asks for your consent, and starts the official Claude Code CLI with the coordinator's setup prompt. The interview happens there, on your own subscription sign-in; this repository never asks for a credential and never calls a model itself. Answers are saved as files in the directory you choose and the setup can be stopped and continued at any time. The menu also offers an offline rehearsal that needs no CLI. Details: `docs/guided-setup.md`.

## Enable real workers

Real execution is off until you turn it on, for one project and one task at a time. Nothing here spends money: the sessions run on your own Claude subscription sign-in, and no API key is ever used or accepted.

1. Finish the guided setup and the offline rehearsal (menu option 2), and rate it. Right after your rating the script asks: "Keep using the offline demo, or enable real Claude workers?" Answer `1` and nothing changes (`live_enabled` stays false). Answer `2`, pick a project and a task, read what would be handed to the model (project, folder, the task, your reserve lines), and type `yes` to write the consent. Without `yes` nothing is written. The same can be done later:

   ```
   python -m orgcore enable-live <instance> --project <id> --task "<exact task line>"          # preview only
   python -m orgcore enable-live <instance> --project <id> --task "<exact task line>" --yes    # writes the consent
   python -m orgcore disable-live <instance>                                                   # withdraws it
   ```

2. Right after the consent is written the setup re-checks the sign-in, the CLI options the workers need, and every usage window. Anything missing leaves the setup in the phase `live_waiting_for_usage` with the reasons listed; the offline demo result stays valid. When everything holds the phase becomes `live_ready`. No probe task is run to obtain a reading.
3. Headless (`claude -p`) sessions produce no usage readings, and readings go stale. So keep one interactive Claude Code session open that runs the status-line hook (`docs/real-workers.md`), for example:

   ```
   claude --settings '{"statusLine":{"type":"command","command":"python -m orgcore usage-capture <instance>"}}'
   ```

   Run it from the repository folder (or give `python` the full path to it) so the hook finds `orgcore`; quote the JSON the way your shell needs. The session can stay idle; the readings refresh as it redraws its status line. Check with `python -m orgcore usage <instance>`; the gate opens when every window has a fresh reading under its limits.
4. Start one real unit:

   ```
   python -m orgcore run <instance>
   ```

   It takes the consented row through an executor session, then a separate reviewer session (different prompt, different session id, different identity), and then records how much of your usage window the unit consumed. If any check fails it lists the reasons and starts nothing. One command, one executor round and one review; a row sent back for rework is run again by the next command.

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
| `orgcore/live.py` | the consent for real workers, the readiness check and the one-unit `run` command |
| `orgcore/check.py` | one-pass validation of an instance |
| `bootstrap.py`, `prompts/`, `orgcore/setup.py`, `doctor.py` | guided setup: entry point, coordinator prompt, answer writer, machine checks (`docs/guided-setup.md`) |
| `docs/` | file formats |
| `examples/` | fictional demo instance |
| `tools/check_private.py` | scan the tree and history against a private ban list kept outside the repo |

Your real instance directory lives outside this repository; see `docs/config.md`.
