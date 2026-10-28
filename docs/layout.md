# Layout

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
| `bootstrap.py`, `setup_hook.py`, `prompts/`, `orgcore/setup.py`, `doctor.py` | guided setup: entry point, the setup session's two hooks, coordinator prompt, answer writer, machine checks (`docs/guided-setup.md`) |
| `docs/` | file formats |
| `examples/` | fictional demo instance |
| `tools/check_private.py` | scan the tree and history against a private ban list kept outside the repo |
| `tools/make_screenshots.py`, `tools/svg_to_png.ps1`, `docs/images/` | regenerate the README images from the fictional demo (SVG sources and PNGs) |

Your real instance directory lives outside this repository; see `docs/config.md`.
