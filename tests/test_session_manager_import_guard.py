"""Regression: `import session_manager` must be a no-op.

Added 2026-09-26 (Step 4 Gate 4B) after an ARM64 candidate-environment
import scan accidentally launched a full web server. Before the fix,
importing the module ran the top-level ``runpy.run_path("run.py")``
call, which bound a port, spawned children, and wrote a spawn line to
``logs/_server.log`` — none of which should happen from an ``import``.

The fix moved every side effect into ``_startup()`` and guarded it
behind ``if __name__ == "__main__":``. These tests lock that in.

Why we test this by parsing the source rather than actually importing:
importing session_manager from pytest would either (a) demonstrate the
regression by actually starting a server on port 5050 (destroys the
user's dev environment) or (b) succeed silently after the fix, at which
point the test proves nothing about the guard being in place. Reading
the source is the deterministic check.
"""

import ast
from pathlib import Path

import pytest


_SESSION_MANAGER = (
    Path(__file__).resolve().parent.parent / "session_manager.py"
)


@pytest.fixture(scope="module")
def module_source() -> str:
    return _SESSION_MANAGER.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def module_tree(module_source: str) -> ast.Module:
    return ast.parse(module_source)


class TestImportIsNoOp:
    """Any side effect at module scope re-introduces the port-9999 bug."""

    def test_has_main_guard(self, module_tree: ast.Module) -> None:
        """A top-level `if __name__ == "__main__":` block must exist."""
        found = False
        for node in module_tree.body:
            if not isinstance(node, ast.If):
                continue
            t = node.test
            # `__name__ == "__main__"` in either operand order
            if (
                isinstance(t, ast.Compare)
                and len(t.ops) == 1
                and isinstance(t.ops[0], ast.Eq)
            ):
                left, right = t.left, t.comparators[0]
                names = {
                    getattr(left, "id", None),
                    getattr(right, "id", None),
                }
                strings = {
                    getattr(left, "value", None) if isinstance(left, ast.Constant) else None,
                    getattr(right, "value", None) if isinstance(right, ast.Constant) else None,
                }
                if "__name__" in names and "__main__" in strings:
                    found = True
                    break
        assert found, (
            "session_manager.py must gate its side effects with "
            "`if __name__ == \"__main__\":`. Without it, importing the "
            "module (e.g. from tooling or a dependency scan) will call "
            "`runpy.run_path('run.py')` at import time and launch the full "
            "Flask + SocketIO server, binding a port and spawning children. "
            "This is exactly the Step 3 (2026-09-25) ARM64 import-scan bug."
        )

    def test_no_top_level_runpy(self, module_tree: ast.Module) -> None:
        """`runpy.run_path(...)` at module scope re-launches the server on import."""
        for node in module_tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                func = node.value.func
                # runpy.run_path(...)
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "run_path"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "runpy"
                ):
                    pytest.fail(
                        "runpy.run_path(...) found at module scope. It must "
                        "live inside _startup() (called from the __main__ "
                        "guard), otherwise `import session_manager` starts "
                        "the whole web server."
                    )

    def test_no_top_level_reviver_hook_call(self, module_tree: ast.Module) -> None:
        """`_reviver_hook()` at module scope spawns child processes on import."""
        for node in module_tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                func = node.value.func
                if isinstance(func, ast.Name) and func.id == "_reviver_hook":
                    pytest.fail(
                        "_reviver_hook() found at module scope. It spawns a "
                        "reviver subprocess and issues a POST to port 5052 — "
                        "must be called only from _startup()."
                    )

    def test_no_top_level_launch_splash_call(self, module_tree: ast.Module) -> None:
        """`_launch_splash()` at module scope pops a Tk window on import."""
        for node in module_tree.body:
            # Look for both bare `_launch_splash()` and `if not _launch_splash():`
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                func = node.value.func
                if isinstance(func, ast.Name) and func.id == "_launch_splash":
                    pytest.fail(
                        "_launch_splash() found at module scope. It spawns a "
                        "boot-splash Tk window subprocess — must be called "
                        "only from _startup()."
                    )
            if isinstance(node, ast.If):
                # `if not _launch_splash():`
                t = node.test
                if isinstance(t, ast.UnaryOp) and isinstance(t.op, ast.Not):
                    op = t.operand
                    if (
                        isinstance(op, ast.Call)
                        and isinstance(op.func, ast.Name)
                        and op.func.id == "_launch_splash"
                    ):
                        pytest.fail(
                            "`if not _launch_splash():` found at module "
                            "scope. Splash launch must live in _startup()."
                        )

    def test_no_top_level_chdir(self, module_tree: ast.Module) -> None:
        """`os.chdir(...)` at module scope mutates the caller's working dir on import."""
        for node in module_tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                func = node.value.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "chdir"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "os"
                ):
                    pytest.fail(
                        "os.chdir(...) found at module scope. Importing the "
                        "module would move the caller's cwd — must live in "
                        "_startup()."
                    )


