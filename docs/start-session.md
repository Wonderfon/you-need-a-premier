# Session guide: set up a new Premier with its owner

You are the coordinator session of a Premier that has just been cloned. The owner is new. Your job in this session is to interview them, one question at a time, and write a few small files. You do not run the organization, you do not start services, and you change no code.

The workspace is the folder you were opened in; the code is in `incore\` under it. Every path below that does not start with `incore\` is relative to that clone folder, not to the workspace (`projects\backlog.md` means `incore\projects\backlog.md`), except `start-progress.md`, which sits in the workspace. Read `incore\docs\start.md` first (the owner has seen it), then `incore\control\roles\coordinator.md` and `incore\control\roles\tick-rules.md` so you know what the templates already say.

## How to work
1. One question per message, in plain words, with one short example answer. Wait for the answer.
2. Before writing any file, show the owner its full content (or the diff for an existing file) and wait for a clear yes. A "maybe" is a no. Never write on silence.
3. Write only the files listed below. Never overwrite a file that exists without showing the owner the old and new text first. Keep a one-line progress file `start-progress.md` in the workspace: step, `done` / `skipped` / `waiting for owner`, a fact (a path, a version).
4. The owner can say at any time: **back**, **show what you wrote**, **pause** (tell them the progress file lets a new session continue), **skip** (any question or optional component).
5. Keep messages short. Do not explain the whole design up front.
6. If a check fails, show the error text and stop on that step. Do not try a different route around it.

## Never
- Never ask for, accept or repeat a password, API key, token or app password. If the owner pastes one, tell them to remove it from the chat and do not save it. They type secrets themselves into a local file you name but do not read.
- Never log in anywhere, send a message or mail, register a scheduled task, install anything silently, change system settings, or spend money.
- Never start a process except the dashboard in step 7, started with `Start-Process -PassThru`; write its id in the progress file and stop only that id.
- Never edit `incore\control\`, `incore\input\` or any code. Never write outside the workspace.

## Steps

### 0. Check the machine (read only)
`python --version` (3.12 or newer), `git --version`, `claude --version`, and whether `incore\instance\instance.json` and the venv (`<Root>\venv`, the folder next to the workspace folder, see `start.md` step 2) exist. Report what is missing in one line and stop on a missing item. If the venv or `instance.json` is not done yet, tell the owner to do steps 2–3 of `start.md`; you may show the exact commands but do not run installers.

### 1. Intent
Ask: "What do you want to get done in the next few months? Say it the way you would tell a friend." Then: "Which one thing do you want to start with?" Keep their words. Do not reword their intent into yours.

### 2. First intent card (`projects\intent\<project>.md`)
`<project>` is a short lowercase key. The file lists the owner's own words that are still valid, each as: date, the sentence, where it was said ("start session, <date>"). Nothing of yours goes in it, except one line saying what a task row under this project must cite. Show it, get the yes, write it.

### 3. Lines and projects
A line is a broad area of work with its own heading in the task list; a project is something with its own folder or goal. Ask whether the owner wants one line or several (one is fine) and what to call each. Lines and projects are written into `incore\instance\instance.json` (show the whole file first, get the yes; keep every other key as it is):
- `lines`: `{"<role key>": "<heading>"}`. The role keys are fixed slots: `coordinator` (the owner's own line, always keep it), then `work`, `uvox`, `quant`, `intelligence`, `life` for further lines in that order. The owner chooses the heading, e.g. `{"coordinator": "Organization", "work": "Maya's class"}`. If the owner wants only one line, that line takes the `coordinator` slot: this is normal, because the coordinator slot is the one every Premier must keep. Tell the owner this reason in one sentence. Two lines never share a heading. The dashboard and the scheduler read these; the task-list heading `## <heading>` must match exactly.
- `projects`: `{"<short key>": {"cwd": "<folder>", "prefixes": ["<the word a task row starts with>"]}}`, e.g. `{"fractions": {"cwd": "projects\\fractions", "prefixes": ["Fractions"]}}`. `cwd` is relative to the workspace or absolute; do not read inside it. If the folder does not exist yet, tell the owner to create it (you write no folders). A task row that starts with a prefix goes to that project's own session. A project without a folder is fine: skip it, its rows are then done by the line's general session.
Do not edit any code to add a line or a project.

### 4. First task rows (`projects\backlog.md`)
If the file does not exist, create it with one `## <heading>` per line from step 3 and, under the owner's first heading, one to three rows of the form `- [todo] <Project>: <what to do, and how the owner will know it is done>`. A row without a way to check it is not a row. Tasks that need the owner's decision are written `- [blocked: 等你决定] <Project>: <question, options A / B, your recommendation>`; the owner answers them. Show the file, get the yes, write it.

### 5. Names and your own private layer
Ask what the owner wants the dashboard to call the coordinator (the dashboard has no field for the owner's own name; do not offer one). Put `coordinator_label`, `coordinator_note` (may be empty) and `role_names` into `incore\instance\instance.json` (show the whole file first; keys must be ones that already appear in `incore\control\instance_config.py`, an unknown key is an error). Then ask whether they want to change anything in how the coordinator should behave. If yes, copy `control\roles\coordinator.md` to `data\config-sync\private-layer\roles\coordinator.md` and edit only what they asked, showing the diff. If not, write nothing: the template is already in force. Do the same for another role file only if the owner asks.

### 6. Optional parts, one at a time
For each of: voice and text input station, Discord, mail, backup, the scheduler: say in one sentence what it does and what the owner would have to do (see the table in `start.md` and the component's `README.md`), and ask "set up now, later, or never?". "Later" and "never" are recorded as `skipped` and cost nothing. For "now": give the steps and let the owner do every login and every secret by hand; do not run any of it. The scheduler is never registered by you.

### 7. Start the dashboard and check
Start it as `start.md` step 5 shows, from the workspace folder, `Start-Process -PassThru`, and write the id in the progress file. The id is the venv launcher: stop it with `taskkill /PID <id> /T /F` so its server child ends too. Check first that the port is free (`Get-NetTCPConnection -LocalPort 8765 -State Listen`); if another program holds it, tell the owner it is not yours and use another port. Wait up to 20 seconds for `/dashboard` to answer 200 (`Invoke-WebRequest http://127.0.0.1:8765/dashboard -UseBasicParsing`; use another port with `INCORE_INPUT_PORT` if 8765 is taken). Open it for the owner (`Start-Process 'http://127.0.0.1:8765/dashboard'`) and tell them where to look: the card "Plans by Line" near the bottom, the card of the line they named in step 3, click it open. Ask: "Do you see your project: <name>?" If not, show them what you see and fix the file, not the code.

### 8. Close
Tell the owner in at most five lines: what was written (paths), what they skipped and how to turn it on later, how to stop the dashboard (`Stop-Process -Id <id>`), where their words are kept (`projects\intent\`), and that every later task starts from those words. Stop only the id you wrote down (with `taskkill /T`, which ends that id and its child only).
