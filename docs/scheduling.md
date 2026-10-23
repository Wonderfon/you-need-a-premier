# Scheduling, usage gate and roles

## One dispatch

`scheduler.dispatch(instance, usage_source, actor)` hands out at most one row per call. Under the board lock it checks, in order:

1. **Consistency.** The board and `org.json` must agree (see Weights below). Otherwise: `inconsistent`.
2. **Concurrency.** Rows in `running` must be fewer than `policy.max_concurrency`. Otherwise: `busy`.
3. **Work.** Rows in `todo` or `rework` must exist. Otherwise: `no_work`.
4. **Usage gate.** Every configured subscription window must pass. Otherwise: `usage`, with one reason per failing window.
5. **Choice.** Among lines that have a candidate row, take the lowest `(spend + 1) / weight`. `spend` is the number of dispatches that line received in the last 7 days (from `state/runs.jsonl`); `weight` is the line's `Weight:` row. A tie goes to the line listed first. Inside the line, a `rework` row goes first, otherwise the first `todo` row in board order.

Because the whole decision runs under the board lock, two schedulers started at the same moment never claim the same row and never exceed the concurrency limit.

The claim is written to `state/runs.jsonl` before the board changes. A crash between the two over-counts one dispatch; it never loses one.

## Weights

The `Weight:` rows in `projects/backlog.md` are authoritative: the board is the ledger and is edited while the organization runs. `org.json` carries the initial value. `python -m orgcore check` reports every line where they differ, and dispatch refuses while they differ. Project weights in `org.json` are not used by this version.

## Usage observations

A source implements `observe() -> list[Observation]`. Each reading carries `window`, `used_percent` (of the whole window, consumed by anyone on the account), `observed_at`, `resets_at`, `source`, and `synthetic`. Built in: `FakeUsage` (fixed readings) and `JsonFileUsage` (`state/usage.json`).

A window blocks dispatch, with the reason written out, when its reading is:

| Cause | Meaning |
|---|---|
| unknown | no reading for the window, or the source could not be read; never treated as zero |
| stale | older than `max_observation_age_minutes`, or dated in the future |
| reset passed | `resets_at` is already behind us, so the reading describes a finished window |
| reserve | `used_percent >= 100 - reserve_percent` |
| share | `used_percent >= max_share_percent` |
| pacing | optional: `length_hours` and `pacing_slack_percent` set; blocks when `used_percent` is ahead of `max_share_percent * elapsed_fraction + slack` |
| synthetic in live mode | synthetic readings are only accepted while `live_enabled` is false |

## Roles

- The **executor** can do one thing to a running row: `workflow.submit(...)`, which moves it to `review` and records evidence. Only the identity the row was dispatched to can submit it.
- The **reviewer** calls `workflow.review(..., verdict, evidence)` with `pass`, `rework` or `blocked` and non-empty evidence. A reviewer identity that executed any attempt of the row is refused. `pass` moves the row to `done`; `rework` and `blocked` keep the reason in the row.
- The **owner** rating (`useful`, `not_useful`, `rework`) is recorded for finished rows in `state/feedback.jsonl`; it does not change the board in this version.

These checks live in code and compare identities recorded in the ledger. The board is a plain text file, so give agents these functions rather than write access to the file.

## Crash recovery

- A worker that is working on a row holds `workflow.claim_lock(...)`. The operating system releases it if the process dies.
- `backlog.recover_running(path, max_age, alive=workflow.worker_alive(root))` puts a `running` row back to `todo` only when it is older than `max_age` **and** its claim lock is not held. A long task whose worker is alive stays running.
- `python -m orgcore recover <instance> --max-age-minutes N` does the same from the command line.
- On Windows the operating system releases a dead process's lock a few milliseconds late (0 to about 20 ms measured), so a recovery pass run right after a kill can still see the worker as alive; the row is then put back on the next pass. Tests that kill a worker wait for the lock to clear (up to 2 seconds) before asserting.
- The rehearsal resumes a row it dispatched if its process was killed, continuing the same attempt instead of starting another.

## Offline rehearsal

```
python -m orgcore rehearse examples/demo-copy --rating useful
```

Run it on a copy of the demo (it modifies the board and writes `demo/` and `state/`). It touches only projects marked `"demo": true`, uses a fake executor and a fake reviewer, takes one row through `todo -> running -> review -> rework -> running -> review -> done`, records the rating, and sends nothing. The fake executor leaves out one required line on its first attempt so that the rework round happens.
