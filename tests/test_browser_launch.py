"""Source guard: browser launch must use os.startfile on Windows.

Windows 11 prevents child processes of minimized windows from stealing focus.
Since launch.bat starts VibeNode minimized (start /min), using subprocess.Popen
to launch Chrome results in an invisible browser window. os.startfile goes
through the Windows shell URL handler which properly activates the window.

webbrowser.open() is also unsafe — on Windows it internally uses subprocess.Popen
with the default browser, which inherits the minimized parent's focus restrictions.

This test reads run.py source to verify the pattern hasn't been reverted.
"""

import re
from pathlib import Path

_RUN_PY = Path(__file__).resolve().parent.parent / "run.py"


def _get_windows_browser_block():
    """Extract the Windows-specific browser launch block from run.py."""
    src = _RUN_PY.read_text(encoding="utf-8")
    lines = src.splitlines()

    # Find os.startfile(url) — the correct Windows launch method
    startfile_idx = None
    for i, line in enumerate(lines):
        if "os.startfile(url)" in line:
            startfile_idx = i
            break

    # Find the enclosing if sys.platform == "win32" block
    # Walk backwards from os.startfile to find the block start
    block_start = startfile_idx
    for i in range(startfile_idx, max(0, startfile_idx - 20), -1):
        if 'sys.platform' in lines[i] and 'win32' in lines[i]:
            block_start = i
            break

    # Walk forward to find the elif (next platform block)
    block_end = startfile_idx
    for i in range(startfile_idx + 1, min(len(lines), startfile_idx + 20)):
        if lines[i].strip().startswith('elif') and 'platform' in lines[i]:
            block_end = i
            break

    return "\n".join(lines[block_start:block_end]), startfile_idx


class TestBrowserLaunchSourceGuard:

    def test_windows_uses_os_startfile(self):
        """Windows browser launch must use os.startfile, not subprocess."""
        src = _RUN_PY.read_text(encoding="utf-8")
        assert re.search(r"os\.startfile\(url\)", src), \
            "run.py must use os.startfile(url) for Windows browser launch"

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
            "Use os.startfile(url) instead."

    def test_windows_block_has_no_webbrowser(self):
        """The Windows browser block must not use webbrowser.open.

        webbrowser.open() on Windows internally uses subprocess.Popen
        with the default browser — same minimized-parent focus problem.
        os.startfile goes through ShellExecuteEx which is focus-safe.
        """
        win_block, _ = _get_windows_browser_block()
        assert "webbrowser.open" not in win_block, \
            "Windows browser launch must NOT use webbrowser.open() — " \
            "it internally uses subprocess.Popen on Windows, which has " \
            "the same minimized-parent focus problem. " \
            "Use os.startfile(url) instead."

    def test_launch_bat_starts_minimized(self):
        """Verify launch.bat uses start /min — the root cause of the issue.

        If launch.bat ever stops starting minimized, the Popen restriction
        could theoretically be relaxed. But os.startfile is still better,
        so keep this test as documentation of WHY.
        """
        bat = Path(__file__).resolve().parent.parent / "launch.bat"
        if bat.exists():
            src = bat.read_text(encoding="utf-8")
            assert "start /min" in src, \
                "launch.bat uses start /min — this is why browser launch " \
                "must use os.startfile (child processes can't steal focus)"
