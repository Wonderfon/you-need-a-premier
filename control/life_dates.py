"""Read the two life date tables and print reminder candidates; never send or write."""

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
import json
import os
from pathlib import Path
import re

# ---- CONFIG ----
DATES_MD = Path(__file__).resolve().parents[1] / "projects" / "life" / "dates.md"
TODAY = os.environ.get("TODAY") or date.today().isoformat()
WINDOW_DAYS = 30
# ----------------

REMINDERS = {"提前 30 天": 30, "提前 7 天": 7}
HEADERS = {
    1: {"项目", "日期", "下次核对", *REMINDERS},
    2: {"步骤", "日期窗口", "下次核对"},
}


def parse_date(value):
    """Only accept a full ISO date at the beginning of a stripped cell."""
    match = re.match(r"^(\d{4}-\d{2}-\d{2})(?!\d)", value.strip())
    if match:
        try:
            return date.fromisoformat(match[1])
        except ValueError:
            pass
    return None


def split_cells(line):
    return [cell.strip().replace(r"\|", "|")
            for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


@dataclass
class Row:
    table: int
    line: int
    cells: dict

    @property
    def label(self):
        name = self.cells["项目" if self.table == 1 else "步骤"]
        return f"[表 {self.table} dates.md:{self.line}] {name}"


def read_rows(path):
    rows, found = [], set()
    table, headers = None, None
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("## "):
            match = re.match(r"^##\s+表\s*([12])(?:\s*[：:]|\s|$)", line)
            table = int(match[1]) if match else None
            headers = None
        if table is None or not line.strip().startswith("|"):
            continue
        cells = split_cells(line)
        if headers is None:
            headers = cells
            if not HEADERS[table].issubset(headers):
                missing = ", ".join(sorted(HEADERS[table] - set(headers)))
                raise ValueError(f"表 {table} dates.md:{line_no} 缺少表头：{missing}")
            found.add(table)
        elif all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        else:
            if len(cells) != len(headers):
                raise ValueError(f"表 {table} dates.md:{line_no} 列数与表头不一致")
            rows.append(Row(table, line_no, dict(zip(headers, cells))))
    if found != {1, 2}:
        raise ValueError("必须同时找到表 1 和表 2")
    return rows


def build_report(path, today, window_days=WINDOW_DAYS):
    end = today + timedelta(days=window_days)
    upcoming, reminders, checks, mismatches = [], [], [], []
    skipped = Counter()
    rows = read_rows(path)
    for row in rows:
        cells = row.cells
        raw_due = cells["日期" if row.table == 1 else "日期窗口"]
        due = parse_date(raw_due)
        if due is None:
            if not raw_due:
                reason = "空白"
            elif re.match(r"^\d{4}-\d{2}(?![\d-])", raw_due):
                reason = "只有月份"
            elif re.match(r"^\d{4}-\d{2}-\d{2}", raw_due):
                reason = "无效日期"
            else:
                reason = "文字窗口"
            skipped[reason] += 1
        else:
            left = (due - today).days
            if today <= due <= end:
                upcoming.append(f"{row.label}｜日期 {due}｜剩 {left} 天")
            for column, days in REMINDERS.items():
                expected = due - timedelta(days=days)
                # Table 2 has no reminder columns: derive them from its deadline.
                reminder = parse_date(cells[column]) if row.table == 1 else expected
                if row.table == 1 and reminder != expected:
                    mismatches.append(
                        f"{row.label}｜{column} 实填 {cells[column] or '空白'}"
                        f"｜应为 {expected}（日期 {due} − {days} 天）")
                if due > today and reminder is not None and reminder <= today:
                    status = ("今天" if reminder == today
                              else f"已过 {(today - reminder).days} 天")
                    reminders.append(f"{row.label}｜{column} {reminder}（{status}）"
                                     f"｜日期 {due}｜剩 {left} 天")
        next_check = parse_date(cells["下次核对"])
        if next_check is not None and next_check <= today:
            status = "今天" if next_check == today else f"已过 {(today - next_check).days} 天"
            checks.append(f"{row.label}｜下次核对 {next_check}（{status}）")

    lines = [f"日期自检：{today}（窗口 {today} 至 {end}，含首尾）",
             f"来源：{path.resolve()}",
             f"共 {len(rows)} 行；日期列跳过 {sum(skipped.values())} 行："
             + "、".join(f"{reason} {skipped[reason]}" for reason in
                        ("空白", "只有月份", "文字窗口", "无效日期")),
             "仅列候选，不发送、不记录是否已提醒；已过提醒日需人工核对是否补发。"]
    for title, items in (("1. 窗口内日期", upcoming),
                         ("2. 30 天／7 天提醒候选", reminders),
                         ("3. 到期的下次核对", checks)):
        lines.extend(["", title + f"（{len(items)} 条）"])
        lines.extend([f"- {item}" for item in items] or ["- 无"])
        if title.startswith("2."):
            lines.append(f"表 1 提醒日校验：{len(mismatches)} 处不一致")
            lines.extend(f"- {item}" for item in mismatches)
    return "\n".join(lines)


@dataclass(frozen=True)
class DailyReminder:
    table: int
    name: str
    reminder_date: date
    due_date: date
    days: int
    line: int
    overdue_by: int = 0

    @property
    def key(self):
        return {"table": self.table, "name": self.name,
                "reminder_date": self.reminder_date.isoformat()}

    def prompt_line(self):
        if self.days == 7:
            text = f"{self.name} {self.due_date} 到期／截止，只剩 7 天了。"
        elif self.overdue_by:
            text = (f"{self.name} {self.due_date} 到期／截止，提前 30 天提醒日已过 {self.overdue_by} 天，"
                    f"现在还剩 {self.days_left} 天。需要的话本周内定一下续、停或交。")
        else:
            text = f"{self.name} {self.due_date} 到期／截止，还有 30 天。需要的话本周内定一下续、停或交。"
        return f"[表 {self.table} dates.md:{self.line}] {text}"

    @property
    def days_left(self):
        return (self.due_date - self.reminder_date).days - self.overdue_by


def reminder_key(entry):
    return (entry["table"], entry["name"], entry["reminder_date"])


def read_reported(path):
    try:
        entries = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    if not isinstance(entries, list):
        raise ValueError("日期提醒记录必须是 JSON 数组")
    try:
        return {reminder_key(entry) for entry in entries}
    except (KeyError, TypeError):
        raise ValueError("日期提醒记录格式无效") from None


def daily_reminders(path, today, reported=()):
    """Return today's 30/7-day reminders and unreported past 30-day reminders for future dates."""
    reported = set(reported)
    reminders = []
    for row in read_rows(path):
        cells = row.cells
        due = parse_date(cells["日期" if row.table == 1 else "日期窗口"])
        if due is None or due <= today:
            continue
        name = cells["项目" if row.table == 1 else "步骤"]
        for days in (30, 7):
            reminder = due - timedelta(days=days)
            overdue_by = (today - reminder).days if reminder < today and days == 30 else 0
            if reminder != today and not overdue_by:
                continue
            key = (row.table, name, reminder.isoformat())
            if key in reported:
                continue
            reminders.append(DailyReminder(row.table, name, reminder, due, days,
                                           row.line, overdue_by))
    return reminders


def prompt_block(dates_path, reported_path, today):
    reminders = daily_reminders(dates_path, today, read_reported(reported_path))
    # The seven-day reminder supersedes the same row's 30-day backfill in the text;
    # retain both keys below so a successful report acknowledges both reminders.
    by_row = {(reminder.table, reminder.line): reminder for reminder in reminders}
    lines = ["【生活线日期表提醒】"]
    lines.extend(f"- {reminder.prompt_line()}" for reminder in by_row.values())
    if not reminders:
        lines.append("- 今天没有需要带入的日期表提醒。")
    lines.append("照 dates.md 第 2 条的两句模板写进日报。")
    return "\n".join(lines), [reminder.key for reminder in reminders]


def record_reported(path, entries):
    if not entries:
        return
    path = Path(path)
    reported = read_reported(path)
    merged = reported | {reminder_key(entry) for entry in entries}
    values = [{"table": table, "name": name, "reminder_date": reminder_date}
              for table, name, reminder_date in sorted(merged)]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(values, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dates-md", type=Path, default=DATES_MD)
    parser.add_argument("--today", type=date.fromisoformat, default=TODAY)
    parser.add_argument("--window-days", type=int, default=WINDOW_DAYS)
    args = parser.parse_args()
    if args.window_days < 0:
        parser.error("--window-days must be non-negative")
    try:
        print(build_report(args.dates_md, args.today, args.window_days))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"日期自检失败：{exc}\n")


if __name__ == "__main__":
    main()
