"""sol first review (owner\x2010-25 "1 3 4 6 做", records\x2f2026-10-25-owner-terminal-efficiency-codex-dotz.json). run() is called
BEFORE the authoritative review: a read-only sol review of the frozen evidence. When sol judges pass and the row is not drawn
for the spot check (every SPOT_EVERY-th pass, one at random), primary() turns the row into done and the deputy never
reviews it; any other outcome goes to the deputy as before, who is not shown sol's verdict.

The reservation file counts attempts (including crashes), not just completed ledger rows. Two consecutive spot checks
the deputy sends back switch sol first review off (sol-primary-state.json, `disabled`); delete that key to switch it on
again. Codex quota is tick.codex_weekly_used / CODEX_WEEKLY_MAX, as for the executors. Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

# ---- CONFIG ----
DAILY_CAP = 200  # owner\x2011-03 terminal 「提高上限到200次。维持两天先试试」 (was 60 since 10-25); review on 11-05
USAGE_MAX_AGE_S = 1800  # observe_quota: only sessions touched this recently, unless any_age
TIMEOUT_S = 600
MODEL, EFFORT = "gpt-6.1-sol", "high"
SPOT_EVERY, SPOT_STREAK = 5, 2  # one random pass in every 5 goes to the deputy; this many spot reworks in a row switch off
RNG = random.Random()
# The deputy keeps outside-facing/Opus work and reviewer-merged rows (10-04 B), except the two life repos handed off below.
OFFICIAL_SITE = "官网"
# owner\x2011-03 terminal 「现在就改」: internal reports only for the owner to read (给 owner 看, 交 owner, 通知 owner) go to sol
# first too, whatever the burn line; life audience/release work stays with the deputy, other lines may use widened().
OWNER_FACING = re.compile(r"观众试看|给\s*owner\s*试|owner\s*试用?|过审当场|对外|发布|上线", re.I)
NEGATIONS = ("不", "别", "勿", "无需", "禁止", "不要")  # a keyword right after one of these (不对外联系) is the row's own limit, not a request
MERGE_REPOS = ("Thinking Engines", "style_rewriter", "driving-course-sim", "Blade_Heart_Battle_Demo")
LIFE_MERGE_REPOS = ("style_rewriter", "poetry-mask")  # owner\x2011-01: sol reviews, life deputy merges
FOLLOWUP_MAX = 20  # rows listed in the deputy's next review prompt
# Conservative exclusion: a run that changed one of these paths continues through the existing authoritative review only.
# Only the changed-file list is matched, not the task text ("don't touch tick.py" must not exclude); the quant line and
# quant project are excluded as a whole in eligible().
EXCLUDED = re.compile(r"(^|[/\\])tick\.py$|(^|[/\\])quant[^/\\]*|broker|trading|payment|wallet|(^|[/\\])orders?([/\\.]|$)|"
                      r"量化|实盘|下单|券商|资金", re.I)
# Credential file names for the sol-first path (tick.CREDENTIAL_RE's bare `token`/`secret` also hit tokenizer_v2.json,
# trainsets_tokens.json, local-secretary-v0.md). Token files count only bare or with a service prefix (tokens.txt,
# api_token.txt, github_tokens.json); secret/credential only as a whole word (client_secret.json), not secretary.
CRED_NAME = re.compile(r"^\.env|\.dpapi$|^02_secrets$|\.pem$|\.key$|^id_rsa|^client_secret|passw(or)?d|"
                       r"(^|[_.-])(secrets?|credentials?)(?![a-z])|"
                       r"^((api|auth|access|refresh|bearer|oauth|github|gh|discord|openai|anthropic|slack|bot|session)[_.-])?"
                       r"tokens?(\.\w+)?$", re.I)
RETRY_MAX = 2  # a crash before the evidence was frozen may be retried this many more times
# This probe reads only session events; emits quota numbers and timestamp, never transcript text/config/auth.
QUOTA_SCRIPT = '''import json, time
from pathlib import Path
best = {}
files = sorted((Path.home()/".codex/sessions").glob("*/*/*/*.jsonl"), key=lambda p:p.stat().st_mtime, reverse=True)[:10]
for path in files:
    if time.time()-path.stat().st_mtime > MAX_AGE_S:
        continue
    with path.open("rb") as f:
        f.seek(max(0, path.stat().st_size-2_000_000))
        lines = f.read().decode("utf-8", errors="replace").splitlines()
    for line in reversed(lines):
        if '"rate_limits"' not in line:
            continue
        try:
            e = json.loads(line)
            limits = e.get("payload", {}).get("rate_limits")
            at = e.get("timestamp", "")
            if isinstance(limits, dict) and at > best.get("at", ""):
                best = {"at": at, "limits": limits}
        except (ValueError, AttributeError, TypeError):
            continue
print(json.dumps(best))
'''


def weekly_used(observation: dict, now: datetime) -> float | None:
    """used_percent of the weekly window, whatever the age of the observation: usage only grows inside a window, so it
    holds while the window's resets_at is in the future. Anything else is None (unreadable is never zero)."""
    try:
        for k in ("primary", "secondary"):
            w = observation["limits"].get(k)
            if isinstance(w, dict) and w.get("window_minutes") == 10080:
                used = float(w["used_percent"])
                return used if math.isfinite(used) and used >= 0 and w["resets_at"] > now.timestamp() else None
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        pass
    return None


