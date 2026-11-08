# Input console

A single-user local web page for turning speech into text you can trust: record, transcribe, check and edit, copy. Nothing leaves your machine.

## What it does
- **Record → stop and transcribe → check and edit → copy.** Every edit is appended as a new revision; the original, every earlier version and the audio are kept. Copying text does not create a task.
- **Two kinds of edit:** dictation fixes (what the recognizer got wrong) and rewrites (how you would say it). Each revision records its source, UTC time, content hash and parent version.
- **Safe uploads:** the browser writes audio chunks to IndexedDB about every 5 seconds, then uploads them to the local server; unconfirmed chunks stay in a retry queue and survive a page refresh. The full audio is assembled only when the chunks are continuous and their length and SHA-256 check out.
- **Rules panel:** it suggests a new correction rule when the same "heard → corrected" change appears in two or more of your records and no confirmed text contradicts it. Accept applies it to new transcripts only; Ignore stops suggesting it; your own rules can be deleted, built-in rules (`postprocess.RULES`) can only be disabled. Rules live in `data/config-sync/input-rules.json` (inside the backup scope). Code: `suggest.py`, `rules.py`.
- **Backup status footer:** reads `data/backups/status.json` and the separate `cloud-receipt.json`; local snapshot, copy to the synced folder and remote content check are shown separately, and a failure never prints raw exceptions or paths.

## Install
From the workspace root, with the Python environment that has `aiohttp` (no other packages needed):

```powershell
incore\discord\.venv\Scripts\python.exe -m incore.input.server
```

Open `http://127.0.0.1:8765/`. The server binds to `127.0.0.1` only; do not expose it to the LAN or through a public proxy. Data (SQLite, raw recordings, text versions) is stored in `incore/input/data/`.

Speech recognition is optional and pluggable: after a recording ends the server calls `incore.input.transcribe.transcribe(audio_path: Path) -> dict`, which must return a dict with a string `text`. If no recognizer is installed or it fails, the page says so and the audio stays playable and downloadable.

## Use
1. Press record, speak, press stop; the transcript appears as the first version.
2. Edit it (each save is a revision) or pick a version to start from.
3. To hand a version to the organization, explicitly create an instruction from it; export created instructions as JSON for the coordinator. Exporting does not run anything; the real task state is in the control queue.

The service writes no request bodies or transcripts to ordinary logs. Tests use a temporary directory and call no model or network service: see the `test_*.py` files in this folder.