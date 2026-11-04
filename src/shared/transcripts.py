"""Knowledge about transcript content that both layers apply.

Two different questions are asked of a user-role text, and they were answered
by two functions that shared one name (``_is_system_content`` in
``src/web/sessions.py`` and in ``src/engine/session_manager.py``) while doing
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
# them through its backend (``src/engine/backends/claude_layout.py``), which
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
    # Text a Stop hook sent back (Smart termination).  Checked with
    # ``sent_back_entry`` FIRST, which draws it as a card; listed here too so
    # every other reader treats it as injected, never as the user's words.
    "Stop hook feedback:",
)


# ── Smart termination send-backs ────────────────────────────────────────────
# When a Stop hook blocks the end of a turn, the CLI injects its reason as a
# user-role message starting with this prefix.  Every timeline parser turns it
# into a ``sent_back`` entry, which the chat draws as a card (live-panel.js),
# instead of a chat bubble that looks like the user typed it.

STOP_HOOK_FEEDBACK_PREFIX = "Stop hook feedback:"


def _quoted(text: str, limit: int = 4) -> list:
    import re
    out = []
    for q in re.findall(r'"([^"\n]{1,60})"', text):
        if q not in out:
            out.append(q)
    return out[:limit]


def _and_list(items: list) -> str:
    q = ['"%s"' % i for i in items]
    return q[0] if len(q) == 1 else ", ".join(q[:-1]) + " and " + q[-1]


def sent_back_entry(text: str):
    """The timeline entry for a Stop hook send-back, or None if *text* is not
    one: ``{"kind": "sent_back", "name": <reason chip>, "desc": <one plain
    sentence>, "text": <what the session was told>}``."""
    t = (text or "").strip()
    if not t.startswith(STOP_HOOK_FEEDBACK_PREFIX):
        return None
    import re
    reason = t[len(STOP_HOOK_FEEDBACK_PREFIX):].strip()
    body = reason
    if body.lower().startswith("smart termination:"):
        body = body[len("smart termination:"):].strip()
        # What the session was told, without the instruction text every
        # send-back carries after it.
        told = body
        for cut in ("\n\n# Ending a turn", "\n\nEnd your final message with this block"):
            if cut in told:
                told = told.split(cut, 1)[0]
        if told.lower().startswith("your handoff card was sent back."):
            told = told[len("your handoff card was sent back."):]
        told = told.strip()
        reason = told[:1].upper() + told[1:]
    low = body.lower()
    first_para = body.split("\n\n", 1)[0]
    group = re.search(r'not allowed in a "(\w+)" card, ([^:]+):([^\n]*)', body, re.I)
    if "text outside the" in low and "handoff" in low:
        chip = "Text outside the response"
        desc = "It wrote text around its termination response. Everything goes inside it."
    elif "end every turn with one" in low:
        chip = "No response"
        desc = "It ended without a termination response."
    elif group:
        chip = group.group(2).strip()[:1].upper() + group.group(2).strip()[1:]
        words = re.findall(r'"([^"\n]{1,60})" \(', group.group(3))[:4]
        desc = ("It wrote " + _and_list(words) + ' in a "' + group.group(1).replace("_", " ")
                + '" response.') if words else "Its response broke a rule: " + group.group(2).strip() + "."
    elif "labels the user may not know" in low:
        words = re.findall(r'"([^"\n]{1,60})" \(', body)[:4]
        chip = "Unexplained labels"
        desc = ("It used " + _and_list(words) + " without saying what they are.") if words else \
               "It used labels from earlier in the session without saying what they are."
    elif "against what actually happened" in low:
        chip = "Claim not backed"
        desc = "Its response claimed something this session did not actually do."
    elif "format:" in low:
        chip = "Incomplete response"
        desc = "Its response was missing required parts."
    elif "think again" in low or "unfinished work" in low:
        words = _quoted(first_para)
        chip = "Unfinished work"
        desc = ("It said " + _and_list(words) + ". That is work it can do itself, "
                "so it was sent back to finish it.") if words else \
               "It tried to stop with work left that it can do itself, so it was sent back to finish it."
    elif "labels" in low:
        words = _quoted(first_para)
        chip = "Unexplained labels"
        desc = ("It used " + _and_list(words) + " without saying what they are.") if words else \
               "It used labels from earlier in the session without saying what they are."
    elif "nothing is needed" in low or "says nothing" in low:
        chip = "Missing handoff"
        desc = ("It said nothing was needed from you, without saying why, what "
                "happens next, or when you will hear back.")
    elif "handoff" in low:
        chip = "Missing handoff"
        desc = ("Its last message did not say what it is working on, where it "
                "stands, and what it needs from you.")
    else:
        chip = "Sent back"
        desc = first_para.split("\n", 1)[0][:200]
    return {"kind": "sent_back", "name": chip, "desc": desc, "text": reason[:4000]}


# ── Automated session messages ──────────────────────────────────────────────
# Messages that reach a session as user-role text although the owner never
# typed them: a background job's completion notice, the watchdog's nudges, the
# automatic "Continue" after an error or a limit, a wake-up the session
# scheduled for itself (the CLI marks that one ``isMeta`` in the transcript).
# Drawn as the user's own messages, they would also be read by Smart
# termination as requests, so a session's own reminder became "the task".  The
# chat draws
# them under an "Automated session message" banner (live-panel.js
# ``_automatedSource``, the browser twin of this function) and the checker
# skips them (``src/engine/handoff_evidence.py``).

def _continue_prompts() -> tuple:
    import os
    custom = (os.environ.get("VIBENODE_API_RETRY_CONTINUE_PROMPT") or "").strip()
    out = ["continue from where you left off"]
    if custom:
        out.append(custom.lower().rstrip(". "))
    return tuple(out)


def automated_source(text: str, is_meta: bool = False) -> str:
    """Who sent this user-role message when it was not the owner, as a short
    plain label ("Background job finished", "VibeNode watchdog", ...), or ''
    when it reads as the owner's own message.  ``is_meta`` is the transcript's
    ``isMeta`` flag (a wake-up the session scheduled for itself)."""
    t = (text or "").lstrip()
    if t.startswith("<task-notification>"):
        return "Background job finished"
    if t.startswith("[VibeNode watchdog]"):
        return "VibeNode watchdog"
    if t.startswith("[VibeNode"):
        return "VibeNode"
    if t.strip().lower().rstrip(". ") in _continue_prompts():
        return "Automatic continue"
    if t.startswith("Your response above was cut off"):
        return "Automatic continue"
    if t.startswith("Your response above was stopped by a safety classifier"):
        return "Safety notice"
    if t.startswith(("Another Claude session sent a message", "<cross-session-message")):
        return "Message from another session"
    if t.startswith("Base directory for this skill"):
        return "Skill loaded"
    # The CLI also flags its note on a picture the session looked at
    # ("[Image: original WxH, …]") and its own injected context; those are
    # already drawn as a grey pill or a system line, so they get no banner.
    if is_meta and not t.startswith(("[Image", "<", STOP_HOOK_FEEDBACK_PREFIX)):
        return "Scheduled check-in"
    return ""


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
        t.startswith(STOP_HOOK_FEEDBACK_PREFIX) or
        t.startswith("This session is being continued") or
        t.startswith("This is a continuation") or
        t.startswith("The user opened") or
        t.startswith("The user selected") or
        t.startswith("The user is viewing") or
        t.startswith("**What we were working on") or
        t.startswith("**Key context") or
        t.startswith("**Most recent exchanges")
    )
