"""Read-only machine and backup summary for the twice-daily report prompts."""
from __future__ import annotations

import ctypes
import http.client
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
import instance_config as ic  # noqa: E402  (this machine's paths and owner identity; defaults = today's values)

MACHINE_HEALTH = Path(ic.V["workspace"] + r"\incore\data\machine-health.jsonl")
BACKUPS = Path(ic.V["workspace"] + r"\incore\data\backups")
GPU_QUERY = "temperature.gpu,memory.used,memory.total,utilization.gpu"
GB = 1024 ** 3
MIB = 1024 ** 2


def parse_gpu(output: str) -> dict | None:
    rows = []
    for line in output.splitlines():
        try:
            temp, used, total, load = (float(part.strip()) for part in line.split(","))
        except (ValueError, TypeError):
            continue
        rows.append((temp, used, total, load))
    if not rows:
        return None
    return {"temperature": max(row[0] for row in rows),
            "memory_used": sum(row[1] for row in rows),
            "memory_total": sum(row[2] for row in rows),
            "utilization": max(row[3] for row in rows)}


def gpu_reading() -> dict | None:
    try:
        result = subprocess.run(["nvidia-smi", f"--query-gpu={GPU_QUERY}", "--format=csv,noheader,nounits"],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_gpu(result.stdout) if result.returncode == 0 else None


class _MemoryStatus(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def system_readings() -> dict:
    values = {}
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        memory = _MemoryStatus()
        memory.dwLength = ctypes.sizeof(memory)
        get_memory_status = kernel.GlobalMemoryStatusEx
        get_memory_status.argtypes = [ctypes.POINTER(_MemoryStatus)]
        get_memory_status.restype = ctypes.c_int
        if get_memory_status(ctypes.byref(memory)):
            values["memory_free"] = memory.ullAvailPhys
    except (AttributeError, OSError):
        pass
    try:
        values["c_free"] = shutil.disk_usage("C:\\").free
    except OSError:
        pass
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        get_tick_count = kernel.GetTickCount64
        get_tick_count.restype = ctypes.c_ulonglong
        values["uptime"] = get_tick_count() // 1000
    except (AttributeError, OSError):
        pass
    return values


def input_status() -> int | None:
    connection = http.client.HTTPConnection("127.0.0.1", 8765, timeout=3)
    try:
        connection.request("GET", "/")
        return connection.getresponse().status
    except OSError:
        return None
    finally:
        connection.close()


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _package_date(name: str) -> datetime.date | None:
    match = re.match(r"^(\d{4}-\d{2}-\d{2})-", name)
    if not match:
        return None
    try:
        return datetime.strptime(match[1], "%Y-%m-%d").date()
    except ValueError:
        return None


def backup_readings(root: Path, now: datetime) -> dict:
    status = _json(root / "status.json")
    records = [status.get("last_staged_package") or {}]
    try:
        chain_paths = sorted(root.glob("zip-chain-*.json"))
    except OSError:
        chain_paths = []
    for path in chain_paths:
        record = _json(path)
        if record:
            records.append(record)

    metadata_dates, archive_dates, sizes = set(), set(), {}
    archive_dirs = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        package = record.get("package_name")
        if package:
            day = _package_date(str(package))
            if day:
                metadata_dates.add(day)
                if isinstance(record.get("package_bytes"), int):
                    sizes[day] = record["package_bytes"]
        prior = record.get("prior_package_name")
        if prior and (day := _package_date(str(prior))):
            metadata_dates.add(day)
        if record.get("successful_archive_cutoff_date"):
            try:
                metadata_dates.add(datetime.strptime(record["successful_archive_cutoff_date"], "%Y-%m-%d").date())
            except (TypeError, ValueError):
                pass
        package_path = record.get("package_path")
        if package_path:
            directory = Path(str(package_path)).parent
            if directory.is_absolute():
                archive_dirs.add(directory)

    scan_ok = False
    archive_files = {}
    for directory in archive_dirs:
        try:
            for path in directory.glob("*.zip"):
                day = _package_date(path.name)
                if day:
                    archive_dates.add(day)
                    archive_files[day] = path
            scan_ok = True
        except OSError:
            pass

    days = archive_dates if scan_ok else metadata_dates
    latest = max(days, default=None)
    size = sizes.get(latest)
    if size is None and latest in archive_files:
        try:
            size = archive_files[latest].stat().st_size
        except OSError:
            pass

    hour = status.get("daily_archive_hour", 20)
    try:
        hour = int(hour)
    except (TypeError, ValueError):
        hour = 20
    expected_latest = now.date() if now.hour >= hour else now.date() - timedelta(days=1)
    expected = {expected_latest - timedelta(days=offset) for offset in range(7)}
    missing = sorted(expected - days) if scan_ok else None

    ok_at = (status.get("arbitros") or {}).get("ok_at")
    age_hours = None
    if ok_at:
        try:
            saved = datetime.fromisoformat(str(ok_at).replace("Z", "+00:00"))
            current = now if now.tzinfo else now.astimezone()
            saved = saved.astimezone(current.tzinfo) if saved.tzinfo else saved.astimezone()
            age_hours = max(0, int((current - saved).total_seconds() // 3600))
        except (TypeError, ValueError, OverflowError):
            pass
    return {"latest": latest, "size": size, "missing": missing, "age_hours": age_hours,
            "stage": status.get("stage")}


def _gb(value: int) -> str:
    return f"{value / GB:.1f}GB"


def _duration(seconds: int) -> str:
    days, hours = divmod(max(0, seconds) // 3600, 24)
    return f"{days}天{hours}小时" if days else f"{hours}小时"


def _size(value: int | None) -> str:
    if value is None:
        return "大小未知"
    return f"{value / GB:.1f}GB" if value >= GB else f"{value / MIB:.0f}MB"


def _watch_part(now: datetime) -> tuple[str, bool]:
    """Last 12 hours of the 10-minute sampler (control/machine_watch.py), same data as the dashboard row."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import machine_watch
        text = machine_watch.report_part(MACHINE_HEALTH, now.astimezone())
        return text, "持续异常：" in text or "没有机器取样" in text
    except Exception as exc:  # a failed read must not hide the rest of the line
        return f"机器取样摘要读不到（{type(exc).__name__}）", True


def report_line(now: datetime | None = None, root: Path = BACKUPS) -> str:
    now = now or datetime.now()
    gpu, system, backup, status = gpu_reading(), system_readings(), backup_readings(root, now), input_status()
    issues, facts = [], []

    if gpu is None:
        issues.append("GPU读数缺失")
    else:
        used = gpu["memory_used"] / 1024
        facts.append(f"GPU {gpu['temperature']:.0f}°C/{used:.1f}GB/{gpu['utilization']:.0f}%")
        if gpu["memory_used"] > 7 * 1024:
            issues.append(f"GPU显存当前 {used:.1f}GB（超7GB）")

    memory_free = system.get("memory_free")
    if memory_free is None:
        issues.append("可用内存读不到")
    else:
        facts.append(f"内存余 {_gb(memory_free)}")
        if memory_free < 8 * GB:
            issues.append(f"可用内存 {_gb(memory_free)}")

    c_free = system.get("c_free")
    if c_free is None:
        issues.append("C盘空间读不到")
    else:
        facts.append(f"C盘余 {_gb(c_free)}")
        if c_free < 100 * GB:
            issues.append(f"C盘剩 {_gb(c_free)}")

    uptime = system.get("uptime")
    if uptime is None:
        issues.append("开机时长读不到")
    else:
        facts.append(f"开机 {_duration(uptime)}")
        if uptime > 14 * 24 * 3600:
            issues.append(f"开机 {_duration(uptime)}")

    if backup["latest"] is None:
        issues.append("日包记录读不到")
    else:
        facts.append(f"日包 {backup['latest']:%m-%d}/{_size(backup['size'])}")
        if backup["size"] is None:
            issues.append("日包大小读不到")
    if backup["stage"] in ("backup_failed", "staging_error"):
        issues.append("本机备份失败")
    elif backup["stage"] not in ("local_snapshot_verified", "staged_for_drive_sync"):
        issues.append("备份状态读不到")
    if backup["missing"] is None:
        issues.append("近7天日包无法核全")
    elif backup["missing"]:
        issues.append("日包缺 " + "/".join(day.strftime("%m-%d") for day in backup["missing"]))
    else:
        facts.append("近7天日包齐")

    if backup["age_hours"] is None:
        issues.append("Arbitros备份时间读不到")
    else:
        facts.append(f"Arbitros {backup['age_hours']}h")
        if backup["age_hours"] > 36:
            issues.append(f"Arbitros备份 {backup['age_hours']}h未成功")

    if status == 200:
        facts.append("8765=200")
    else:
        issues.append("输入台8765不通" if status is None else f"输入台8765返回{status}")

    watch, watch_bad = _watch_part(now)
    (issues if watch_bad else facts).append(watch)
    health = "正常" if not issues else "异常：" + "；".join(issues)
    return f"机器和备份：{health}（" + "；".join(facts) + "）"
