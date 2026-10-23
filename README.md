# orgcore (working name)

A small, text-first core for running an organization of AI agents: configuration with a strict schema, a plain-text task board with safe concurrent writes, and a checker that keeps private details out of the repository. Python 3.11+, standard library only.

Status: v0.1 in progress. This repository contains the configuration layer, the task board, the scheduler with a usage gate, fake workers and an offline rehearsal. Local reports, owner intent and feedback files, and guided setup are added in later blocks.

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
python -m orgcore rehearse /tmp/demo-instance --rating useful
```

## Layout

| Path | What |
|---|---|
| `orgcore/config.py` | schema validation and loading of the instance files |
| `orgcore/backlog.py` | parse, claim, transition, recover the task board |
| `orgcore/fsio.py` | cross-process lock and atomic write |
| `orgcore/scheduler.py`, `usage.py` | one-row dispatch, usage observations and the gate |
| `orgcore/workflow.py`, `fake.py`, `rehearsal.py` | executor/reviewer roles, fake workers, offline rehearsal |
| `orgcore/check.py` | one-pass validation of an instance |
| `docs/` | file formats |
| `examples/` | fictional demo instance |
| `tools/check_private.py` | scan the tree and history against a private ban list kept outside the repo |

Your real instance directory lives outside this repository; see `docs/config.md`.
