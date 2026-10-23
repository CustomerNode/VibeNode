"""Knowledge about transcript content that both layers apply.

Two different questions are asked of a user-role text, and they were answered
by two functions that shared one name (``_is_system_content`` in
``app/sessions.py`` and in ``daemon/session_manager.py``) while doing
different things.  Both now live here under names that say which question
they answer.  They are deliberately NOT merged: either one's verdict changes
what renders as a user bubble, so unifying them would change behaviour in
both layers.

* :func:`is_system_content`: the live stream (engine).  Does the text carry
  one of the CLI's injected-content markers ANYWHERE?  The engine turns such
  a "user" message into a labelled system entry.
* :func:`is_timeline_system_content`: the session timeline (web).  Does the
  text START like injected context (a tag, a continuation summary, an IDE
  "user opened/selected" note, a compaction summary heading)?  The timeline
  skips such rows.
"""

# Markers the Claude CLI puts into user-role messages it injects itself
# (continuation summaries, local-command output, system reminders,
# interrupts).  These must NOT render as user chat bubbles.  The engine reads
# them through its backend (``daemon/backends/claude_layout.py``), which
# returns this tuple.
CLI_SYSTEM_MARKERS = (
    "This session is being continued from a previous conversation",
    "<system-reminder>",
    "<local-command-stdout>",
    "<local-command-caveat>",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "[Request interrupted by user]",
)


def is_system_content(text: str, markers=CLI_SYSTEM_MARKERS) -> bool:
    """True if ``text`` contains any CLI system-content marker (live stream)."""
    for marker in markers:
        if marker in text:
            return True
    return False


def system_content_label(text: str) -> str:
    """A short human-readable label for system content (live stream)."""
    import re
    if "[Request interrupted by user]" in text:
        return "Session interrupted by user"
    if "This session is being continued from a previous conversation" in text:
        return "Session continued from previous conversation"
    if "<command-name>" in text:
        # Extract command name: <command-name>/compact</command-name>
        m = re.search(r'<command-name>(/?\w+)</command-name>', text)
        cmd = m.group(1) if m else "command"
        # Extract stdout if present
        m2 = re.search(r'<local-command-stdout>(.*?)</local-command-stdout>', text, re.DOTALL)
        stdout = m2.group(1).strip() if m2 else ""
        if stdout:
            return f"{cmd}: {stdout[:100]}"
        return f"Local command: {cmd}"
    if "<local-command-stdout>" in text:
        m = re.search(r'<local-command-stdout>(.*?)</local-command-stdout>', text, re.DOTALL)
        return f"Command output: {(m.group(1).strip()[:100]) if m else '...'}"
    return "System message"


def is_timeline_system_content(text: str) -> bool:
    """True if ``text`` starts like injected context (session timeline)."""
    t = text.strip()
    return bool(
        t.startswith("<") and not t.startswith("<!") or
        t.startswith("This session is being continued") or
        t.startswith("This is a continuation") or
        t.startswith("The user opened") or
        t.startswith("The user selected") or
        t.startswith("The user is viewing") or
        t.startswith("**What we were working on") or
        t.startswith("**Key context") or
        t.startswith("**Most recent exchanges")
    )