def observe_quota(tk, any_age: bool = False) -> dict:
    """The newest rate_limits event of the 10 newest Codex sessions; without any_age only sessions touched within USAGE_MAX_AGE_S."""
    script = f"MAX_AGE_S = {1e12 if any_age else USAGE_MAX_AGE_S}\n" + QUOTA_SCRIPT
    out = subprocess.run([*tk.CODEX[:2], "python3", "-c", script], capture_output=True, text=True,
                         encoding="utf-8", timeout=30, creationflags=tk.CREATE_NO_WINDOW)
    return json.loads(out.stdout) if out.returncode == 0 else {}


def executor_for(tk, role: str, item: str) -> dict | None:
    return max((r for r in tk.load_runs() if r.get("kind") == "executor" and r.get("role") == role
                and r.get("item") == item), key=lambda r: r["_at"], default=None)


def review_id_of(role: str, section: str, item: str, ex: dict) -> str:
    """One id per executor run of a row: the same execution is never sol-reviewed twice."""
    return hashlib.sha256(json.dumps([role, section, item, ex["at"], ex.get("run_file")], ensure_ascii=False).encode()).hexdigest()[:24]


def shadow_row(tk, review_id: str) -> dict | None:
    """The ledger row of this execution's sol attempt, if any."""
    return next((r for r in tk.load_runs() if r.get("kind") == "shadow_review" and r.get("review_id") == review_id), None)


def frozen(tk, review_id: str) -> dict | None:
    """{"review_id", "review_snapshot"} of an earlier sol attempt whose frozen evidence is still intact (as run() reuses it)."""
    previous = shadow_row(tk, review_id)
    if previous and previous.get("review_snapshot"):
        evidence = Path(previous["review_snapshot"])
        if evidence.exists() and hashlib.sha256(evidence.read_bytes()).hexdigest() == previous.get("snapshot_sha256"):
            return {"review_id": review_id, "review_snapshot": str(evidence)}
    return None


def pre_snapshot_failure(r: dict) -> bool:
    """A sol attempt that crashed before any evidence was frozen (nothing was reviewed): it does not count as tried."""
    return bool(r.get("error")) and not r.get("review_snapshot")


def tried_ids(runs: list[dict]) -> set:
    """review_ids sol already took; a pre-snapshot crash is retried, RETRY_MAX more times (3 attempts in all)."""
    fails, tried = {}, set()
    for r in runs:
        if r.get("kind") != "shadow_review":
            continue
        if pre_snapshot_failure(r):
            fails[r.get("review_id")] = fails.get(r.get("review_id"), 0) + 1
        else:
            tried.add(r.get("review_id"))
    return tried | {i for i, n in fails.items() if n > RETRY_MAX}