class TestStartupContractPreserved:
    """The __main__ guard must call _startup(), and _startup() must run
    the full launch sequence in the pre-refactor order. Missing any of
    these breaks a launch surface — launch.bat, launch.sh, or the
    scheduled task via reviver.py."""

    def test_startup_function_exists(self, module_tree: ast.Module) -> None:
        names = {n.name for n in module_tree.body if isinstance(n, ast.FunctionDef)}
        assert "_startup" in names, (
            "_startup() must be defined at module scope so the __main__ "
            "guard can invoke it."
        )

    def test_main_guard_calls_startup(self, module_tree: ast.Module) -> None:
        for node in module_tree.body:
            if not isinstance(node, ast.If):
                continue
            t = node.test
            if not (
                isinstance(t, ast.Compare)
                and any(
                    isinstance(x, ast.Constant) and x.value == "__main__"
                    for x in (t.left, *t.comparators)
                )
            ):
                continue
            # Body of the guard must call _startup()
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.Expr)
                    and isinstance(stmt.value, ast.Call)
                    and isinstance(stmt.value.func, ast.Name)
                    and stmt.value.func.id == "_startup"
                ):
                    return
            pytest.fail(
                "The `if __name__ == \"__main__\":` block must call "
                "_startup(). Nothing else runs the launch sequence."
            )
        pytest.fail(
            "No `if __name__ == \"__main__\":` block found — see "
            "TestImportIsNoOp.test_has_main_guard."
        )

    def test_startup_runs_full_launch_sequence(self, module_source: str) -> None:
        """_startup()'s body must still contain every step of the launch
        sequence — chdir, spawn probe, splash, reviver hook, runpy — so
        launch.bat / launch.sh / reviver behavior is byte-equivalent."""
        # Isolate _startup body: from `def _startup` to next top-level `\n\n\n`
        # or `\nif __name__`.
        start = module_source.find("def _startup")
        assert start != -1, "_startup() must be defined"
        end = module_source.find('\nif __name__', start)
        if end == -1:
            end = len(module_source)
        body = module_source[start:end]

        # Each required call must appear inside _startup().
        for needle, why in [
            ("os.chdir(_HERE)", "chdir keeps relative paths working"),
            ("_log_spawn_probe()", "CLAUDE.md invariant: spawn probe line"),
            ("_launch_splash()", "user sees boot progress"),
            ("_show_notification(", "fallback when splash unavailable"),
            ("_reviver_hook()", "Mobile Command hand-off"),
            ("runpy.run_path", "handoff to run.py's __main__"),
        ]:
            assert needle in body, (
                f"_startup() is missing `{needle}` — {why}. Removing this "
                "changes launcher behavior in production."
            )

    def test_launch_bat_still_invokes_as_script(self) -> None:
        """launch.bat MUST invoke session_manager.py as a script — otherwise
        the guard fires with __name__ != '__main__' and nothing runs."""
        lb = (_SESSION_MANAGER.parent / "launch.bat").read_text(
            encoding="utf-8", errors="replace"
        )
        assert "session_manager.py" in lb, (
            "launch.bat must invoke `session_manager.py` (as a script). If "
            "some future refactor changes launch.bat to `-m session_manager` "
            "or similar, this test needs updating; today, the only supported "
            "invocation is `pythonw session_manager.py`."
        )
