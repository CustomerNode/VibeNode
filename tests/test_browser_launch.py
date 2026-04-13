"""Source guard: Windows browser launch MUST open Chrome (not the default browser).

TWO hard requirements for browser launch on Windows:

1. MUST open Chrome/Chromium — The Web Speech API (voice input) is Chromium-only.
   Firefox and Safari do not implement it. If the default browser is Firefox, voice
   input silently disappears with no error message. This broke in production when
   os.startfile (default browser) replaced the Chrome-specific launch.

2. MUST be focus-safe — launch.bat starts VibeNode minimized (start /min).
   subprocess.Popen and webbrowser.open spawn child processes that inherit the
   minimized state on Windows 11, creating invisible browser windows.
   ShellExecuteW (via ctypes) and os.startfile go through ShellExecuteEx which
   is focus-safe.  ShellExecuteW lets us target Chrome specifically.

The correct pattern is: find Chrome → ShellExecuteW(chrome, url) → fall back to
os.startfile(url) only if Chrome is not installed.

This test reads run.py source to verify these invariants haven't been broken.
"""

import re
from pathlib import Path

_RUN_PY = Path(__file__).resolve().parent.parent / "run.py"


def _get_windows_browser_block():
    """Extract the Windows-specific browser launch block from run.py."""
    src = _RUN_PY.read_text(encoding="utf-8")
    lines = src.splitlines()

    # Find open_browser(), then the if sys.platform == "win32" block within it
    in_open_browser = False
    block_start = None
    for i, line in enumerate(lines):
        if 'def open_browser' in line:
            in_open_browser = True
        if in_open_browser and 'sys.platform' in line and 'win32' in line:
            block_start = i
            break

    # Walk forward to find the elif (next platform block)
    block_end = block_start + 1
    for i in range(block_start + 1, min(len(lines), block_start + 40)):
        if lines[i].strip().startswith('elif') and 'platform' in lines[i]:
            block_end = i
            break

    # Find os.startfile within the block for backwards compat
    startfile_idx = block_start
    for i in range(block_start, block_end):
        if "os.startfile(url)" in lines[i]:
            startfile_idx = i
            break

    return "\n".join(lines[block_start:block_end]), startfile_idx


class TestBrowserLaunchSourceGuard:

    def test_find_chrome_function_exists(self):
        """run.py must have a _find_chrome() function.

        This function locates Chrome on the system so we can open it
        specifically rather than relying on the default browser.
        Removing it breaks voice input when the default browser is Firefox.
        """
        src = _RUN_PY.read_text(encoding="utf-8")
        assert "def _find_chrome" in src, \
            "run.py must define _find_chrome() — Chrome-specific launch " \
            "is required because Web Speech API (voice) is Chromium-only. " \
            "DO NOT replace with os.startfile(url) alone."

    def test_windows_block_uses_shellexecutew(self):
        """Windows browser launch must use ShellExecuteW to open Chrome.

        ShellExecuteW is the only method that is both:
        - Focus-safe (works when parent is minimized via launch.bat start /min)
        - Targetable (can specify Chrome instead of default browser)
        """
        win_block, _ = _get_windows_browser_block()
        assert "ShellExecuteW" in win_block, \
            "Windows browser launch must use ShellExecuteW to open Chrome — " \
            "it is focus-safe AND allows targeting Chrome specifically. " \
            "DO NOT replace with os.startfile (opens default browser, " \
            "breaks voice if default is Firefox)."

    def test_windows_block_calls_find_chrome(self):
        """Windows block must call _find_chrome() before launching."""
        win_block, _ = _get_windows_browser_block()
        assert "_find_chrome" in win_block, \
            "Windows browser launch must call _find_chrome() to locate " \
            "Chrome before launching. Voice input requires Chromium."

    def test_windows_has_startfile_fallback(self):
        """os.startfile must exist as a FALLBACK (not primary) launch method.

        Only used when Chrome is not installed on the system.
        """
        win_block, _ = _get_windows_browser_block()
        assert "os.startfile" in win_block, \
            "Windows browser launch must have os.startfile as fallback " \
            "for systems where Chrome is not installed."

    def test_windows_block_has_no_popen(self):
        """The Windows browser block must not use subprocess.Popen.

        subprocess.Popen spawns a child process that inherits the parent's
        minimized state on Windows 11. The browser window is created but
        never comes to the foreground.
        """
        win_block, _ = _get_windows_browser_block()
        assert "subprocess.Popen" not in win_block, \
            "Windows browser launch must NOT use subprocess.Popen — " \
            "minimized parent windows (launch.bat start /min) prevent " \
            "child processes from stealing focus on Windows 11. " \
            "Use ShellExecuteW instead."

    def test_windows_block_has_no_webbrowser(self):
        """The Windows browser block must not use webbrowser.open.

        webbrowser.open() on Windows internally uses subprocess.Popen
        with the default browser — same minimized-parent focus problem,
        AND it opens the default browser which may not be Chrome.
        """
        win_block, _ = _get_windows_browser_block()
        assert "webbrowser.open" not in win_block, \
            "Windows browser launch must NOT use webbrowser.open() — " \
            "it uses subprocess.Popen internally (focus problem) and " \
            "opens the default browser (may not be Chrome, breaks voice)."

    def test_windows_block_startfile_not_primary(self):
        """os.startfile must NOT be the primary/only launch method.

        os.startfile opens the default browser. If the default is Firefox,
        Web Speech API (voice) is unavailable. Chrome must be tried first.
        This is the exact regression that broke voice input in production.
        """
        win_block, _ = _get_windows_browser_block()
        # ShellExecuteW (Chrome) must appear BEFORE os.startfile (fallback)
        shell_pos = win_block.find("ShellExecuteW")
        startfile_pos = win_block.find("os.startfile")
        assert shell_pos != -1 and startfile_pos != -1, \
            "Windows block must have both ShellExecuteW and os.startfile"
        assert shell_pos < startfile_pos, \
            "ShellExecuteW (Chrome) must come BEFORE os.startfile (fallback). " \
            "os.startfile opens the default browser which may be Firefox. " \
            "Chrome must be the primary launch target."

    def test_launch_bat_starts_minimized(self):
        """Verify launch.bat uses start /min — the root cause of the focus issue.

        If launch.bat ever stops starting minimized, the Popen restriction
        could theoretically be relaxed — but ShellExecuteW is still better
        because it also lets us target Chrome specifically.
        """
        bat = Path(__file__).resolve().parent.parent / "launch.bat"
        if bat.exists():
            src = bat.read_text(encoding="utf-8")
            assert "start /min" in src, \
                "launch.bat uses start /min — this is why browser launch " \
                "must use ShellExecuteW (child processes can't steal focus)"