def candidates(tk, secs: dict, runs: list[dict]) -> list[tuple]:
    """(role, section, line, item, review_id) of the [review] rows the supervisor may hand to a sol-first lane while Claude is
    held back, in file order: cheap checks only (no quant, no Opus tag, no owner-facing words, a successful executor run, no
    sol attempt yet, first review not switched off). The lane builds the review packet and applies skip_reason in full."""
    if load_state(tk).get("disabled"):
        return []
    seen = tried_ids(runs)
    latest = {}
    for r in runs:
        if r.get("kind") == "executor":
            key = (r.get("role"), r.get("item"))
            if key not in latest or r["_at"] > latest[key]["_at"]:
                latest[key] = r
    out = []
    for section, sec in secs.items():
        role = tk.SECTION_ROLES.get(section)
        if not role or role == "quant":
            continue
        for line, item in sec["review_rows"]:
            ex = latest.get((role, item))
            if (ex and ex.get("ok") and ex.get("project") != "quant" and tk.OPUS_TAG not in item
                    and ((role != "life" and widened(tk, ex)) or not owner_facing(item))
                    and (rid := review_id_of(role, section, item, ex)) not in seen):
                out.append((role, section, line, item, rid))
    return out


def widened(tk, ex: dict | None) -> bool:
    """10-26 (owner 「sol 先审扩大范围也做」): while the Codex weekly reading is 10+ points behind the burn line (tick.codex_burning),
    a Codex-executed row outside the quant line is also sol-reviewed first when it is owner-facing or touched EXCLUDED paths.
    Kept as before: quant, the Opus tag, PROTECTED, credential/risky paths and the SPOT_EVERY spot check. Life audience
    and release rows stay with the deputy; its two merge repos have a separate handoff. Other reviewer-merged rows are
    still excluded. Back to the old scope as soon as it catches up."""
    ex = ex or {}
    return ex.get("engine") == "codex" and ex.get("project") != "quant" and bool(getattr(tk, "codex_burning", lambda: False)())


def owner_facing(item: str) -> str | None:
    """The first word that makes the row owner- or outside-facing (the deputy reviews it), skipping negated mentions."""
    if OFFICIAL_SITE in item:
        return OFFICIAL_SITE
    return next((m.group(0) for m in OWNER_FACING.finditer(item)
                 if not any(n in item[max(0, m.start() - 3):m.start()] for n in NEGATIONS)), None)


def life_merge_branches(role: str, facts: dict) -> list[str]:
    """Only frozen branch refs in the two life repos need a merge handoff; reports without branches do not."""
    if role == "life" and facts.get("repo") and Path(facts.get("main_repo") or facts["repo"]).name in LIFE_MERGE_REPOS:
        return list(facts.get("refs") or {})
    return []


