"""Source guards: browser launch on ALL platforms MUST open Chrome first.

The Web Speech API (voice input) is Chromium-only.  Firefox and Safari do not
implement it.  If the default browser is Firefox, voice input silently
disappears with no error message.  All three platforms use a Chrome-finder +
fallback pattern:

  Windows: _find_chrome()       → ShellExecuteW(chrome, url) → os.startfile
  Linux:   _find_chrome_linux() → Popen([chrome, url])       → xdg-open
  macOS:   _find_chrome_macos() → Popen([chrome, url])       → open

Windows additionally requires focus-safety: launch.bat uses start /min, and
subprocess.Popen spawns minimised children that never come to the foreground.
ShellExecuteW is focus-safe and Chrome-specific.

These tests read run.py source to verify none of these invariants have been
accidentally broken.
"""

import re
from pathlib import Path

_RUN_PY = Path(__file__).resolve().parent.parent / "run.py"


def _get_open_browser_lines():
    """Return (lines, first_line_index) for the open_browser() function body."""
    src = _RUN_PY.read_text(encoding="utf-8")
    lines = src.splitlines()
    start = None
    for i, line in enumerate(lines):
        if 'def open_browser' in line:
            start = i
            break
    assert start is not None, "open_browser() not found in run.py"
    # Collect until next top-level def/class or EOF
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i] and not lines[i][0].isspace() and (
            lines[i].startswith('def ') or lines[i].startswith('class ')
        ):
            end = i
            break
    return lines[start:end], start


def _get_platform_block(platform_value: str):
    """Extract the if/elif block for a given platform string inside open_browser().

    Returns the block as a single string.
    platform_value: 'win32', 'darwin', or 'linux'
    """
    lines, _ = _get_open_browser_lines()

    block_start = None
    for i, line in enumerate(lines):
        if 'sys.platform' in line and platform_value in line:
            block_start = i
            break

    assert block_start is not None, \
        f"No sys.platform == {platform_value!r} block found in open_browser()"

    # Walk forward to find the next elif/else/if at the same indent level,
    # or end of function.
    indent = len(lines[block_start]) - len(lines[block_start].lstrip())
    block_end = len(lines)
    for i in range(block_start + 1, len(lines)):
        stripped = lines[i].lstrip()
        if not stripped:
            continue
        curr_indent = len(lines[i]) - len(stripped)
        if curr_indent <= indent and (
            stripped.startswith('elif') or stripped.startswith('else') or
            stripped.startswith('if ') or stripped.startswith('return') or
            stripped.startswith('opened')
        ):
            block_end = i
            break

    return "\n".join(lines[block_start:block_end])


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


# ---------------------------------------------------------------------------
# Linux source guards
# ---------------------------------------------------------------------------

class TestLinuxBrowserLaunch:
    """Verify the Linux browser launch block is Chrome-first with xdg-open fallback.

    The Web Speech API (voice input) is Chromium-only.  xdg-open alone opens
    whatever the user's default browser is; if it is Firefox, voice silently
    breaks.  _find_chrome_linux() must be called first so Chrome/Chromium is
    the primary target.
    """

    def test_find_chrome_linux_function_exists(self):
        """run.py must define _find_chrome_linux()."""
        src = _RUN_PY.read_text(encoding="utf-8")
        assert "def _find_chrome_linux" in src, \
            "run.py must define _find_chrome_linux() — Linux Chrome-first launch " \
            "requires a dedicated finder. Removing it falls back to xdg-open " \
            "which opens Firefox if that is the system default, breaking voice."

    def test_linux_block_calls_find_chrome_linux(self):
        """Linux browser block must call _find_chrome_linux()."""
        block = _get_platform_block("linux")
        assert "_find_chrome_linux" in block, \
            "Linux open_browser() block must call _find_chrome_linux() to locate " \
            "Chrome before launching. Voice input requires Chromium."

    def test_linux_block_has_xdg_open_fallback(self):
        """xdg-open must exist as the FALLBACK (not primary) on Linux."""
        block = _get_platform_block("linux")
        assert "xdg-open" in block, \
            "Linux browser launch must have xdg-open as a fallback for systems " \
            "where Chrome/Chromium is not installed."

    def test_linux_chrome_before_xdg_open(self):
        """Chrome finder must appear BEFORE xdg-open in the Linux block."""
        block = _get_platform_block("linux")
        chrome_pos = block.find("_find_chrome_linux")
        # Search for the actual Popen call (with quotes), not comment references
        xdg_pos = block.find('"xdg-open"')
        assert chrome_pos != -1 and xdg_pos != -1, \
            "Linux block must contain both _find_chrome_linux and xdg-open"
        assert chrome_pos < xdg_pos, \
            "_find_chrome_linux() (Chrome, primary) must come BEFORE " \
            "xdg-open (fallback). xdg-open opens the default browser which " \
            "may be Firefox. Chrome must be the primary launch target."

    def test_find_chrome_linux_checks_snap_path(self):
        """_find_chrome_linux must check /snap/bin/google-chrome.

        Ubuntu 22.04+ ships Google Chrome as a snap package.  The snap
        binary is at /snap/bin/google-chrome and is NOT on the regular PATH
        unless /snap/bin is in PATH.  Without this check, Chrome installed
        via snap is invisible to the finder.
        """
        src = _RUN_PY.read_text(encoding="utf-8")
        # Extract just the _find_chrome_linux function body
        start = src.find("def _find_chrome_linux")
        end = src.find("\ndef ", start + 1)
        func_body = src[start:end]
        assert "/snap/bin/google-chrome" in func_body, \
            "_find_chrome_linux() must check /snap/bin/google-chrome — " \
            "Ubuntu installs Chrome as a snap and the binary lives there."


