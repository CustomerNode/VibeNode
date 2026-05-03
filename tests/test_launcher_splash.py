"""Source guards for the boot-splash tkinter pre-flight in session_manager.py.

session_manager.py runs ``_launch_splash()`` at import time, which forks a
subprocess running ``app/boot_splash.py``. boot_splash.py begins with a
``try: import tkinter`` block that silently ``sys.exit(0)`` on ImportError —
which is invisible to the parent. On Debian/Ubuntu, tkinter is shipped as
a separate ``python3-tk`` package and is missing by default, so the splash
just never appears with no diagnostic.

The fix (shipped 2026-05-03) is twofold:

1. Pre-flight ``import tkinter`` *in the parent* before forking the splash
   subprocess. If tkinter is missing here, return False and let the
   notify-send fallback fire.
2. When the fallback fires on Linux specifically, surface the apt-hint
   ("install python3-tk") in the toast so the user knows what to do.

These tests assert both branches still exist — they are easy to delete
during a refactor and the user-visible breakage is silent.

We can't import session_manager.py directly because it has top-level side
effects (chdir, PATH augmentation, splash launch, runpy of the app). So
these are read-the-source guards, same pattern as test_browser_launch.py.
"""

from pathlib import Path


_SESSION_MANAGER = (
    Path(__file__).resolve().parent.parent / "session_manager.py"
)


def _launch_splash_body() -> str:
    src = _SESSION_MANAGER.read_text(encoding="utf-8")
    start = src.find("def _launch_splash")
    assert start != -1, "session_manager.py must define _launch_splash()"
    end = src.find("\ndef ", start + 1)
    if end == -1:
        # Could be the last def in the file — fall through to top-level code
        end = src.find("\n# Try the splash", start + 1)
    return src[start:end]


def _post_launch_block() -> str:
    """Return the top-level "if not _launch_splash():" block — that's where
    the notify-send fallback message gets built."""
    src = _SESSION_MANAGER.read_text(encoding="utf-8")
    start = src.find("if not _launch_splash():")
    assert start != -1, \
        "session_manager.py must call _launch_splash() and fall back to " \
        "_show_notification on failure."
    # Take everything until the next top-level statement (runpy / import)
    end = src.find("\nimport runpy", start)
    if end == -1:
        end = len(src)
    return src[start:end]


class TestSplashTkinterPreflight:
    """The parent process MUST verify tkinter is importable before forking
    the splash subprocess — otherwise the subprocess silently sys.exit(0)
    and the user never sees the splash with no log line, no toast, nothing."""

    def test_launch_splash_imports_tkinter_as_preflight(self):
        body = _launch_splash_body()
        assert "import tkinter" in body, \
            "_launch_splash() must `import tkinter` as a pre-flight check. " \
            "Without it, when python3-tk is missing on Debian/Ubuntu, the " \
            "splash subprocess sys.exit(0)'s silently and the user sees " \
            "nothing during the multi-second app startup."

    def test_launch_splash_returns_false_on_import_error(self):
        """The pre-flight must catch ImportError and return False so the
        caller falls through to the notify-send fallback."""
        body = _launch_splash_body()
        # Find the import tkinter line, then check the surrounding except
        idx = body.find("import tkinter")
        assert idx != -1
        nearby = body[idx:idx + 400]
        assert "ImportError" in nearby, \
            "Pre-flight import tkinter must be wrapped in try/except " \
            "ImportError so missing python3-tk doesn't crash the launcher."
        assert "return False" in nearby, \
            "On ImportError the function must `return False` so the " \
            "caller's `if not _launch_splash():` branch fires the toast."


class TestSplashFallbackToast:
    """When the splash fails on Linux specifically AND tkinter is the
    missing piece, the notify-send toast must include the apt hint so the
    user knows it's a one-package fix, not a broken install."""

    def test_fallback_block_checks_linux_platform(self):
        block = _post_launch_block()
        assert 'sys.platform == "linux"' in block, \
            "Fallback toast must branch on Linux specifically — Windows " \
            "and macOS bundle tkinter, so the apt hint would be wrong " \
            "advice on those platforms."

    def test_fallback_block_mentions_python3_tk(self):
        """The user-visible string must name the actual package."""
        block = _post_launch_block()
        assert "python3-tk" in block, \
            'The Linux fallback toast must mention "python3-tk" so the ' \
            'user knows the apt package name. Without it the toast just ' \
            'says "Starting up..." with no actionable info.'

    def test_fallback_block_imports_tkinter_to_decide_message(self):
        """The hint should only appear when tkinter is the actual cause —
        not when (e.g.) boot_splash.py is missing or some other failure."""
        block = _post_launch_block()
        assert "import tkinter" in block, \
            "Fallback should test for tkinter availability before adding " \
            "the apt hint, otherwise users on Linux with tkinter installed " \
            "but a different splash failure get misleading advice."
