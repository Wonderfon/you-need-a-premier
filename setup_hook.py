"""Hooks for the setup session. `python setup_hook.py <instance>` saves the answer files the session wrote to
state/inbox/ and hands the result back to the model; `python setup_hook.py <instance> guard` allows only such writes.
Standard library only; works from any folder like usage_hook.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from orgcore.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["setup", sys.argv[1], "guard"] if sys.argv[2:] == ["guard"] else ["setup", sys.argv[1], "ingest", "--hook"]))