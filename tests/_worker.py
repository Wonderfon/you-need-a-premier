"""Helper run as a separate process by the concurrency tests: python tests/_worker.py <mode> <target> <arg>."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orgcore import backlog, config, scheduler, usage, workflow  # noqa: E402
from orgcore.fsio import file_lock  # noqa: E402

mode, target, arg = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
if mode == "claim":  # claim until nothing is todo, print each claimed text
    while (task := backlog.claim(target)) is not None:
        print(task.text, flush=True)
elif mode == "add":  # append 25 rows tagged with this worker's id
    for number in range(25):
        backlog.add_task(target, "research", "demo-notes", f"{arg}-{number}")
elif mode == "hold":  # take the lock, announce it, and sleep until killed
    with file_lock(target):
        print("held", flush=True)
        time.sleep(60)
elif mode == "dispatch":  # target = instance dir; dispatch until refused, print "<actor> <text>" per row, then the code
    instance = config.load_instance(target)
    while True:
        result = scheduler.dispatch(instance, usage.FakeUsage.healthy(instance.policy, usage.now_utc()), arg)
        if isinstance(result, scheduler.Refusal):
            print("REFUSED", result.code, flush=True)
            break
        print(arg, result.task.text, flush=True)
elif mode == "holdclaim":  # target = instance dir, arg = JSON key; hold the claim lock like a live executor
    instance = config.load_instance(target)
    with workflow.claim_lock(instance.root, tuple(json.loads(arg))):
        print("held", flush=True)
        time.sleep(60)
