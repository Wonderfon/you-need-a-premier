"""Read-only project delivery/merge inventory; only write() persists derived snapshots.

Sources: accepted backlog rows and local git metadata. No fetch, checkout or project code execution.
"""
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path

import review_packet
import instance_config as ic  # noqa: E402  (this machine's paths and owner identity; defaults = today's values)

AGE_LIMIT = 7
BRANCH_LIMIT = 5
STALE_HOURS = 9  # one 8-hour review plus a scheduling margin
OWNER_NAMES = set(ic.V["owner_aliases"])
ROW = re.compile(r"^- \[(done(?![\w-]).*?)\]\s+(.+)$")
TAG = re.compile(r"给\s*owner\s+(\d{2}-\d{2})\s*[：:]")
RC = re.compile(r"(?:^|[-/])rc(?:$|[-/.])", re.I)


def tag_date(stamp: str, today: date) -> date | None:
    try:
        day = date.fromisoformat(f"{today.year}-{stamp}")
        if day > today and today.month == 1 and day.month == 12:
            day = day.replace(year=today.year - 1)
        return day if day <= today else None
    except ValueError:
        return None


def accepted_rows(text: str, tk, today: date) -> dict:
    """Completion notes are authoritative; historical 原状态 text is not a new receipt."""
    out = {p: {"deliveries": [], "approved": {}} for p in tk.PROJECTS}
    for number, line in enumerate(text.splitlines(), 1):
        match = ROW.match(line)
        if not match:
            continue
        status, item = match.groups()
        project = tk.project_of(item)
        if project not in out:
            continue
        status = status.split("原状态", 1)[0]
        for stamp in TAG.findall(status):
            day = tag_date(stamp, today)
            if day:
                out[project]["deliveries"].append((day, f"backlog:{number} 给 owner {stamp}"))
        # Modern rows name the reviewed branch in the completion note. Legacy done rows may name it only in the body.
        modern = bool(review_packet.BRANCH_RE.search(status))
        source = status if modern else item.split("原文", 1)[0].split("原问题", 1)[0]
        branches = list(review_packet.BRANCH_RE.finditer(source))
        for branch in branches if modern else branches[:1]:  # legacy body can also name an unreviewed base branch
            sha = re.match(r"[`\s（(：:,，]*([0-9a-f]{7,40})(?![0-9a-f])", source[branch.end():])
            out[project]["approved"].setdefault(branch.group(), set()).add(sha[1] if sha else None)
    return out


def repo_inventory(folder: Path) -> dict:
    git = lambda *args: review_packet.git(folder, *args)
    root = git("rev-parse", "--show-toplevel")
    if root is None:
        return {"state": "unknown", "reason": "仓库不可读或不存在"}
    refs = git("for-each-ref", "refs/heads", "--format=%(refname:short)|%(objectname)")
    if refs is None:
        return {"state": "unknown", "reason": "分支读取失败"}
    tips = dict(line.split("|", 1) for line in refs.splitlines() if "|" in line)
    remote = (git("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD") or "").strip()
    default = remote.removeprefix("origin/") if remote.startswith("origin/") else None
    default = default if default in tips else next((b for b in ("main", "master") if b in tips), None)
    if default is None:
        return {"state": "unknown", "reason": "默认分支未知"}
    bases = [default, *(b for b in tips if RC.search(b))]
    merged = set()
    for base in bases:
        names = git("for-each-ref", "refs/heads/claude", f"--merged={base}", "--format=%(refname:short)")
        if names is None:
            return {"state": "unknown", "reason": "合并关系读取失败"}
        merged.update(names.splitlines())
    return {"state": "ok", "root": root.strip(), "default": default, "bases": bases,
            "tips": tips, "merged": merged}


def owner_merge(folder: Path, inventory: dict, today: date, scopes: tuple = (".",)) -> tuple | None:
    # First-parent owner merges only: feature commits, RC updates and agent merges do not reset delivery age.
    history = review_packet.git(folder, "log", inventory["default"], "--first-parent", "--merges", "--full-history",
                                "--format=%H|%cI|%an|%cn", "--", *scopes)
    if history is None:
        raise OSError("owner 合并历史读取失败")
    found = []
    for line in history.splitlines():
        parts = line.split("|")
        if len(parts) != 4 or parts[2] not in OWNER_NAMES or parts[3] not in OWNER_NAMES:
            continue
        try:
            day = datetime.fromisoformat(parts[1]).astimezone().date()
        except ValueError:
            continue
        if day <= today:
            found.append((day, f"owner merge {parts[0][:12]} ({Path(inventory['root']).name})"))
    return max(found, default=None)


