"""Helper run as a separate process by the concurrency tests: python tests/_worker.py <mode> <backlog> <arg>."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orgcore import backlog  # noqa: E402
from orgcore.fsio import file_lock  # noqa: E402

mode, path, arg = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
if mode == "claim":  # claim until nothing is todo, print each claimed text
    while (task := backlog.claim(path)) is not None:
        print(task.text, flush=True)
elif mode == "add":  # append 25 rows tagged with this worker's id
    for number in range(25):
        backlog.add_task(path, "research", "demo-notes", f"{arg}-{number}")
elif mode == "hold":  # take the lock, announce it, and sleep until killed
    with file_lock(path):
        print("held", flush=True)
        time.sleep(60)
