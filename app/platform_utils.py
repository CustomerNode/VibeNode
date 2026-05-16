"""
Cross-platform constants and shared utilities.

Extracted to eliminate duplication across multiple modules that each
independently defined the same platform-specific constants and
message-classification helpers.
"""

import logging
import re
import subprocess
import sys

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Windows process creation flag
# ---------------------------------------------------------------------------
# subprocess.CREATE_NO_WINDOW prevents console windows from flashing on
# Windows.  On other platforms the flag doesn't exist and isn't needed.

NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# ---------------------------------------------------------------------------
# System-user message classification
# ---------------------------------------------------------------------------
# Markers that indicate a UserMessage is SDK/CLI system content, not human
# input.  Used by both live_api.py and ws_events.py when rendering session
# logs so that injected system messages get a distinct visual treatment.

SYSTEM_USER_MARKERS = (
    "This session is being continued from a previous conversation",
    "<system-reminder>",
    "<local-command-stdout>",
    "<local-command-caveat>",
    "<command-name>",
    "<command-message>",
    "<command-args>",
)


from pathlib import Path


# ---------------------------------------------------------------------------
# Native folder picker (cross-platform)
# ---------------------------------------------------------------------------

def native_folder_picker() -> tuple:
    """Open a native folder-picker dialog and return (path, error).

    Returns:
        (chosen_path, None)   on success
        (None, "cancelled")   if the user cancelled
        (None, error_string)  on failure / unsupported platform
    """
    if sys.platform == "win32":
        # Windows folder-picker history of pain:
        #   - FolderBrowserDialog.ShowDialog() with no owner opens BEHIND
        #     Chrome on most machines because the PowerShell child has no
        #     foreground window of its own.
        #   - A TopMost owner form isn't enough: Windows refuses
        #     SetForegroundWindow from a process that doesn't own the
        #     current foreground (security feature, since Windows 2000).
        # Canonical fix: AttachThreadInput to the foreground thread, then
        # BringWindowToTop + SetForegroundWindow, then detach. This is the
        # documented workaround in MS knowledge base Q97925 and is the only
        # reliable way to surface a dialog from a background subprocess.
        ps_script = r'''
$ErrorActionPreference = 'Stop'
try {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class VNFG {
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, IntPtr pid);
    [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool f);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int n);
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
    public static void Force(IntPtr hWnd) {
        uint fg = GetWindowThreadProcessId(GetForegroundWindow(), IntPtr.Zero);
        uint me = GetCurrentThreadId();
        if (fg != 0 && fg != me) {
            AttachThreadInput(me, fg, true);
            try { ShowWindow(hWnd, 5); BringWindowToTop(hWnd); SetForegroundWindow(hWnd); }
            finally { AttachThreadInput(me, fg, false); }
        } else {
            ShowWindow(hWnd, 5); BringWindowToTop(hWnd); SetForegroundWindow(hWnd);
        }
    }
}
"@
    $owner = New-Object System.Windows.Forms.Form
    $owner.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
    $owner.StartPosition = [System.Windows.Forms.FormStartPosition]::Manual
    $owner.Location = New-Object System.Drawing.Point -10000, -10000
    $owner.Size = New-Object System.Drawing.Size 1, 1
    $owner.ShowInTaskbar = $false
    $owner.TopMost = $true
    $owner.Opacity = 0
    $owner.Show()
    [VNFG]::Force($owner.Handle)
    try {
        $fb = New-Object System.Windows.Forms.FolderBrowserDialog
        $fb.Description = "Select a project folder"
        $fb.RootFolder = [System.Environment+SpecialFolder]::MyComputer
        $fb.ShowNewFolderButton = $true
        $result = $fb.ShowDialog($owner)
    } finally {
        $owner.Close()
        $owner.Dispose()
    }
    if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
        Write-Output ("OK::" + $fb.SelectedPath)
    } else {
        Write-Output "CANCELLED::"
    }
} catch {
    Write-Output ("ERROR::" + $_.Exception.Message)
}
'''
        try:
            _log.info("native_folder_picker: spawning PowerShell picker")
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True, text=True, timeout=120,
                creationflags=NO_WINDOW,
            )
            stdout = (r.stdout or "").strip()
            stderr = (r.stderr or "").strip()
            _log.info(
                "native_folder_picker: rc=%s stdout=%r stderr=%r",
                r.returncode, stdout[:500], stderr[:500],
            )
            # Parse tagged output to distinguish silent failures from cancels.
            if stdout.startswith("OK::"):
                return (stdout[4:], None)
            if stdout.startswith("CANCELLED::") or stdout == "":
                return (None, "cancelled")
            if stdout.startswith("ERROR::"):
                return (None, "Picker error: " + stdout[7:])
            if stderr:
                return (None, "PowerShell stderr: " + stderr[:300])
            return (None, "Unexpected picker output: " + stdout[:300])
        except subprocess.TimeoutExpired:
            _log.error("native_folder_picker: PowerShell timed out after 120s")
            return (None, "Folder picker timed out — the dialog may be hidden behind another window")
        except Exception as e:
            _log.exception("native_folder_picker: subprocess failed")
            return (None, str(e))

    elif sys.platform == "darwin":
        try:
            r = subprocess.run(
                ["osascript", "-e", "POSIX path of (choose folder)"],
                capture_output=True, text=True, timeout=120,
            )
            chosen = r.stdout.strip()
            if r.returncode != 0 or not chosen:
                return (None, "cancelled")
            return (chosen.rstrip("/"), None)
        except Exception as e:
            return (None, str(e))

    else:
        # Linux: try zenity, then kdialog
        for cmd in [
            ["zenity", "--file-selection", "--directory"],
            ["kdialog", "--getexistingdirectory", str(Path.home())],
        ]:
            try:
                r = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=120,
                )
                chosen = r.stdout.strip()
                if r.returncode != 0 or not chosen:
                    return (None, "cancelled")
                return (chosen, None)
            except FileNotFoundError:
                continue
            except Exception as e:
                return (None, str(e))
        return (None, "No folder picker available — install zenity or kdialog, or use the Path tab")


