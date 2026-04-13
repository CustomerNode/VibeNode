"""
Cross-platform constants and shared utilities.

Extracted to eliminate duplication across multiple modules that each
independently defined the same platform-specific constants and
message-classification helpers.
"""

import re
import subprocess
import sys

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
        ps_script = r'''
Add-Type -AssemblyName System.Windows.Forms
$fb = New-Object System.Windows.Forms.FolderBrowserDialog
$fb.Description = "Select a project folder"
$fb.RootFolder = [System.Environment+SpecialFolder]::MyComputer
$fb.ShowNewFolderButton = $true
$result = $fb.ShowDialog()
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
    Write-Output $fb.SelectedPath
} else {
    Write-Output "::CANCELLED::"
}
'''
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True, text=True, timeout=120,
                creationflags=NO_WINDOW,
            )
            chosen = r.stdout.strip()
            if not chosen or chosen == "::CANCELLED::":
                return (None, "cancelled")
            return (chosen, None)
        except Exception as e:
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
