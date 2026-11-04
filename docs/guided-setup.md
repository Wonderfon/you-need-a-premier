# Guided setup

`python bootstrap.py [--instance DIR]` is the only thing you start. Standard library only; Python 3.11+.

## What happens

1. **Local checks.** Python version; whether the official Claude Code CLI is installed. If it is not, the script prints the official install commands (native installer first, `winget` as the alternative) and stops. It never installs anything.
2. **Menu.** `1` start or resume the guided setup, `2` run the offline rehearsal, `3` exit.
3. **Consent.** Option 1 prints what will happen (the CLI opens, your subscription sign-in is used, answers are saved as files, nothing is sent, no credential is requested). Only `yes` continues.
4. **Subscription check.** The script runs `claude auth status --json` and keeps two facts: signed in, and which method. It passes only for a subscription login (`claude.ai` or `oauth_token`) and only if `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, the Bedrock/Vertex/Foundry switches and an `apiKeyHelper` (user or project settings) are all absent. It does not use the plan name. Nothing else from the status output (account, organization) is stored or printed. On failure it says what to fix; it does not sign you in.
5. **Launch.** The CLI starts in the instance directory with `--append-system-prompt-file` (the English coordinator prompt, filled in with the instance path), `--add-dir`, the tools `Read` and `Write` only (no shell) and two hooks. The coordinator asks one question at a time.

The session cannot run commands. It saves an answer by writing one JSON file, `state/inbox/<block>.json` (`<block>.change.json` to replace a saved answer). Windows has no working "Write, but only here" allow rule (tried five forms; only a bare `Write` matched), so two hooks do it: a `PreToolUse` hook (`setup_hook.py <instance> guard`) allows exactly `*.json` files directly in `state/inbox/` and refuses every other write, and a `PostToolUse` hook (`setup_hook.py <instance>`) validates and saves the file, deletes it, and hands the result, the status and the next question back to the model. Writing `confirm.json` also composes the files. Checked on CLI 2.1.310 with no user or project settings (`--setting-sources ''`): no approval prompt, a write outside the inbox refused. Then walked through in a real Windows terminal (ConPTY, CLI 2.1.314, permission mode "manual"): all 12 blocks, a change to a saved answer, and a refused answer, with no tool approval prompt.

One question comes from the CLI itself, not from this setup: the first time it opens in a folder it has not seen, it asks "Is this a project you created or one you trust?" with **"No, exit" selected by default**. Move down to "Yes, I trust this folder" and press Enter. Pressing Enter at once closes the session; run the script again and it continues from the next unanswered question.

## Where answers go

```
<instance>/state/setup.json            checkpoint: per block a status and the hash of the saved answer, the next question
<instance>/state/answers/<block>.json  the answers
<instance>/state/answers/history/      answers you changed, kept
```

The coordinator saves through the inbox files above; by hand, use `python -m orgcore setup <instance> save <block> --json '{...}'`. Every answer is validated first; a bad answer changes nothing. A saved answer is never replaced unless you were asked and `--change` is given. If an answer file is edited by hand, the block returns to `needs_action` instead of being trusted. A change to `lines` or `projects` sends the blocks that depend on it back for another look.

Resuming reads these files, never a chat history: run `bootstrap.py` again and the coordinator starts from the setup state printed at the end of its prompt.

A successful `save` is already an accepted answer: the checkpoint is written in the same call. `accept` exists only to finish a save that was interrupted in between; you never need it in a normal run.

### Answer shapes (for saving by hand)

The exact examples the coordinator follows are in `prompts/setup-coordinator.md`. In short:

| block | shape |
|---|---|
| `identity` | `{"organization_name": "...", "owner_name": "...", "instance_id": "slug"}` |
| `goal` | `{"goal": "..."}` |
| `lines` | `{"lines": [{"id": "slug", "name": "..."}]}` |
| `projects` | `{"projects": [{"id": "slug", "name": "...", "line": "<line id>", "path": null}]}` every line needs a project |
| `weights` | `{"lines": {"<line id>": 1}, "projects": {"<project id>": 1}}` numbers above 0, one per id |
| `time` | `{"timezone": "UTC"}` |
| `reporting` | `{"report_times": ["09:00"], "quiet_hours": {"start": "23:00", "end": "08:00"}, "preferred": "local"}` |
| `spending` | `{"currency": "USD", "period": "month", "cap": 0}` |
| `subscription` | `{"windows": [{"name": "5-hour", "reserve_percent": 20, "max_share_percent": 50, "max_observation_age_minutes": 30}], "max_concurrency": 1}`; once the account has reported usage, a window it never reports (for example 7-day on an enterprise plan, which reports only 5-hour) is refused, because it would keep the gate closed forever |
| `rules` | `{"text": "..."}` |
| `confirm` | `{"confirmed": true}` after every other block is accepted |

On Windows PowerShell 5 a `--json '{...}'` argument loses its double quotes; use `--file answer.json` or `--json -` with the text piped in (a `--file` under `<instance>/state/tmp/` is deleted after it saved; any other file is left alone). PowerShell 7 and bash take the single-quoted form as written.

After the last block, `python -m orgcore setup <instance> status` ends with a `Next step:` line naming the command to run next.

## Blocks

`identity`, `goal`, `lines`, `projects`, `weights`, `time`, `reporting`, `spending`, `subscription`, `rules`, `install`, `confirm`. `install` is filled in by the script itself after the subscription check. `confirm` runs `compose`, which writes `org.json`, `policy.json`, `reporting.json` and an empty `projects/backlog.md` from the answers, never replacing a file that already differs.

The `time` block checks that the time zone can be resolved on this machine. If it cannot (a named zone on Windows without `tzdata`), the block is `needs_action` and you choose: change it to `UTC`, or install `tzdata` and save again.

## Setup commands

```
python -m orgcore setup <instance> start|status|show [block]|save <block> (--json TEXT | --json - | --file PATH) [--change]|accept <block>|compose|ingest|guard
```

`ingest` saves the files waiting in `state/inbox/` (what the hook runs); `guard` reads a tool call on standard input and prints the hook decision.

`accept` finishes a save that was interrupted after the answer was written but before the checkpoint.

## Offline rehearsal

Menu option 2 copies the demo into `<instance>/rehearsal`, takes one pretend row through dispatch, review, rework and done with fake workers (no model, nothing sent), prints the finished work, and only then asks whether it was useful. If a setup exists in the instance, its phase moves to `demo_running` and then `demo_ready` once you have rated.

After the rating, a setup that has been composed (it has an `org.json`) asks: "Keep using the offline demo, or enable real Claude workers?" `1` keeps the demo and changes nothing. `2` lists the real projects, takes a waiting task (or a new task line), shows what would be handed to the model and writes the consent only after you type `yes`; then the phase becomes `live_ready`, or `live_waiting_for_usage` with the reasons if a check fails. See `docs/real-workers.md`.

## Not in this version

Real work runs one unit at a time on your command (`python -m orgcore run`); there is no unattended loop yet, and reports stay local files. Credentials are never part of setup; if you paste one into the chat, remove it.
