"""Keep input tests importable as incore.input in the stack trial."""

import ast
import unittest
from pathlib import Path


class TestImportStyle(unittest.TestCase):
    def test_no_absolute_input_package_imports(self):
        test_dir = Path(__file__).resolve().parent
        violations = []
        for path in sorted(test_dir.glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    if any(alias.name.split(".", 1)[0] == "input" for alias in node.names):
                        violations.append(f"{path.name}:{node.lineno}")
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    if node.module and node.module.split(".", 1)[0] == "input":
                        violations.append(f"{path.name}:{node.lineno}")
        self.assertEqual([], violations, "Use relative imports inside input tests: " + ", ".join(violations))


if __name__ == "__main__":
    unittest.main()
