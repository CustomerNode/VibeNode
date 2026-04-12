"""
Session title generation — uses Claude Haiku for concise, high-quality names
with a fast heuristic fallback if the API is unavailable.

Title generation chain:
  1. Direct Anthropic API  (fastest, needs ANTHROPIC_API_KEY)
  2. Claude CLI             (uses CLI auth/OAuth, no key needed)
  3. Heuristic              (instant, no API needed)
"""

import re
import logging

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TRIVIAL = {
    "yes","no","ok","okay","sure","thanks","thank","good","great","cool","done",
    "right","fine","got","gotcha","perfect","awesome","nice","yep","nope","hi",
    "hello","hey","continue","go","next","more","please","again","back","stop",
    "that","this","it","so","and","but","or","the","a","an",
}

_STRIP_PREFIXES = re.compile(
    r"^(can you|could you|please|i need (you )?to|i want (you )?to|"
    r"help me (to )?|i'd like (you )?to|i would like (you )?to|"
    r"how do i|how can i|how come|what is|what are|can we|let's|lets|"
    r"i have a|i've got a|i got a|i'm trying to|i am trying to)\s+",
    re.IGNORECASE
)

# Second pass: strip action-verb fluff that leaves titles sounding like commands
# e.g. "take a look at the websocket code" → "the websocket code"
_STRIP_ACTION_FLUFF = re.compile(
    r"^(please\s+)?(go ahead and |go into |go and |go |"
    r"take a look at |take a look |look at |look into |"
    r"check out |check on |check |have a look at |have a look |"
    r"figure out |work on |deal with |sort out |dig into |dive into |get into |"
    r"see if you can |see if |see about |see |"
    r"make sure (that )?(the |that |it |we |they |I )?"
    r"|ensure (that )?(the |that |it |we |they )?"
    r"|confirm (that )?(the |that |it |we |they )?)",
    re.IGNORECASE
)

_ARROW = "\u2192"   # → (Read tool line-number prefix)

# URLs, file paths, and IP:port patterns to strip from title text
_URL_OR_PATH = re.compile(
    r"(https?://\S+|file:///\S+|[A-Z]:\\[\w\\./\-]+|/[\w/.\-]{10,}|"
    r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?)",
    re.IGNORECASE
)