# ---------------------------------------------------------------------------
# Default project scan roots (platform-aware)
# ---------------------------------------------------------------------------

def default_project_roots() -> list:
    """Return a list of Path objects for directories to scan for projects.

    Only includes directories that actually exist on disk.
    """
    home = Path.home()
    candidates = [
        home / "Documents",
        home / "Desktop",
    ]
    if sys.platform == "win32":
        candidates.append(home / "source" / "repos")  # Visual Studio default
    elif sys.platform == "darwin":
        candidates.append(home / "Developer")
    else:
        # Linux common project directories
        for name in ("projects", "src", "code", "dev", "repos"):
            candidates.append(home / name)
    return [p for p in candidates if p.is_dir()]


def is_system_user_content(text: str) -> bool:
    """Return True if *text* contains any system-user marker."""
    for marker in SYSTEM_USER_MARKERS:
        if marker in text:
            return True
    return False


def system_user_label(text: str) -> str:
    """Return a short human-readable label for a system-user message."""
    if "This session is being continued from a previous conversation" in text:
        return "Session continued from previous conversation"
    m = re.search(r'<command-name>(/?\w+)</command-name>', text)
    if m:
        cmd = m.group(1)
        m2 = re.search(r'<local-command-stdout>(.*?)</local-command-stdout>', text, re.DOTALL)
        stdout = m2.group(1).strip() if m2 else ""
        return f"{cmd}: {stdout[:100]}" if stdout else f"Local command: {cmd}"
    m = re.search(r'<local-command-stdout>(.*?)</local-command-stdout>', text, re.DOTALL)
    if m:
        return f"Command output: {m.group(1).strip()[:100]}"
    return "System message"
