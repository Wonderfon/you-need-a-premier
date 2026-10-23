"""Cross-process file lock and atomic write. No stale-lock heuristics: the OS drops the lock when the holder dies."""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

LOCK_TIMEOUT_SECONDS = 15.0


def _try_lock(fd: int) -> bool:
    try:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    if fcntl:
        fcntl.flock(fd, fcntl.LOCK_UN)
    else:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


@contextmanager
def file_lock(target: Path, timeout: float = LOCK_TIMEOUT_SECONDS):
    """Exclusive lock for `target`, held on the sidecar file `<target>.lock` (never deleted, so waiters never race it)."""
    lock_path = target.with_name(target.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT)
    deadline = time.monotonic() + timeout
    try:
        while not _try_lock(fd):
            if time.monotonic() > deadline:
                raise TimeoutError(f"lock busy: {lock_path}")
            time.sleep(0.02)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes) -> None:
    """Write to a temp file in the same directory, fsync, then replace. A crash leaves the old or the new file, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(40):  # Windows: a reader may hold the target open for a moment
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 39:
                    raise
                time.sleep(0.05)
    finally:
        if tmp.exists():
            tmp.unlink()
    if os.name == "posix":
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
