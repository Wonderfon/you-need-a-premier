# Restoring the incore from the daily ZIPs (new machine, no code yet)

Every ZIP in the Drive folder `Org Core — Private Input Archive` carries this file as `restore/README.md` and the
backup program as `restore/backup.py` (always, even when unchanged). Python 3.12 and Git are all that is needed to start.
Steps 1–4 (the original Windows user, WSL, Drive for Desktop, Git/Python/Node/Claude Code installs) are in
`audits/2026-10-13-org-restore-checklist.md` ④; the owner does the logins.

All ZIPs are in `G:\My Drive\Org Core — Private Input Archive`. Pick the newest ZIP by file name
(`<date>-<snapshot>.zip`). Each ZIP names its predecessor in `manifest.json`; the restore follows the chain by itself, so
keep the whole folder together.

1. **Take the restore program out of the newest ZIP** (PowerShell; `<newest.zip>` is the full path):

   ```powershell
   $work = 'C:\restore-work'
   New-Item -ItemType Directory $work
   Expand-Archive -LiteralPath '<newest.zip>' -DestinationPath "$work\head" -Force
   ```

   `restore\backup.py` is now at `C:\restore-work\head\restore\backup.py`.

2. **Restore the whole chain into a new folder** (it must not exist yet; every file is checked against its SHA-256):

   ```powershell
   python C:\restore-work\head\restore\backup.py --restore-zip '<newest.zip>' --restore-to C:\restore-work\out
   ```

   Output ends with `"verification": "zip_chain_files_sha256_verified"`.

3. **Clone the code** from the git bundle inside the restored tree (all `master` and `claude/*` branches come with it):

   ```powershell
   git clone C:\restore-work\out\git\incore.bundle <workspace>\incore
   ```

   If the bundle is missing from `out\git\`, find the newest ZIP whose `manifest.json` lists `git/incore.bundle` in
   `included_paths` and extract that member by hand. Other projects' bundles are in `out\git\projects\`
   (`git clone <bundle> <folder>`).

4. **Copy the state back** from `C:\restore-work\out\` into the clone: `records\`, `input\data\`, `discord\data\`,
   `control\data\` (the `.sqlite3`, `*.json`, `*.jsonl`), `audits\`, `projects\`, `meetings\`, `review-audio\`,
   `data\config-sync\`, `discord\data\attachments\`, `notify\data\` (send receipts), and from `data\`: `owner-todo-ids.json`,
   `review-dismissed.json` (if present), `machine-health.jsonl`; from `input\data\`: `llm-postprocess.json`. Then, outside the clone:
   - `external\llm-prompt\<name>` → the path written in `prompt_path` of `input\data\llm-postprocess.json` (edit the path if the new machine differs)
   - `external\claude-memory\` → `<user_home>\.claude\projects\<workspace 路径转成的目录名>\memory\`
   - `external\claude-skills\` → `<user_home>\.claude\skills\`
   - `external\claude-home\settings.json`, `CLAUDE.md` → `<user_home>\.claude\`; `AGENTS.md` → `<workspace>\`
   - `external\wsl-home\`: `codex-config.toml` → `~/.codex/config.toml`, `settings.json` → `~/.claude/settings.json`,
     `gitconfig` → `~/.gitconfig` (inside WSL)

Left for the owner to fill in on the new machine (not in any ZIP): `input\data\private-https.json` (the input station's HTTPS
setting), the local model and llama server files named in `llm-postprocess.json` (`model_path`, `server_exe`; download again).

Not in any ZIP, on purpose: `auth.json`, `.credentials.json`, `.env`, `*.dpapi`, secrets, tokens and keys. Log in again and
re-create them; the list of names and where to get them is in the restore checklist, section 三.
