# Backup

Continuous local snapshots plus a daily incremental ZIP to a synced folder (for example Google Drive for desktop). Python standard library only: no model or network API. Nothing is ever deleted from the originals.

## What it saves
- `records/` (your original instructions), the Discord and input SQLite databases, `input/data/sessions/` (recordings and chunks), and the control databases when present.
- Organization state: `projects/*.md`, `audits/`, `data/config-sync/` (including your private layer), the Claude memory folder, selected control ledgers and one git bundle per repository (only re-made after new commits).
- Never saved: `.env` and dotfiles, `*.dpapi`, any path segment containing `secret`, `token`, `key`/`keys`, `apikey`, and the forbidden folder names (`secrets`, `credentials`, `config`, `logs`, `models`, `node_modules`, …). A repository whose history contains such files is not bundled.

SQLite databases are copied with the online backup API (not raw file copies), one consistent snapshot per database.

## How it stores
- **Local:** every 60 seconds a content-addressed snapshot goes to `data/backups/` (`objects/<hash prefix>/<SHA-256>`, `snapshots/<id>.json`). Identical audio is stored once; unchanged runs reuse the manifest.
- **Daily ZIP:** once a day (20:00 America/New_York by default; the hour and time zone are set in `backup.py`) the watcher checks for changes and stages one `YYYY-MM-DD-<snapshot_id>.zip` in the target folder only if something changed. The first ZIP is a full baseline; later ZIPs hold only new or changed files and name their predecessor in `manifest.json`. Keep every ZIP back to the baseline. Recordings are paired with their latest text (`audio/<session>.webm`, `.txt`, `.json` with all revisions and hashes).
- A ZIP is accepted only after the local file, every member and the synced-folder read-back verify. A failure never advances the chain.

## Install and use
Set `DEFAULT_TARGET` in `backup.py` to your synced folder (it must already exist and be mounted) or pass `--target`; `--drive` uses `DEFAULT_TARGET`. Then:

```powershell
python incore/backup/backup.py --once                  # one local snapshot
python incore/backup/backup.py --once --drive          # force a baseline or increment into the Drive folder
python incore/backup/backup.py --watch --drive --interval 60   # local every 60 s, daily ZIP
python incore/backup/backup.py --restore-zip '<path>\<latest>.zip' --restore-to 'C:\restore\new'
```

Stop the watcher with Ctrl+C or by creating `data/backups/STOP` (it is never auto-deleted). The program does not install a startup task; start it and watch it yourself. Restore verifies the whole ZIP chain, refuses path traversal, absolute paths, symlinks and case conflicts, checks every SHA-256 and SQLite `quick_check`, and never overwrites an existing folder.

`status.json` is the receipt the UI reads: `local_snapshot_verified`, `staged_for_drive_sync` (waiting for the sync client, not a cloud receipt) or `backup_failed` / `staging_error`. `cloud_received` is always false here; a remote check of the downloaded ZIP is recorded separately in `cloud-receipt.json`.

Offline checks: `python incore/backup/test_backup.py` (selection and exclusions, WAL restore, revision lineage, baseline independence, daily gate, failure handling, traversal refusal) and, for the optional website-data pull, `test_arbitros_pull.py`.