def collect(tk, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    today = now.astimezone().date()
    try:
        text = tk.BACKLOG.read_text(encoding="utf-8")
        backlog_ok = True
    except OSError:
        text, backlog_ok = "", False
    accepted = accepted_rows(text, tk, today)
    cache, projects = {}, []
    for project, (cwd, prefixes) in tk.PROJECTS.items():
        dates = list(accepted[project]["deliveries"])
        branches, repos, issues = [], [], [] if backlog_ok else ["backlog 读取失败"]
        seen = set()
        for folder in (cwd, *tk.PROJECT_ADD_DIRS.get(project, ())):
            folder = Path(folder)
            # Projects inside one repository still have separate backlog approval sets and path-scoped merge dates.
            inv = cache.setdefault(folder, None)
            if inv is None:
                inv = cache[folder] = repo_inventory(folder)
            if inv["state"] != "ok":
                issues.append(f"{folder.name}：{inv['reason']}")
                continue
            root = Path(inv["root"])
            if root in seen:
                continue
            seen.add(root)
            repos.append({"name": root.name, "default": inv["default"], "bases": inv["bases"]})
            try:
                relative = folder.resolve().relative_to(root.resolve()).as_posix()
                scopes = ["." if relative == "." else f":(top){relative}"]
                if relative == ".":
                    for other, _ in tk.PROJECTS.values():
                        try:
                            child = Path(other).resolve().relative_to(root.resolve()).as_posix()
                        except ValueError:
                            continue
                        if child != ".":
                            scopes.append(f":(top,exclude){child}")
                merged = owner_merge(folder, inv, today, tuple(scopes))
                if merged:
                    dates.append(merged)
            except (OSError, ValueError) as exc:
                issues.append(str(exc))
            for branch, hashes in accepted[project]["approved"].items():
                tip = inv["tips"].get(branch)
                if (tip and branch not in inv["bases"] and branch not in inv["merged"]
                        and (None in hashes or any(tip.startswith(h) for h in hashes))):
                    branches.append({"repo": root.name, "branch": branch, "commit": tip[:12]})
        latest = max(dates, default=None)
        days = (today - latest[0]).days if latest else None
        count = len(branches) if backlog_ok and not issues else None
        projects.append({"key": project, "name": prefixes[0].rstrip("："),
                         "last_delivery": latest[0].isoformat() if latest else None,
                         "delivery_source": latest[1] if latest else None, "days_since_delivery": days,
                         "delivery_state": "known" if latest else "unknown" if issues else "never",
                         "approved_unmerged": count, "branches": branches, "repos": repos, "issues": issues,
                         "age_exceeded": days is not None and days > AGE_LIMIT,
                         "branches_exceeded": count is not None and count > BRANCH_LIMIT,
                         "pause_new_drafts": len(branches) > BRANCH_LIMIT})
    return {"schema_version": 1, "generated_at_utc": now.astimezone(timezone.utc).isoformat(),
            "age_limit_days": AGE_LIMIT, "branch_limit": BRANCH_LIMIT, "projects": projects}


def render(data: dict) -> str:
    lines = ["# 项目交付与待合分支", "", f"核对：{data['generated_at_utc']}", "",
             "天数只认 done 状态的「给 owner MM-DD」或默认分支上 owner 合并；不把执行和审查日期当交付。",
             "待合只认 done 行已验收的本地 claude/ 分支，排除已进入默认分支或任一 rc 的分支。",
             "超过 7 天标交付超线；超过 5 条暂停自发新草样，先叠已有分支交一个可用版本。owner 交办、返工、修复照常。",
             "查不到的数据写未知；无交付凭据写无记录。不得把未知当 0。", "",
             "| 项目 | 距交付（天） | 过审未合（条） | 提醒 |", "|---|---:|---:|---|"]
    for p in data["projects"]:
        days = p["days_since_delivery"] if p["days_since_delivery"] is not None else "无记录" if p["delivery_state"] == "never" else "未知"
        n = p["approved_unmerged"] if p["approved_unmerged"] is not None else "未知"
        flags = (["交付超线 >7天"] if p["age_exceeded"] else []) + (["暂停新草样 >5条"] if p["pause_new_drafts"] else [])
        if p["issues"]:
            flags.append("；".join(p["issues"]))
        lines.append(f"| {p['name']} ({p['key']}) | {days} | {n} | {'；'.join(flags) or '—'} |")
    return "\n".join(lines) + "\n"


def write(tk) -> dict:
    data = collect(tk)
    tk.DATA.mkdir(parents=True, exist_ok=True)
    # Per-process temp name: coordinator and deputy may refresh concurrently. Readers see a complete snapshot.
    import os
    for suffix, content in (("json", json.dumps(data, ensure_ascii=False, indent=2) + "\n"), ("md", render(data))):
        target = tk.DATA / f"project-delivery.{suffix}"
        tmp = target.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)
    return data


if __name__ == "__main__":
    import tick
    print(render(write(tick)))