# Leading filler words to strip after prefix removal
_LEADING_FILLER = re.compile(
    r"^(the|my|a|an|our|this|that|some|and|or|but|so|into|on|for|"
    r"every time|i see|i\'?m|i am|i)\s+",
    re.IGNORECASE
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_trivial(text: str) -> bool:
    words = text.lower().split()
    return not words or (len(words) <= 2 and all(w.strip(".,!?") in _TRIVIAL for w in words))


def _clean_message(text: str) -> str:
    """Strip system tags, continuation preambles, and normalise whitespace."""
    text = re.sub(r"<[^>]{1,60}>.*?</[^>]{1,60}>", " ", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]{1,60}/?>", " ", text)
    text = re.sub(r"^This (session is being continued|is a continuation).*?\n", "", text, flags=re.DOTALL)
    text = re.sub(r"\*\*What we were working on:\*\*.*?(?=\n\n|\Z)", "", text, flags=re.DOTALL)
    text = re.sub(r"\*\*Key context.*?\*\*.*?(?=\n\n|\Z)", "", text, flags=re.DOTALL)
    text = re.sub(r"\*\*Most recent exchanges.*?\*\*.*?(?=\n\n|\Z)", "", text, flags=re.DOTALL)
    text = re.sub("^\\s*\\d+" + _ARROW, "", text, flags=re.MULTILINE)
    text = re.sub(r"/\*|\*/", " ", text)
    return " ".join(text.split())


def _score(text: str) -> float:
    """Score a message by topic-richness. Used by summary/analysis routes."""
    words = text.split()
    if len(words) < 3:
        return 0.0
    score = min(len(text), 250) / 12
    score += sum(1 for w in words if len(w) > 6)
    return score


def _is_system_junk(text: str) -> bool:
    """Detect system-injected content that isn't a real user message."""
    t = text.strip().lower()
    # Agent catalog / system prompt fragments
    if "# available agents" in t or "specialist agents available" in t:
        return True
    if t.startswith("you have") and "agent" in t:
        return True
    # Continuation / handoff preambles (anything _clean_message didn't catch)
    if t.startswith(("this session is being continued", "this is a continuation",
                     "the user opened", "the user selected", "the user is viewing")):
        return True
    # Read tool output that leaked through (many line-number arrows)
    if text.count(_ARROW) > 2:
        return True
    # Looks like a file dump (very long, no question marks, lots of code chars)
    if len(text) > 500 and "?" not in text[:500]:
        specials = sum(1 for c in text[:500] if c in "{}()[];=><|&^~`$@")
        if specials > 20:
            return True
    return False


def _extract_user_texts(messages: list, max_msgs: int = 5, max_chars: int = 300) -> list:
    """Pull the first few real user messages (no tool results, no system junk)."""
    texts = []
    for m in messages:
        if m.get("role") != "user" or m.get("type") == "tool_result":
            continue
        raw = _clean_message(m.get("content", ""))
        if not raw or _is_trivial(raw):
            continue
        # Only check system junk on the first ~500 chars — long user messages
        # with pasted content can trigger false positives (e.g. arrow chars)
        if _is_system_junk(raw[:500]):
            continue
        texts.append(raw[:max_chars])
        if len(texts) >= max_msgs:
            break
    return texts


# ---------------------------------------------------------------------------
# LLM title generation (primary)
# ---------------------------------------------------------------------------

def _has_word_overlap(title: str, source_texts: list) -> bool:
    """Check that the title shares at least one meaningful word with the input."""
    title_words = {w.lower().strip(".,;:!?\"'") for w in title.split() if len(w) > 3}
    if not title_words:
        return True  # very short words only — can't validate, allow it
    source_blob = " ".join(source_texts).lower()
    return any(w in source_blob for w in title_words)


_TITLE_SYSTEM_PROMPT = (
    "You generate very short titles for coding chat sessions. "
    "Rules: 3-4 words MAX, describe the core task, use words from the messages, "
    "never include file paths or URLs.\n\n"
    "Examples:\n"
    "Input: 'can you look at the incoming git changes and see whether its trivial to pull them?'\n"
    "Title: Review git changes\n\n"
    "Input: 'I'm hitting issues where it goes into idle state after messaging'\n"
    "Title: Debug idle state\n\n"
    "Input: 'take a look at the front end and identify polish opportunities'\n"
    "Title: Frontend polish pass\n\n"
    "Input: 'audit my test suite and identify gaps then patch them'\n"
    "Title: Test suite gaps\n\n"
    "Input: 'in prod i keep getting random 502 errors'\n"
    "Title: Fix 502 errors\n\n"
    "Reply with ONLY the title, nothing else."
)


def _validate_llm_title(title: str, source_texts: list) -> str | None:
    """Apply quality checks to an LLM-generated title. Returns the title or None."""
    title = title.strip().strip("\"'").rstrip(".")
    if not title or len(title) <= 2 or len(title) >= 80:
        return None
    # Reject single-word titles — too vague to be useful
    if len(title.split()) < 2:
        log.debug("LLM title rejected (single word): %r", title)
        return None
    # Reject all-caps gibberish (e.g. "FOOT")
    if title.isupper() and len(title) > 4:
        log.debug("LLM title rejected (all caps): %r", title)
        return None
    # Reject titles with no meaningful word overlap with the source text
    if not _has_word_overlap(title, source_texts):
        log.debug("LLM title rejected (no overlap): %r", title)
        return None
    return title


def _llm_title(messages: list) -> str | None:
    """Ask Claude Haiku for a concise session title. Returns None on failure.

    Requires ANTHROPIC_API_KEY in the environment. If not set, skips the API
    call entirely and returns None so the CLI or heuristic fallback runs.
    """
    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None

    try:
        import anthropic
    except ImportError:
        return None

    texts = _extract_user_texts(messages)
    if not texts:
        return None

    msg_block = "\n".join(f"- {t}" for t in texts)

    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=30,
            system=_TITLE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": msg_block}],
        )
        return _validate_llm_title(resp.content[0].text, texts)
    except Exception as e:
        log.debug("LLM title generation failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Daemon title generation — routes through the existing daemon connection
# so it doesn't spawn a competing CLI process that steals the TCP socket.
# ---------------------------------------------------------------------------

def _daemon_title(messages: list) -> str | None:
    """Generate a title via the session daemon (uses the web server's existing connection).

    IMPORTANT: We pass effort="low" to disable extended thinking. Without this,
    the CLI enables thinking by default, which causes the API to silently upgrade
    from Haiku to Sonnet (Haiku doesn't support extended thinking). The result is
    a slow, expensive, verbose response that fails title validation.
    """
    import time
    import uuid

    try:
        from flask import current_app
        sm = current_app.session_manager
    except Exception as e:
        log.debug("_daemon_title: no flask context: %s", e)
        return None

    if not hasattr(sm, 'start_session') or not hasattr(sm, 'is_connected'):
        log.debug("_daemon_title: session_manager missing methods")
        return None
    if not sm.is_connected:
        log.debug("_daemon_title: daemon not connected")
        return None

    texts = _extract_user_texts(messages)
    if not texts:
        log.debug("_daemon_title: no user texts extracted")
        return None

    msg_block = "\n".join(f"- {t}" for t in texts)
    sid = f"_title_{uuid.uuid4().hex[:8]}"

    from pathlib import Path as _Path
    from .config import _SYSTEM_UTILITY_CWD
    _Path(_SYSTEM_UTILITY_CWD).mkdir(parents=True, exist_ok=True)
    result = sm.start_session(
        session_id=sid,
        prompt=msg_block,
        cwd=_SYSTEM_UTILITY_CWD,
        system_prompt=_TITLE_SYSTEM_PROMPT + "\n\nIMPORTANT: Do NOT use any tools. Just reply with the title text directly.",
        max_turns=1,
        model="haiku",
        allowed_tools=[],
        permission_mode="plan",
        session_type="title",
        extra_args={"effort": "low"},
    )
    log.debug("_daemon_title: start_session result: %s", result)
    if not result or not result.get("ok"):
        return None

    # Poll for completion (max ~12s — Haiku with effort=low finishes in 2-4s,
    # but CLI startup on Windows can add latency)
    for _ in range(60):
        time.sleep(0.2)
        # Check state first — faster than fetching entries
        state = sm.get_session_state(sid)
        done = False
        if isinstance(state, str) and state in ("idle", "stopped"):
            done = True
        elif isinstance(state, dict) and state.get("state") in ("idle", "stopped"):
            done = True
        if done:
            entries = sm.get_entries(sid, since=0)
            title = _extract_title_from_entries(entries, texts)
            sm.remove_session(sid)
            _cleanup_title_jsonl(sid, sm)
            return title

    # Timeout — grab whatever we have
    entries = sm.get_entries(sid, since=0)
    sm.remove_session(sid)
    _cleanup_title_jsonl(sid, sm)
    return _extract_title_from_entries(entries, texts)


def _cleanup_title_jsonl(sid: str, sm=None):
    """Delete leftover JSONL files created by title generation sessions.

    The SDK remaps session IDs, so the JSONL may be under a different name
    than the original sid.  Check both the original and remapped ID.
    """
    try:
        from app.config import _sessions_dir
        sd = _sessions_dir()
        # Try original ID
        jsonl = sd / f"{sid}.jsonl"
        if jsonl.exists():
            jsonl.unlink()
        # Try remapped ID
        if sm and hasattr(sm, '_id_aliases'):
            remapped = sm._id_aliases.get(sid)
            if remapped:
                rjsonl = sd / f"{remapped}.jsonl"
                if rjsonl.exists():
                    rjsonl.unlink()
                    return
        # Fallback: title sessions are tiny (<5KB), scan and match by content
        import json as _json
        for f in sd.glob("*.jsonl"):
            if f.stat().st_size > 5000:
                continue
            try:
                first = _json.loads(f.read_text(encoding="utf-8").split("\n", 1)[0])
                if "You generate very short titles" in first.get("content", ""):
                    f.unlink()
            except Exception:
                pass
    except Exception as e:
        log.debug("_cleanup_title_jsonl: %s", e)


def _extract_title_from_entries(entries, texts):
    """Extract a valid title from daemon session entries.

    The SDK wraps responses in agent behavior, so Haiku may return a long
    response instead of just a title. Try the full text first, then try
    each line individually (the title is usually the first non-empty line).
    """
    for e in entries:
        if not isinstance(e, dict) or e.get("kind") != "asst":
            continue
        raw = (e.get("text") or "").strip()
        if not raw:
            continue
        # Try full text first (works when response is just the title)
        title = _validate_llm_title(raw, texts)
        if title:
            return title
        # Agent response — try each line (title is usually the first line)
        for line in raw.split("\n"):
            line = line.strip().strip("#*-").strip()
            if not line:
                continue
            title = _validate_llm_title(line, texts)
            if title:
                return title
    return None


# ---------------------------------------------------------------------------
# CLI title generation — uses `claude -p` with --no-session-persistence
# to avoid writing JSONL files, and --effort low to disable extended thinking
# so the API actually uses Haiku instead of silently upgrading to Sonnet.
# ---------------------------------------------------------------------------

def _cli_title(messages: list) -> str | None:
    """Use the Claude CLI to generate a title. Works with OAuth/CLI auth.

    Uses --no-session-persistence to prevent phantom JSONL files, and
    --effort low to disable extended thinking (which would cause Haiku
    to be silently upgraded to Sonnet by the API).

    Falls back to None if the CLI is unavailable or fails.
    """
    import subprocess
    import sys

    texts = _extract_user_texts(messages)
    if not texts:
        return None

    msg_block = "\n".join(f"- {t}" for t in texts)

    prompt = (
        _TITLE_SYSTEM_PROMPT + "\n\n"
        "Here are the user messages:\n" + msg_block
    )

    try:
        from .platform_utils import NO_WINDOW
        creationflags = NO_WINDOW
        r = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "text",
             "--max-turns", "1", "--model", "haiku",
             "--effort", "low", "--no-session-persistence"],
            capture_output=True, text=True, timeout=20,
            creationflags=creationflags
        )
        if r.returncode != 0:
            log.debug("CLI title generation failed (exit %d): %s", r.returncode, r.stderr[:200])
            return None
        raw = r.stdout.strip()
        # The CLI may return multi-line output; take the first non-empty line
        for line in raw.split("\n"):
            line = line.strip()
            if line:
                title = _validate_llm_title(line, texts)
                if title:
                    return title
        log.debug("CLI title generation produced no valid title from: %r", raw[:200])
        return None
    except FileNotFoundError:
        log.debug("CLI title generation skipped: 'claude' command not found")
        return None
    except subprocess.TimeoutExpired:
        log.debug("CLI title generation timed out")
        return None
    except Exception as e:
        log.debug("CLI title generation failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Heuristic fallback
# ---------------------------------------------------------------------------

def _to_title(text: str, max_chars: int = 55) -> str:
    """Turn raw message text into a clean readable title."""
    # Strip URLs and file paths, then clean up dangling prepositions
    text = _URL_OR_PATH.sub("", text).strip()
    text = re.sub(r"\b(at|from|in|on|to|via|of)\s+(and|but|or|,)", r"\2", text)
    # Two-pass prefix stripping
    text = _STRIP_PREFIXES.sub("", text).strip()
    text = _STRIP_ACTION_FLUFF.sub("", text).strip()
    # Strip leading punctuation left over from URL/path removal
    text = text.lstrip(".,;:!?-– ").strip()
    # Strip leading filler words ("the", "my", "a", "I'm", etc.) — loop for chained filler
    for _ in range(3):
        prev = text
        text = _LEADING_FILLER.sub("", text).strip()
        if text == prev:
            break
    # Strip code comment prefixes
    text = re.sub("^(//|/\\*|\\*/|#!?\\s|" + _ARROW + ")\\s*", "", text).strip()
    # Take the first sentence/clause — find the earliest separator match
    _seps = ("\n", ". ", "? ", "! ", ", and ", ", but ", " and see ",
             " and then ", " and come back", " I think ", " i think ",
             " I'm ", " i'm ", " I don't ", " i don't ")
    best_pos = len(text)
    for sep in _seps:
        pos = text.find(sep, 0, 120)
        if pos != -1 and pos < best_pos:
            best_pos = pos
    if best_pos < len(text):
        text = text[:best_pos].strip()
    text = " ".join(text.split())
    # If stripping left us with almost nothing, signal with empty return
    if len(text.split()) < 2:
        return ""
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0].rstrip(".,;:!?")
    else:
        text = text.rstrip(".,;:!?")
    return text[:1].upper() + text[1:] if text else ""


