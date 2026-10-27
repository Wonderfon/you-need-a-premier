# Owner intent, feedback summary and local reports

## Intent files

One page per project: `projects/intent/<project>.md`. It holds the owner's own words, the date, and where the words came from, so work can point back to the sentence it serves.

```
# Intent: demo-notes

- [I-001] 2026-01-05 | source: chat-2026-01-05 | "Keep the notes under one page."
- [I-002] 2026-01-09 | source: chat-2026-01-09 | "Add a reading list." | superseded-by: I-003
- [I-003] 2026-01-12 | source: chat-2026-01-12 | "Add a reading list, five items at most."
```

- Ids are `I-001`, `I-002`, ... per project and are never reused. A replaced entry stays in the file with `superseded-by`, so the history stays readable.
- Headings and prose around the entries are ignored; every line starting with `- [` must be a valid entry.
- A task row cites intent anywhere in its text: `intent: I-001` or `intent: I-001, I-003`.
- `python -m orgcore check` reports, together with its other findings: an intent file with a malformed line, a bad date, a duplicate id or a dangling `superseded-by` (each with its line number); a row that cites an id that does not exist, one that is superseded, or a project that has no intent file. A damaged file is reported, never skipped and never overwritten.
- `orgcore.intent.add(root, project, quote, source, day, supersedes=None)` appends the next entry under a lock. Quotes are one line without double quotes.

## Feedback summary

`python -m orgcore feedback <instance>` reads `state/feedback.jsonl` (written by `workflow.record_feedback`) against the task board and prints: the number of rated rows by latest rating, finished rows nobody has rated yet, and rows whose latest rating is `rework`. A later rating replaces an earlier one; the file is append-only.

## Local reports

`python -m orgcore report <instance>` writes the report that is due now, if any, to `reports/<date>-<HHMM>.md` (for example `reports/2026-01-05-0900.md`). Run it from any scheduler you like, as often as you like; a report is written at most once per slot.

Settings come from `reporting.json`: `timezone`, `report_times`, `quiet_hours`. An empty `report_times` means no reports.

**One slot = local date + local time + channel.** The channel is `local`. The key is printed inside each report.

| Situation | What happens |
|---|---|
| The command runs again for a slot that has a file | Nothing is written; the file is untouched. |
| It runs within 60 minutes after the slot time | `Kind: scheduled`. |
| It runs later | `Kind: catch-up`, with the due time and the time of writing. |
| Several slots were missed | One report, for the latest missed slot. Earlier ones inside the last 24 hours are recorded as skipped in `state/reports.jsonl` and named in that report; they are never written later. Slots older than 24 hours are forgotten. |
| Clocks go back and a local time happens twice | One slot: the first occurrence. |
| Clocks go forward over a report time | The slot is due when the clock jumps past it. |
| Slot time inside `quiet_hours` | The report is written, with a `Quiet hours` line. Nothing is pushed anywhere in v0.1; delivery channels added later must respect the setting. |

Contents (English): board counts per line and the rows waiting for review, sent back, or blocked; ledger activity since the previous slot; the feedback summary; the latest usage reading per window from `state/usage.json` (a missing or unreadable file is reported as "no reading", never as zero) and whether the dispatch gate is open.

The time zone is resolved with Python's `zoneinfo`. On Windows, if the zone database is missing, install the `tzdata` package or set `"timezone": "UTC"`; the command then says so and writes nothing.