def skip_reason(tk, role: str, item: str, packet: dict | None, ex: dict | None) -> str | None:
    """Why this review row is left to the deputy; None when sol reviews it first. No cost limit. A row without git changes
    (no branch) is fine: its evidence is the task and the result files."""
    if role == "quant" or (ex or {}).get("project") == "quant":
        return "量化线"
    if not ex or not ex.get("ok"):
        return "没有成功的执行行"
    if not packet or packet.get("hard"):
        return "没有审查前材料或自动返工"
    if tk.OPUS_TAG in item:
        return "〔执行者：Opus〕"
    wide = widened(tk, ex)
    if (role == "life" or not wide) and (word := owner_facing(item)):
        return f"行里有「{word}」"
    facts = packet.get("facts") or {}
    paths = facts.get("changed") or []
    repo_name = Path(facts.get("main_repo") or facts.get("repo") or "").name
    life_repo = role == "life" and repo_name in LIFE_MERGE_REPOS
    life_merge = life_merge_branches(role, facts)
    if role == "life" and ex.get("project") == "writing" and tk.review_packet.BRANCH_RE.search(item) and not life_merge:
        return "生活线写作分支缺可冻结的仓库或分支，不能交接合并"
    if not life_repo and repo_name in MERGE_REPOS:
        return f"审查员可合并的仓库 {repo_name}"
    if not life_repo and paths and all(p.lower().endswith(".md") for p in paths):
        return "分支只改 .md"
    if not facts.get("result_files"):
        return "没有结果文件"
    if facts.get("refs") and not (facts.get("repo") and facts.get("base")):
        return "有分支但缺 repo 或 base，冻结不了 diff"
    if facts.get("risky"):
        return "碰了敏感路径"
    bad = next((p for p in paths if (EXCLUDED.search(p) and not wide) or tk.review_packet.PROTECTED.search(p)
                or any(CRED_NAME.search(part) for part in Path(p).parts)
                or (ex.get("project") == "umba" and "data" in Path(p).parts)), None)
    return None if bad is None else f"改了不走先审的路径 {bad}"


def eligible(tk, role: str, item: str, packet: dict | None, ex: dict | None) -> bool:
    return skip_reason(tk, role, item, packet, ex) is None


def reserve(tk, review_id: str, now: datetime, retry: bool = False) -> tuple[bool, str | None]:
    path = tk.DATA / "shadow-review-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with tk.backlog_io.lock(path):
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"attempts": []}
        attempts = state["attempts"]
        today = now.astimezone().date()
        if not retry and any(a["id"] == review_id for a in attempts):
            return False, "重复"
        if sum(datetime.fromisoformat(a["at"]).astimezone().date() == today for a in attempts) >= DAILY_CAP:
            return False, "当日上限"
        attempts.append({"id": review_id, "at": now.isoformat()})
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    return True, None


def snapshot(tk, directory: Path, item: str, packet: dict, ex: dict) -> Path:
    """Pin git objects and copy result text BEFORE either reviewer can change them. No raw-data/credential diff."""
    facts = packet["facts"]
    repo, base, refs = facts.get("repo") and Path(facts["repo"]), facts.get("base"), facts.get("refs") or {}
    parts = ["# Frozen pre-review evidence", f"Task: {item}",
             f"Executor: {ex['at']} / {ex.get('run_file')}", packet["text"],
             "Use only these immutable git objects and the result text below. For callers/tests use git show <sha>:<path>; "
             "do not read later worktrees, audits, backlog or reviewer verdicts." if refs else
             "This row has no git changes: review the task and the result text below only. "
             "Do not read later worktrees, audits, backlog or reviewer verdicts."]
    if refs:
        parts.append(f"Repository: {repo}\nBase: {base}")
    for name, sha in refs.items():
        # facts["changed"] is the union of these refs' own file lists and eligible() checked all of it, so the whole branch diff
        # is the same set; no pathspec (800+ paths overran the Windows command line: git() returned None, 11-04 OSError x4).
        # Disabling external diff/textconv avoids running project hooks.
        patch = tk.review_packet.git(repo, "diff", "--no-ext-diff", "--no-textconv", f"{base}...{sha}") if facts["changed"] else ""
        if patch is None:
            raise OSError("cannot freeze git diff")
        parts.append(f"Branch: {name}\nCommit: {sha}\n```diff\n{patch}\n```")
    audits = tk.WORKSPACE / "incore" / "audits"
    for name in facts["result_files"]:
        if Path(name).name != name or tk.review_packet.PROTECTED.search(name) or CRED_NAME.search(name):
            raise ValueError("unsafe result path")
        parts.append(f"## Result: {name}\n" + (audits / name).read_text(encoding="utf-8"))
    parts.append("## Review rules\n" + tk.read_rules() + "\n" + tk.REVIEW_AS_TRIAL)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "evidence.md"
    path.write_text("\n\n".join(parts), encoding="utf-8")
    return path