def _heuristic_title(messages: list) -> str:
    """Fast, local title from the first non-trivial user message."""
    texts = _extract_user_texts(messages)
    if not texts:
        return "Untitled Session"

    title = _to_title(texts[0])

    # If first message produced a weak title, try the second message
    if len(title) < 12 and len(texts) > 1:
        runner = _to_title(texts[1], max_chars=45)
        if runner and runner.lower() not in title.lower():
            if title:
                title = title.rstrip("\u2026") + " \u2014 " + runner
            else:
                title = runner

    # Last resort: if stripping left us empty, take the raw first message
    if not title:
        raw = _URL_OR_PATH.sub("", texts[0]).strip()
        raw = " ".join(raw.split())
        if len(raw) > 55:
            raw = raw[:55].rsplit(" ", 1)[0].rstrip(".,;:!?")
        title = raw[:1].upper() + raw[1:] if raw else "Untitled Session"

    return title or "Untitled Session"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def smart_title(messages: list) -> str:
    """Generate a session title.

    Tries strategies in order:
      1. Session daemon          (uses existing daemon connection, effort=low)
      2. Direct Anthropic API    (needs ANTHROPIC_API_KEY)
      3. Claude CLI              (uses CLI auth, --no-session-persistence)
      4. Local heuristic         (instant, no API)
    """
    title = _daemon_title(messages)
    if title:
        log.debug("Title from daemon: %r", title)
        return title
    title = _llm_title(messages)
    if title:
        log.debug("Title from API: %r", title)
        return title
    title = _cli_title(messages)
    if title:
        log.debug("Title from CLI: %r", title)
        return title
    title = _heuristic_title(messages)
    log.debug("Title from heuristic: %r", title)
    return title
