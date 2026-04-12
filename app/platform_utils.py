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
