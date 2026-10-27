import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from orgcore import config

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "examples" / "demo"
WORKER = Path(__file__).with_name("_worker.py")


def run_worker(mode, target, arg=""):
    return subprocess.Popen([sys.executable, str(WORKER), mode, str(target), arg], stdout=subprocess.PIPE, text=True,
                            cwd=ROOT)


def run_cli(*args):
    return subprocess.run([sys.executable, "-m", "orgcore", *map(str, args)], capture_output=True, text=True, cwd=ROOT)


def board(*lines_per_section):
    """Build board text: board(("research", 60, ["- [todo] demo-notes: a"]), ("build", 40, [...]))."""
    out = ["# Backlog", ""]
    for name, weight, rows in lines_per_section:
        out += [f"## Line: {name}", f"Weight: {weight}%", *rows, ""]
    return "\n".join(out)


class InstanceCase(unittest.TestCase):
    """A fresh copy of the demo instance in a temp directory; edit it with `edit_policy` and `write_board`."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orgcore-inst-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "inst"
        shutil.copytree(DEMO, self.root)

    def edit_policy(self, **changes):
        path = self.root / "policy.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(changes)
        path.write_text(json.dumps(data), encoding="utf-8")

    def edit_org(self, change):
        path = self.root / "org.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        change(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    def write_board(self, text):
        (self.root / "projects" / "backlog.md").write_text(text, encoding="utf-8")

    def instance(self):
        return config.load_instance(self.root)