# ---------------------------------------------------------------------------
# macOS source guards
# ---------------------------------------------------------------------------

class TestMacosBrowserLaunch:
    """Verify the macOS browser launch block is Chrome-first with 'open' fallback.

    The Web Speech API (voice input) is Chromium-only.  The macOS system
    'open' command launches the user's default browser; if it is Firefox,
    voice silently breaks.  _find_chrome_macos() must be called first.
    """

    def test_find_chrome_macos_function_exists(self):
        """run.py must define _find_chrome_macos()."""
        src = _RUN_PY.read_text(encoding="utf-8")
        assert "def _find_chrome_macos" in src, \
            "run.py must define _find_chrome_macos() — macOS Chrome-first launch " \
            "requires a dedicated finder. Removing it falls back to 'open url' " \
            "which opens Firefox if that is the system default, breaking voice."

    def test_macos_block_calls_find_chrome_macos(self):
        """macOS browser block must call _find_chrome_macos()."""
        block = _get_platform_block("darwin")
        assert "_find_chrome_macos" in block, \
            "macOS open_browser() block must call _find_chrome_macos() to locate " \
            "Chrome before launching. Voice input requires Chromium."

    def test_macos_block_has_open_fallback(self):
        """The macOS system 'open' command must exist as the FALLBACK."""
        block = _get_platform_block("darwin")
        assert '"open"' in block or "'open'" in block, \
            "macOS browser launch must fall back to the system 'open' command " \
            "for systems where Chrome/Chromium is not installed."

    def test_macos_chrome_before_open_fallback(self):
        """Chrome finder must appear BEFORE 'open' fallback in the macOS block."""
        block = _get_platform_block("darwin")
        chrome_pos = block.find("_find_chrome_macos")
        open_pos = block.find('"open"')
        if open_pos == -1:
            open_pos = block.find("'open'")
        assert chrome_pos != -1 and open_pos != -1, \
            "macOS block must contain both _find_chrome_macos and the 'open' fallback"
        assert chrome_pos < open_pos, \
            "_find_chrome_macos() (Chrome, primary) must come BEFORE " \
            "'open' (fallback). 'open' launches the default browser which " \
            "may be Firefox. Chrome must be the primary launch target."

    def test_find_chrome_macos_checks_applications_bundle(self):
        """_find_chrome_macos must check the standard .app bundle path."""
        src = _RUN_PY.read_text(encoding="utf-8")
        start = src.find("def _find_chrome_macos")
        end = src.find("\ndef ", start + 1)
        func_body = src[start:end]
        assert "Google Chrome.app" in func_body, \
            "_find_chrome_macos() must check the standard macOS app bundle path " \
            "'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'."


# ---------------------------------------------------------------------------
# Daemon detachment source guard
# ---------------------------------------------------------------------------

class TestDaemonDetachment:
    """Verify the daemon is properly detached from the terminal on Linux/macOS.

    Without start_new_session=True, closing the launch terminal sends SIGHUP
    to the daemon's process group, killing it and terminating every active
    Claude session.  Windows uses CREATE_NEW_PROCESS_GROUP; Linux/macOS must
    use start_new_session=True (which calls setsid() in the child process).
    """

    def test_daemon_popen_uses_start_new_session_on_non_windows(self):
        """ensure_daemon() must set start_new_session=True on non-Windows."""
        src = _RUN_PY.read_text(encoding="utf-8")
        # Locate ensure_daemon()
        start = src.find("def ensure_daemon")
        end = src.find("\ndef ", start + 1)
        func_body = src[start:end]
        assert "start_new_session" in func_body, \
            "ensure_daemon() must set start_new_session=True for Linux/macOS. " \
            "Without it, closing the launch terminal sends SIGHUP to the " \
            "daemon's process group, killing all active Claude sessions."

    def test_daemon_start_new_session_in_else_branch(self):
        """start_new_session must be in the non-Windows (else) branch."""
        src = _RUN_PY.read_text(encoding="utf-8")
        start = src.find("def ensure_daemon")
        end = src.find("\ndef ", start + 1)
        func_body = src[start:end]
        # Locate the else: that follows the win32 creationflags check
        else_pos = func_body.find("else:")
        assert else_pos != -1, \
            "ensure_daemon() must have an else branch for non-Windows daemon spawn"
        # Look for the actual assignment (not comment references to it)
        sn_code = 'popen_kwargs["start_new_session"]'
        sn_pos = func_body.find(sn_code)
        assert sn_pos != -1, \
            "ensure_daemon() must assign popen_kwargs[\"start_new_session\"] = True"
        assert sn_pos > else_pos, \
            "start_new_session=True must appear in the else (non-Windows) branch, " \
            "not in the Windows block. Windows uses CREATE_NEW_PROCESS_GROUP instead."

    def test_daemon_windows_uses_create_new_process_group(self):
        """Windows daemon spawn must still use CREATE_NEW_PROCESS_GROUP."""
        src = _RUN_PY.read_text(encoding="utf-8")
        start = src.find("def ensure_daemon")
        end = src.find("\ndef ", start + 1)
        func_body = src[start:end]
        assert "CREATE_NEW_PROCESS_GROUP" in func_body, \
            "ensure_daemon() must use CREATE_NEW_PROCESS_GROUP on Windows so " \
            "the daemon survives the web server process dying."


