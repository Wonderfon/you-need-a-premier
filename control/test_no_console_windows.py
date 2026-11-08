"""Every subprocess reachable through tick's control imports must hide its window."""
import ast
from pathlib import Path
import tempfile
import unittest


CONTROL = Path(__file__).resolve().parent
PROCESS_CALLS = {"run", "Popen", "check_output", "check_call", "call"}


def control_modules(control=CONTROL):
    """Read the import closure without importing tick or running any service code."""
    modules = {}
    for path in control.rglob("*.py"):
        relative = path.relative_to(control)
        if any(p == "archive" or p == "tests" or p.startswith("test_") for p in relative.parts):
            continue
        parts = relative.with_suffix("").parts
        name = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
        modules[name] = path
    pending, found = ["tick"], {}
    while pending:
        name = pending.pop()
        path = modules[name]
        if path in found:
            continue
        tree = found[path] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imports = []
            if isinstance(node, ast.Import):
                imports = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                package = path.parent.relative_to(control).parts
                prefix = package[:len(package) - node.level + 1] if node.level else ()
                base = ".".join((*prefix, node.module or "")).strip(".")
                imports = [base, *(f"{base}.{a.name}".strip(".") for a in node.names)]
            for imported in imports:
                parts = imported.removeprefix("control.").split(".")
                for length in range(1, len(parts) + 1):
                    dependency = ".".join(parts[:length])
                    if dependency in modules and modules[dependency] not in found:
                        pending.append(dependency)
    return found


def process_calls(tree):
    aliases, functions = {"subprocess"}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update(a.asname or a.name for a in node.names if a.name == "subprocess")
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            functions.update(a.asname or a.name for a in node.names if a.name in PROCESS_CALLS)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                and func.value.id in aliases and func.attr in PROCESS_CALLS
                or isinstance(func, ast.Name) and func.id in functions):
            yield node


class NoConsoleWindows(unittest.TestCase):
    def test_tick_imports_hide_every_subprocess_window(self):
        modules = control_modules()
        self.assertIn(CONTROL / "review_packet.py", modules)
        self.assertIn(CONTROL / "records_index.py", modules)  # indirect import
        for path, tree in sorted(modules.items()):
            for call in process_calls(tree):
                with self.subTest(module=path.relative_to(CONTROL).as_posix(), line=call.lineno):
                    self.assertIn("creationflags", [k.arg for k in call.keywords],
                                  f"control/{path.relative_to(CONTROL).as_posix()}:{call.lineno} "
                                  "subprocess call needs creationflags=CREATE_NO_WINDOW")

    def test_import_closure_handles_cycles_and_excludes_tests_archive_and_external_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = {"tick.py": "import direct as d\nimport test_skip\nimport archive.skip\nimport outside\n",
                       "direct.py": "from control import indirect\n",
                       "indirect.py": "from . import tick\n",
                       "test_skip.py": "invalid python", "archive/skip.py": "invalid python",
                       "unreachable.py": "invalid python"}
            for name, source in sources.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(source, encoding="utf-8")
            self.assertEqual({p.name for p in control_modules(root)}, {"tick.py", "direct.py", "indirect.py"})

    def test_process_calls_include_all_five_apis_and_import_aliases(self):
        source = "import subprocess as sp\nfrom subprocess import run as execute\n"
        source += "\n".join(f"subprocess.{api}([])" for api in sorted(PROCESS_CALLS))
        source += "\nsp.Popen([])\nexecute([])\nother.run([])\n"
        calls = list(process_calls(ast.parse(source)))
        self.assertEqual(len(calls), 7)
        self.assertTrue(all(not call.keywords for call in calls))


if __name__ == "__main__":
    unittest.main()
