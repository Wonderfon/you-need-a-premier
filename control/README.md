# Incore control queue

`control.py` is a local stdlib queue for owner intake. It stores queue state and receipts in `data/control.sqlite3`. Original voice/text revisions stay in `incore/input/data/input.sqlite3`; task rows retain revision IDs, ancestry and SHA-256. Discord tasks retain message IDs and read their exact text from the allowlisted Discord archive. Corrections append as separate source pointers.

`import-input` accepts only `kind='instruction'` revisions marked `explicit_user_instruction`; it is idempotent. `watch-intake` polls that source plus new owner/server/channel-verified Discord `/ask` rows, every 60 seconds by default. It only imports and reports counts. It never probes a model, dispatches work, or changes pause state. Imported coordinator inputs wait in `needs_coordinator_review`; a coordinator must explicitly hand one to the work deputy.

The only model call in this release is a manually invoked, one-turn work-deputy intake analysis. The runner uses the fixed `claude-opus-5-5` / `high` model, stdin input, no tools/MCP/skills/settings, no retry and a 180-second hard limit. It checks local and Discord pause state and reservations every second while running; a pause, overlapping reservation or hard timeout kills only this process tree and leaves the task blocked for review. Environment-based paid/API routing fails closed.

Before a call, import an owner-observed built-in `/usage` snapshot younger than five minutes. It must state `complete_visible_snapshot: true`, contain each actually displayed window and list any unreported windows explicitly. Every reported window is checked; a missing seven-day window remains unknown, never zero. New tasks stop with five points of headroom below 70%, and 80% is the hard boundary. Paid overage is blocked unless a recent account event explicitly says `overageDisabledReason: org_level_disabled`; `isUsingOverage: false` alone does not prove that. The run command requires `--no-probe`, so no automatic usage probe or unattended model dispatch is enabled.

## Local operations

```powershell
python incore/control/control.py init
python incore/control/control.py import-input --instruction-id UUID
python incore/control/control.py import-discord
python incore/control/control.py watch-intake --interval 60
python incore/control/control.py observe-quota --file incore/audits/claude-usage-manual-startup.json
python incore/control/control.py status
python incore/control/control.py run-one --task-id UUID --no-probe
python incore/control/control.py accept --task-id UUID --evidence incore/audits/acceptance.json
```

Dispatch starts paused and disabled. `enable` and `resume` only arm a manually invoked call; current quota, pause, reservation, role, source, and overage gates still apply. A successful model response becomes `reviewing`, never `done`. The coordinator can mark it `done` only with a separate acceptance command referencing evidence inside `incore/audits/`.

The input and Discord archives remain the source of truth; output JSONL and SQLite are local user data under ignored `data/`. No control command prints input text, and initialization does not import old Discord history or dispatch a model.
