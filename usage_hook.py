"""Status-line hook for the official CLI: `python usage_hook.py <instance>`. Standard library only.

The CLI starts the hook in whatever folder its session runs in, where `orgcore` is usually not importable, so this file
puts its own folder on the import path and then does what `python -m orgcore usage-capture <instance>` does.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from orgcore.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["usage-capture", *sys.argv[1:]]))
