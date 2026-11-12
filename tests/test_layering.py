"""The Session Engine never imports the web package, and ``src/shared/`` imports
neither layer.

Why: the engine (``src/engine/``) and the web server (``src/web/``) are separate
processes that restart independently, and the engine is restarted rarely
(it ends every running session).  The engine once imported
``app.*`` in a dozen places (config, kanban DB, live_api's model list, the
web's transcript parser, the singleton), so an engine process carried a
second, private copy of web state (caches, the models list) that nothing kept
in sync, and a web-side refactor could break the engine at its next restart.
Code both need lives in ``src/shared/``.

The scan reads source, so a lazy ``from app.x import y`` inside a function
counts just as much as a top-level import.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _imports(path: Path):
    """``(lineno, module)`` for every import statement in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue  # relative: stays inside its own package
            yield node.lineno, node.module or ""


def _top(module: str) -> str:
    return module.split(".", 1)[0]


def _py_files(package: str):
    files = sorted((ROOT / package).rglob("*.py"))
    assert files, f"no python files under {package}/"
    return [f for f in files if "__pycache__" not in f.parts]


def test_engine_never_imports_the_web_package():
    bad = [f"{p.relative_to(ROOT).as_posix()}:{line} imports {mod}"
           for p in _py_files("src/engine") for line, mod in _imports(p)
           if _top(mod) == "web"]
    assert not bad, "src/engine/ must not import web (move shared code to src/shared/):\n  " + "\n  ".join(bad)


@pytest.mark.parametrize("forbidden", ["web", "engine"])
def test_common_imports_neither_layer(forbidden):
    bad = [f"{p.relative_to(ROOT).as_posix()}:{line} imports {mod}"
           for p in _py_files("src/shared") for line, mod in _imports(p)
           if _top(mod) == forbidden]
    assert not bad, f"src/shared/ must not import {forbidden}/:\n  " + "\n  ".join(bad)


def test_the_scan_sees_lazy_imports(tmp_path):
    """Guard the guard: an import inside a function body is found."""
    f = tmp_path / "x.py"
    f.write_text("def f():\n    from web.config import x\n", encoding="utf-8")
    assert ("web.config" in {m for _, m in _imports(f)})
