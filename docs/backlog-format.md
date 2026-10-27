# Task board format

The task board is one UTF-8 text file, `projects/backlog.md` in your instance directory. It is meant to be read and edited by people and agents alike; the code only ever rewrites the single row it changes.

```markdown
# Backlog

## Line: research
Weight: 60%
- [todo] demo-notes: Summarize the three sample articles
- [running 2026-01-05T10:00:00Z] demo-notes: Draft the outline
- [review] demo-garden: List five plants for a shaded bed
- [blocked waiting for the sample parts list] demo-bikes: Estimate the repair cost
- [done 2026-01-05T09:30:00Z] demo-notes: Create the empty notes file
```

## Grammar

| Element | Form | Rules |
|---|---|---|
| Line section | `## Line: <id>` | `<id>` is a line id from `org.json`. Each id appears once. Any other `## ` heading is an error. |
| Weight row | `Weight: <number>%` | Optional, at most one per section, directly under the heading by convention. Must be positive. |
| Task row | `- [<state>] <project>: <text>` | `<project>` is a project id in that line. `<text>` is one line. |
| State with detail | `- [<state> <detail>] ...` | Detail is free text without `]`, except for `running`. |
| Anything else | prose, blank lines, `#`/`###` headings | Passed through untouched. |

A line that starts with `- [` or `Weight:` and does not fit the grammar is an error that names the line number. Nothing is skipped silently, and a board with errors is never rewritten.

## States

| State | Meaning | Detail |
|---|---|---|
| `todo` | Waiting to be picked up | none |
| `running` | Claimed by a worker | required: claim time in UTC, `2026-01-05T10:00:00Z` |
| `review` | Delivered, waiting for an independent reviewer | optional |
| `rework` | Reviewer sent it back | optional: what to fix |
| `done` | Accepted | optional: time |
| `blocked` | Cannot proceed | optional: reason |

Allowed moves: `todo -> running | blocked`, `running -> review | blocked | todo`, `review -> done | rework | blocked`, `rework -> running | blocked`, `blocked -> todo`. The move `running -> todo` is what crash recovery uses.

## Concurrency and crashes

- Every change is locked, read, modified and saved in one step. The lock is an operating-system file lock on `backlog.md.lock`, so it disappears if the process that holds it is killed. There is no stale-lock timer to wait out.
- The file is saved by writing a temporary file in the same directory, flushing it to disk, and replacing the original. After a crash you have the old board or the new board, never a mix.
- `recover_running(path, max_age)` puts `running` rows older than `max_age` back to `todo`.
- A board you edit by hand while a worker runs is safe as long as your editor saves with the same replace-on-save behavior. Close the file in your editor before a long unattended run.
