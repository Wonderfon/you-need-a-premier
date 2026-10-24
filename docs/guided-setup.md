# Guided setup

`python bootstrap.py [--instance DIR]` is the only thing you start. Standard library only; Python 3.11+.

## What happens

1. **Local checks.** Python version; whether the official Claude Code CLI is installed. If it is not, the script prints the official install commands (native installer first, `winget` as the alternative) and stops. It never installs anything.
2. **Menu.** `1` start or resume the guided setup, `2` run the offline rehearsal, `3` exit.
3. **Consent.** Option 1 prints what will happen (the CLI opens, your subscription sign-in is used, answers are saved as files, nothing is sent, no credential is requested). Only `yes` continues.
4. **Subscription check.** The script runs `claude auth status --json` and keeps two facts: signed in, and which method. It passes only for a subscription login (`claude.ai` or `oauth_token`) and only if `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, the Bedrock/Vertex/Foundry switches and an `apiKeyHelper` (user or project settings) are all absent. It does not use the plan name. Nothing else from the status output (account, organization) is stored or printed. On failure it says what to fix; it does not sign you in.
5. **Launch.** The CLI starts in the instance directory with `--append-system-prompt-file` (the English coordinator prompt, filled in with the instance path), `--add-dir`, and permission only for the setup writer command. The coordinator asks one question at a time.

## Where answers go

```
<instance>/state/setup.json            checkpoint: per block a status and the hash of the saved answer, the next question
<instance>/state/answers/<block>.json  the answers
<instance>/state/answers/history/      answers you changed, kept
```

The coordinator saves through `python -m orgcore setup <instance> save <block> --json '{...}'`. Every answer is validated first; a bad answer changes nothing. A saved answer is never replaced unless you were asked and `--change` is given. If an answer file is edited by hand, the block returns to `needs_action` instead of being trusted. A change to `lines` or `projects` sends the blocks that depend on it back for another look.

Resuming reads these files, never a chat history: run `bootstrap.py` again and the coordinator starts from `status`.

## Blocks

`identity`, `goal`, `lines`, `projects`, `weights`, `time`, `reporting`, `spending`, `subscription`, `rules`, `install`, `confirm`. `install` is filled in by the script itself after the subscription check. `confirm` runs `compose`, which writes `org.json`, `policy.json`, `reporting.json` and an empty `projects/backlog.md` from the answers, never replacing a file that already differs.

The `time` block checks that the time zone can be resolved on this machine. If it cannot (a named zone on Windows without `tzdata`), the block is `needs_action` and you choose: change it to `UTC`, or install `tzdata` and save again.

## Setup commands

```
python -m orgcore setup <instance> start|status|show [block]|save <block> (--json TEXT | --json - | --file PATH) [--change]|accept <block>|compose
```

`accept` finishes a save that was interrupted after the answer was written but before the checkpoint.

## Offline rehearsal

Menu option 2 copies the demo into `<instance>/rehearsal`, takes one pretend row through dispatch, review, rework and done with fake workers (no model, nothing sent), prints the finished work, and only then asks whether it was useful. If a setup exists in the instance, its phase moves to `demo_running` and then `demo_ready` once you have rated.

## Not in this version

The coordinator does not yet run real work: there is no real CLI adapter for executors and reviewers, no real usage readings, and reports stay local files. Credentials are never part of setup; if you paste one into the chat, remove it.