# ---------------------------------------------------------------------------
# Chrome process detachment (Linux + macOS)
# ---------------------------------------------------------------------------
#
# The browser process MUST be detached from session_manager.py's process
# group so a daemon restart doesn't take Chrome with it, and stdio MUST be
# routed to /dev/null so Chrome's chatty DBus/GPU debug output doesn't
# scroll over the launch.sh terminal. Both fixes shipped 2026-05-03 after
# users on Linux saw their browser tab die when the daemon recycled.
#
# Windows uses ShellExecuteW which is already detached by definition
# (different mechanism, same effect), so it doesn't need these guards.

class TestChromeDetachment:
    """Source guards: Chrome launch on Linux/macOS uses start_new_session +
    DEVNULL stdio so the browser survives daemon restarts cleanly."""

    def test_linux_chrome_uses_start_new_session(self):
        """Linux Chrome Popen must pass start_new_session=True so the
        browser detaches from session_manager.py's process group. Without
        it, a daemon restart cascades a SIGTERM to the Chrome child."""
        block = _get_platform_block("linux")
        # Heuristic: locate the Popen([chrome_path, ...]) call (NOT the
        # xdg-open Popen) and confirm start_new_session=True is in the
        # same call.
        chrome_call_idx = block.find("Popen(\n")
        if chrome_call_idx == -1:
            chrome_call_idx = block.find("Popen([chrome_path")
        assert chrome_call_idx != -1, \
            "Linux block must Popen the chrome_path"
        # Take the next 400 chars from the Popen — should fit the kwargs
        nearby = block[chrome_call_idx:chrome_call_idx + 400]
        assert "start_new_session=True" in nearby, \
            "Linux Chrome Popen() must pass start_new_session=True so the " \
            "browser survives a daemon restart. Without it, Chrome inherits " \
            "the parent process group and dies when the parent recycles."

    def test_linux_chrome_redirects_stdio_to_devnull(self):
        """Chrome's debug output (DBus, GPU warnings) clutters the
        launch.sh terminal. stdout/stderr=DEVNULL keeps the user's
        startup messages readable."""
        block = _get_platform_block("linux")
        chrome_call_idx = block.find("Popen(\n")
        if chrome_call_idx == -1:
            chrome_call_idx = block.find("Popen([chrome_path")
        nearby = block[chrome_call_idx:chrome_call_idx + 400]
        assert "stdout=subprocess.DEVNULL" in nearby, \
            "Linux Chrome Popen() must redirect stdout to DEVNULL — Chrome " \
            "is chatty and otherwise scrolls over our launcher's messages."
        assert "stderr=subprocess.DEVNULL" in nearby, \
            "Linux Chrome Popen() must redirect stderr to DEVNULL too — " \
            "stderr is where Chrome's DBus/GPU warnings actually go."

    def test_macos_chrome_uses_start_new_session(self):
        """Same detachment rationale as Linux: macOS Chrome must outlive
        the parent's process group churn."""
        block = _get_platform_block("darwin")
        chrome_call_idx = block.find("Popen(\n")
        if chrome_call_idx == -1:
            chrome_call_idx = block.find("Popen([chrome_path")
        assert chrome_call_idx != -1, \
            "macOS block must Popen the chrome_path"
        nearby = block[chrome_call_idx:chrome_call_idx + 400]
        assert "start_new_session=True" in nearby, \
            "macOS Chrome Popen() must pass start_new_session=True. " \
            "Same reasoning as Linux — without it the browser dies when " \
            "the parent process recycles."

    def test_macos_chrome_redirects_stdio_to_devnull(self):
        """macOS Chrome stdio also goes to DEVNULL for terminal cleanliness."""
        block = _get_platform_block("darwin")
        chrome_call_idx = block.find("Popen(\n")
        if chrome_call_idx == -1:
            chrome_call_idx = block.find("Popen([chrome_path")
        nearby = block[chrome_call_idx:chrome_call_idx + 400]
        assert "stdout=subprocess.DEVNULL" in nearby
        assert "stderr=subprocess.DEVNULL" in nearby
