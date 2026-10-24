# Real workers and usage readings

Nothing here runs during the offline rehearsal, and no test calls a model: the official CLI is replaced by a fake.

## Usage readings (`orgcore/statusline.py`)

The source is the official CLI's own status data. An interactive session runs the configured `statusLine` command and passes it JSON on standard input; when the account reports them, `rate_limits.five_hour` and `rate_limits.seven_day` carry `used_percentage` (0-100) and `resets_at` (Unix seconds). The hook

```
python -m orgcore usage-capture <instance>
```

reads that JSON and merges the windows into `state/usage.json` with source `claude-code-statusline <version>`. The setup session started by `bootstrap.py` is launched with this hook through `--settings`, so it records readings while it runs. Nothing is polled and no endpoint is called.

- A window that is absent, malformed, outside 0-100 or already reset is not written: unknown stays unknown, never zero.
- Readings are matched to the windows in `policy.json` by name: `five_hour`, `5-hour`, `5h` mean the 5-hour window; `seven_day`, `7-day`, `weekly` the 7-day one.
- Each window has `max_observation_age_minutes`; an old reading closes the gate by itself.
- Headless (`-p`) sessions draw no status line, so they produce no readings. Real work therefore needs an interactive session open (or another source writing the same file); otherwise the readings go stale and the gate stays closed. This is the intended failure direction.
- `python -m orgcore usage <instance>` prints the readings and whether the dispatch gate is open, with the reason for each closed window.

## Launching a worker (`orgcore/adapter.py`)

One session of the official CLI per role and attempt. A session starts only if all of these hold, and each failure lists its reason: `live_enabled` is true; the live consent names the project; the CLI is signed in with a subscription login with no API key, token, cloud-provider switch or `apiKeyHelper`; every usage window has a fresh reading under its reserve and share limits. The reviewer must not be the executor.

| | executor | reviewer |
|---|---|---|
| prompt | `prompts/executor.md` | `prompts/reviewer.md` |
| allowed tools | Read, Edit, Write, Glob, Grep | Read, Glob, Grep |
| denied tools | Bash, WebFetch, WebSearch | Bash, Edit, Write, WebFetch, WebSearch, NotebookEdit |
| ends with | `EVIDENCE: <relative path>` | `VERDICT: pass`, `VERDICT: rework` or `VERDICT: blocked` |

Common to both: `-p` with `--output-format json`, `--permission-mode dontAsk` (anything not allowed is refused instead of asked), `--add-dir` and the working directory set to the project's folder (`path` in `org.json`, else `<instance>/work/<project>`), a fresh `--session-id` derived from instance, row, role and attempt, and the environment with the billing variables removed. An instance can keep its own edited copy of a prompt in `<instance>/prompts/`; it wins over the packaged one and is never rewritten.

The executor's evidence must be an existing file inside its working directory, and the reviewer's reply must end in a verdict line; otherwise the session counts as failed, not as a pass.

After a run, `scheduler.settle` records what it consumed (see `docs/scheduling.md`).