def execute(tk, evidence: Path, stream: Path) -> int:
    prompt = ("你是 sol 先审审查员，在 WSL bash 中只读审查；C:\\X 是 /mnt\x2fc/X，Windows python 用 python3。"
              "只读当前目录 evidence.md，它包含正式审查的同一份冻结材料、任务和规则。按固定提交核对调用方和测试；"
              "禁止读取后来的工作树、审查报告、backlog 或其他审查结论。规则中修改状态、补任务、发消息、合并的指示"
              "对你不适用，由调度器根据你的结论处理：不改任何文件，不合并、不部署、不联系任何人、不花钱、不读凭据。"
              "不能验证的运行面明确写出，不把未验证算通过。只有挡住使用、偏离意图或碰红线才打回。"
              "你判 pass 时这一项直接算完成，通常没有人再复核。"
              "列出证据及任何挡住使用的问题；再写一节「建议的后续」（可逆、本地、不需要 owner 决定的后续任务建议，"
              "没有就写无），副总理之后会读它；最后一行单独写 VERDICT: pass、VERDICT: rework 或 VERDICT: blocked。")
    argv = [*tk.CODEX, "exec", "-m", MODEL, "-c", f'model_reasoning_effort="{EFFORT}"', "-s", "read-only",
            "-c", 'approval_policy="never"', "--skip-git-repo-check", "--json", "-C", tk.wsl_path(evidence.parent), "-"]
    with stream.open("wb") as out:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT,
                                creationflags=tk.CREATE_NO_WINDOW)
        try:
            proc.communicate(prompt.encode("utf-8"), timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            tk.kill_tree(proc.pid)
            proc.communicate()
            return -1
    return proc.returncode


def run(tk, role: str, section: str, item: str, packet: dict | None) -> tuple[dict | None, str | None]:
    """Best-effort aid: return (review context, skip reason); never block the real review."""
    context, row, failure_reason, started = None, None, None, time.time()
    try:
        ex = executor_for(tk, role, item)
        reason = skip_reason(tk, role, item, packet, ex)
        if reason is not None:
            return None, f"不合格：{reason}"
        if load_state(tk).get("disabled"):
            return None, "已禁用"
        review_id = review_id_of(role, section, item, ex)
        rows = [r for r in tk.load_runs() if r.get("kind") == "shadow_review" and r.get("review_id") == review_id]
        retry = bool(rows) and all(pre_snapshot_failure(r) for r in rows) and len(rows) <= RETRY_MAX
        previous = None if retry else next(iter(rows), None)
        if previous and previous.get("review_snapshot"):
            evidence = Path(previous["review_snapshot"])
            if evidence.exists() and hashlib.sha256(evidence.read_bytes()).hexdigest() == previous.get("snapshot_sha256"):
                return {"review_id": review_id, "review_snapshot": str(evidence)}, None
            return None, "重复：已有尝试的取证包无效"
        now = datetime.now(timezone.utc)
        used = tk.codex_weekly_used()  # the executors' gate: at CODEX_WEEKLY_MAX no new Codex run, unreadable runs
        if used is not None and used >= tk.CODEX_WEEKLY_MAX:
            reason = f"本周额度已到上限：{used:.0f}%（上限 {tk.CODEX_WEEKLY_MAX:.0f}%）"
            tk.log("shadow_skipped", reason, role=role, item=item)
            return None, reason
        reserved, reason = reserve(tk, review_id, now, retry)
        if not reserved:
            reason = f"{reason}（{DAILY_CAP} 次）" if reason == "当日上限" else reason
            tk.log("shadow_skipped", reason, role=role, item=item)
            return None, reason
        row = {"at": now.isoformat(), "role": role, "section": section, "item": item, "project": ex["project"],
               "kind": "shadow_review", "engine": "codex", "model": MODEL, "effort": EFFORT, "cost_usd": 0.0,
               "review_id": review_id, "executor_at": ex["at"], "executor_run_file": ex.get("run_file"),
               "merge_branches": life_merge_branches(role, packet["facts"]),
               "quota": {"weekly_used": used}, "verdict": "unknown", "ok": False, "exit_code": None}
        directory = tk.DATA / "shadow-reviews" / review_id
        evidence = snapshot(tk, directory, item, packet, ex)
        context = {"review_id": review_id, "review_snapshot": str(evidence)}
        stream = directory.with_suffix(".jsonl")  # kept outside the shared evidence directory
        row.update(context, snapshot_sha256=hashlib.sha256(evidence.read_bytes()).hexdigest(), run_file=str(stream))
        # ponytail: synchronous sampling delays Opus by <=600s; use a queue only if that latency matters.
        code = execute(tk, evidence, stream)
        tid, done, text, usage = tk.codex_result(stream)
        verdict = tk.parse_verdict(text)
        row.update(exit_code=code, session_id=tid, verdict=verdict,
                   ok=code == 0 and done and verdict != "unknown", input_tokens=usage.get("input_tokens"),
                   cache_read_tokens=usage.get("cached_input_tokens"), output_tokens=usage.get("output_tokens"))
    except Exception as exc:  # shadow failure never interrupts the authoritative review
        failure_reason = f"先审失败：{type(exc).__name__}"
        tk.log("shadow_failed", type(exc).__name__, role=role, item=item)
        if row is not None:
            row["error"] = type(exc).__name__
    finally:
        if row is not None:
            row["elapsed_s"] = round(time.time() - started)
            try:
                tk.append_jsonl(tk.RUNS_FILE, row)
            except OSError:
                tk.log("shadow_ledger_failed", role=role, item=item)
    return context, failure_reason if context is None else None


def load_state(tk) -> dict:
    """sol-primary-state.json: passes, pick, decided {review_id: spot|done}, judged, streak, disabled, reminded. Missing = {};
    unreadable = disabled (never guessed as enabled)."""
    try:
        return json.loads((tk.DATA / "sol-primary-state.json").read_text(encoding="utf-8"))
    except OSError:
        return {}
    except ValueError:
        return {"disabled": {"at": "", "reason": "状态文件读不了"}}


def update_state(tk, change):
    """Run change(state) under the file lock and save it atomically; returns what change returns."""
    path = tk.DATA / "sol-primary-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with tk.backlog_io.lock(path):
        state = load_state(tk)
        out = change(state)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    return out


def decide(state: dict, review_id: str) -> str:
    """'spot' for one random pass in every SPOT_EVERY (a block of SPOT_EVERY passes holds exactly one), else 'done'. Asked
    again for the same review_id (a retry) it answers the same."""
    decided = state.setdefault("decided", {})
    if review_id not in decided:
        n = state.get("passes", 0)
        state["passes"] = n + 1
        if n % SPOT_EVERY == 0 or "pick" not in state:
            state["pick"] = RNG.randrange(SPOT_EVERY)
        decided[review_id] = "spot" if n % SPOT_EVERY == state["pick"] else "done"
        for old in list(decided)[:-200]:
            del decided[old]
    return decided[review_id]


def primary(tk, role: str, section: str, line: str, item: str, shadow: dict) -> bool:
    """sol judged this review pass and it is not drawn for the spot check: save sol's full review under audits/, turn the
    row into done, add the review row (kind review, engine codex, so the executor's first-pass rate counts it). False
    leaves the row to the deputy's review."""
    try:
        review_id = shadow["review_id"]
        rows = tk.load_runs()
        sol = next((r for r in rows if r.get("kind") == "shadow_review" and r.get("review_id") == review_id), None)
        if not (sol and sol.get("ok") and sol.get("verdict") == "pass"):
            return False
        if any(r.get("primary") == "sol" and r.get("review_id") == review_id for r in rows):
            return True
        # Old frozen life reviews did not retain the repo/refs needed for a safe merge handoff.
        if role == "life" and sol.get("project") == "writing" and "merge_branches" not in sol:
            return False
        if update_state(tk, lambda s: decide(s, review_id)) == "spot":
            tk.log("sol_spot_check", "sol pass drawn for the deputy's review", role=role, item=item, review_id=review_id)
            return False
        stream, now = Path(sol["run_file"]), datetime.now()
        text = tk.codex_result(stream)[2]
        slug = re.sub(r"\W+", "", item)[:20]
        name = f"{now:%Y-%m-%d}-{slug}-{review_id[:6]}-sol-review.md"
        (tk.WORKSPACE / "incore" / "audits" / name).write_text(
            f"# sol 先审：{item[:80]}\n\n模型 {MODEL}（{EFFORT}，只读），冻结材料 {sol.get('review_snapshot')}，review_id {review_id}\n\n{text}\n",
            encoding="utf-8")
        state = f"done {now:%m-%d %H:%M}：sol 审查通过，audits/{name}"
        if role == "life" and sol.get("merge_branches"):
            # One locked transaction: never mark done without also handing every frozen branch to the deputy.
            tk.backlog_io.apply_batch([{"section": section, "old_line": line, "expected_state": "review",
                                       "new_state": state, "insert_after": [
                                           f"- [todo] 写作模型：合并 {branch}（sol 已审过，生活副总理照 10-04 B 合并）"
                                           for branch in sol["merge_branches"]]}], tk.BACKLOG)
        elif not tk.rewrite_line(line, f"- [{state}] {item}"):
            tk.log("skipped", "backlog line changed before sol done", role=role, item=item, kind="review")
            return False
        tk.record_run(role, section, "review", EFFORT, "new", sol.get("session_id"), item, sol.get("elapsed_s"), 0, True, text,
                      stream, model=MODEL, project=sol.get("project"), metadata={
                          "engine": "codex", "cost_usd": 0.0, "review_id": review_id, "primary": "sol", "audit_file": f"audits/{name}",
                          "input_tokens": sol.get("input_tokens"), "cache_read_tokens": sol.get("cache_read_tokens"),
                          "output_tokens": sol.get("output_tokens"), "context_tokens": None})
        tk.note_verdict(role, item, "pass")
        tk.log("ran", "sol_primary", kind="review", section=section, role=role, item=item, model=MODEL, effort=EFFORT)
        return True
    except Exception as exc:  # the deputy's review is the fallback
        tk.log("sol_primary_failed", repr(exc)[:200], role=role, item=item)
        return False


def note_spot(tk, review_id: str, verdict: str) -> None:
    """The deputy's verdict on a spot-checked row: rework counts up, pass resets (blocked/unknown: unchanged); SPOT_STREAK
    reworks in a row set `disabled`. Rows that were not drawn are ignored."""
    def change(state):
        if verdict not in ("pass", "rework") or state.get("decided", {}).get(review_id) != "spot" or review_id in state.get("judged", []):
            return
        state["judged"] = [*state.get("judged", [])[-199:], review_id]
        state["streak"] = 0 if verdict == "pass" else state.get("streak", 0) + 1
        if state["streak"] >= SPOT_STREAK and not state.get("disabled"):
            state["disabled"] = {"at": datetime.now(timezone.utc).isoformat(), "reason": f"连续 {SPOT_STREAK} 次抽查副总理判 rework"}
    try:
        update_state(tk, change)
    except Exception as exc:  # the review and its verdict are already recorded
        tk.log("sol_state_failed", repr(exc)[:200], review_id=review_id)


def followups(tk, role: str) -> str:
    """For the deputy's next review prompt: the rows sol passed (done) in this role's line since the deputy's last
    review, with their sol review files, whose 「建议的后续」 the deputy turns into todo rows or not."""
    rows = [r for r in tk.load_runs() if r.get("kind") == "review" and r.get("role") == role]
    last = max((r["_at"] for r in rows if r.get("engine") != "codex" and r.get("model") != "script" and r.get("ok")), default=None)
    mine = [r for r in rows if r.get("primary") == "sol" and (last is None or r["_at"] > last)]
    if not mine:
        return ""
    return (f"\n本线自你上次审查以来，sol 先审直接判通过、已改成 done 的行（共 {len(mine)} 条，列最近 {min(len(mine), FOLLOWUP_MAX)} 条）。"
            "每个审查文件末尾有「建议的后续」：读它，由你决定补不补 todo 行（补的行照常以项目名开头）；这些行已完成，不用重审。"
            "正在验收的这一项的 sol 结论仍不要读。\n"
            + "\n".join(f"- {(r.get('item') or '')[:80]}｜{r.get('audit_file')}" for r in mine[-FOLLOWUP_MAX:]) + "\n")


def disabled_note(tk) -> str:
    """Line for the coordinator's report prompt, once, after sol first review switched itself off."""
    state = load_state(tk)
    if not state.get("disabled") or state.get("reminded"):
        return ""
    why = state["disabled"].get("reason") if isinstance(state["disabled"], dict) else state["disabled"]
    return f"报告里单独写一句：sol 先审已自动停用（{why}），需要总理或 owner 复核后再恢复。"


def mark_reminded(tk) -> None:
    try:
        update_state(tk, lambda s: s.__setitem__("reminded", True))
    except Exception as exc:  # the report is written; saying it twice is the worst case
        tk.log("sol_state_failed", repr(exc)[:200])


def summary(rows: list[dict]) -> dict:
    shadows = {r["review_id"]: r for r in rows if r.get("kind") == "shadow_review" and r.get("review_id")}
    reviews = {}
    for r in rows:
        if (r.get("kind") == "review" and r.get("review_id") in shadows and r.get("model") in ("opus", "sonnet")
                and r.get("ok") and r.get("verdict") in ("pass", "rework", "blocked")):
            reviews.setdefault(r["review_id"], r)  # same execution, first authoritative verdict only
    valid = [(s, reviews[k]) for k, s in shadows.items() if k in reviews and s.get("ok")]
    pairs = [(s, r) for s, r in valid if r["model"] == "opus"]
    other = [(s, r) for s, r in valid if r["model"] != "opus"]
    reject = lambda r: r["verdict"] != "pass"
    misses = [s["review_id"] for s, r in pairs if reject(r) and not reject(s)]
    rejected = [(s, r) for s, r in pairs if reject(r)]
    last40 = sorted(rejected, key=lambda p: p[1]["at"])[-40:]
    return {"attempts": len(shadows), "paired": len(pairs), "non_opus_pairs": len(other),
            # sol passes finished without a deputy review (kind review, primary sol); only the spot checks are paired
            "primary_done": sum(1 for r in rows if r.get("kind") == "review" and r.get("primary") == "sol"),
            "spot_pairs": sum(s["verdict"] == "pass" for s, _ in pairs),
            "non_opus_agreement": sum(reject(s) == reject(r) for s, r in other) / len(other) if other else None,
            "unpaired_or_failed": len(shadows) - len(valid),
            "agreement": sum(reject(s) == reject(r) for s, r in pairs) / len(pairs) if pairs else None,
            "three_way_agreement": sum(s["verdict"] == r["verdict"] for s, r in pairs) / len(pairs) if pairs else None,
            "opus_rejected": len(rejected), "misses": misses,
            "miss_rate": len(misses) / len(rejected) if rejected else None,
            "last40_rejected": len(last40), "last40_misses": sum(not reject(s) for s, _ in last40),
            "promotion": "Manual review required: at least 40 Opus rejections, <=2 misses in the last 40, "
                         "and no miss that blocks use. Never auto-promote."}


if __name__ == "__main__":
    import argparse
    import tick
    parser = argparse.ArgumentParser(description="Read-only shadow trial statistics")
    parser.add_argument("--runs", type=Path, default=tick.RUNS_FILE)
    args = parser.parse_args()
    tick.RUNS_FILE = args.runs
    print(json.dumps(summary(tick.load_runs()), ensure_ascii=False, indent=2))
