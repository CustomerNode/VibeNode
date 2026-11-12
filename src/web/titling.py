"""
Session title generation — uses Claude Haiku for concise, high-quality names
with a fast heuristic fallback if the API is unavailable.

Title generation chain:
  1. Daemon (haiku via existing daemon connection, effort=low)
  2. Direct Anthropic API (fastest, needs ANTHROPIC_API_KEY)
  3. Claude CLI            (uses CLI auth/OAuth, no key needed)
  4. Heuristic             (instant, no API needed)

Title-utility JSONL cleanup
---------------------------
Daemon-based title generation spawns a session with ``cwd=_SYSTEM_UTILITY_CWD``
(``~/.claude/_system``). The SDK persists those JSONL files in the encoded form
of that path — ``C--Users-<user>--claude--system`` — **never under the user's
active project**. The cleanup logic must therefore look for the file in the
*system utility project*, not in ``_active_project``.

``_cleanup_title_jsonl(sid, sm, project)`` tries three strategies, in order:

  1. ``<system_project>/<sid>.jsonl`` — original spawn-time sid.
  2. ``<system_project>/<remapped_sid>.jsonl`` — looked up via
     ``sm._id_aliases`` (the in-memory daemon-client proxy map updated by the
     ``session_id_remapped`` IPC event). This covers the same-process case.
  3. Content-scan fallback: iterate the system project's ``*.jsonl`` files,
     parse the first line as JSON, match the system-prompt signature. This
     covers the cross-restart case (where ``_id_aliases`` was cleared) and
     also picks up oversized title JSONLs (e.g. when Haiku silently upgrades
     to Sonnet and the response goes long).

After unlinking, the sid is tombstoned in the system project so a
late-flushed JSONL with the same ID stays hidden.

PERF NOTE: this cleanup is *not* on any per-turn hot path — it runs once per
title-gen completion (a few times per minute at most). No PERF-CRITICAL
constraint applies here.
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

# Text VibeNode injects into a user's message (never the user's own words).
_INJECTED_BLOCK = re.compile(r"\u27e6VN-(?:MOBILE|NOTE)\u27e7.*?(\u27e6/VN-(?:MOBILE|NOTE)\u27e7|\Z)", re.DOTALL)
_SENT_FROM_LINE = re.compile(
    r"\n*[ \t]*Sent from \S+ at [^\n]*(\(transcribed from voice[^)]*\))?[ \t]*$"
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


def _is_gibberish(text: str) -> bool:
    """A lone run of letters that is not a word: keyboard mash ("mmnnmnmnmnmke").

    Naming a session after one gives "Mmnnmnmnmnmke Message Unclear", so it is
    not offered to the namer: the session waits for a real message.
    """
    t = text.strip().strip(".,!?").lower()
    if not 6 <= len(t) <= 40 or not t.isalpha() or not t.isascii():
        return False
    return sum(c in "aeiouy" for c in t) / len(t) <= 0.25


def _clean_message(text: str) -> str:
    """Strip system tags, continuation preambles, and normalise whitespace."""
    # What VibeNode adds to a message and hides on screen (live-panel.js strips
    # the same two): the phone instructions are ~2,000 characters and only the
    # first few hundred of a message are read, so left in they are ALL the namer
    # sees, and every phone session would be named after the setup text.
    text = _INJECTED_BLOCK.sub(" ", text)
    text = _SENT_FROM_LINE.sub("", text)
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
        if not raw or _is_trivial(raw) or _is_gibberish(raw):
            continue
        # Only check system junk on the first ~500 chars — long user messages
        # with pasted content can trigger false positives (e.g. arrow chars)
        if _is_system_junk(raw[:500]):
            continue
        # A long message keeps its end too: pasted text comes first and what the
        # person is asking for usually comes last.
        if len(raw) > max_chars:
            keep = (max_chars - 3) // 2
            raw = raw[:keep] + " \u2026 " + raw[-(max_chars - 3 - keep):]
        texts.append(raw)
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
    "Rules: 3-4 words MAX, sentence case, describe the core task, use words from "
    "the messages, never include file paths or URLs, never name the app or project "
    "being worked on. The messages may open with text pasted from elsewhere (a "
    "card, a log, some code): name what the person is asking for, not the paste. "
    "Do not answer or reply to the messages.\n\n"
    "Examples:\n"
    "'can you look at the incoming git changes and see whether its trivial to pull them?' "
    "becomes: Review git changes\n"
    "'I'm hitting issues where it goes into idle state after messaging' "
    "becomes: Debug idle state\n"
    "'take a look at the front end and identify polish opportunities' "
    "becomes: Frontend polish pass\n"
    "'audit my test suite and identify gaps then patch them' "
    "becomes: Test suite gaps\n"
    "'in prod i keep getting random 502 errors' "
    "becomes: Fix 502 errors\n\n"
    "Reply with only those words: no label before them, no quotes, nothing after them."
)


def _title_system(avoid=()) -> str:
    """The system prompt, told which names are already taken in the project."""
    if not avoid:
        return _TITLE_SYSTEM_PROMPT
    taken = "; ".join(sorted(avoid))
    return _TITLE_SYSTEM_PROMPT + (
        "\n\nThese names are already taken, so be more specific than them: " + taken
    )


# Negative-pattern set for LLM titles. Each pattern below corresponds to an
# observed phantom-title shape that polluted ``_session_names.json`` in
# production. The
# patterns are deliberately narrow — they reject titles that look like task
# instructions, prompt echoes, or system-prompt fragments, NOT legitimate
# short titles. When extending this list, prefer adding a tight observed
# pattern over a broad heuristic.

# Numbered-list item: e.g. "1. Refactor X", "2) Add Y".
_LIST_PREFIX_RE = re.compile(r"^\s*\d+[.)]\s")

# Instruction-style title prefixes (LLM echoes its task back at us):
# "Title: ...", "Here's a title: ...", "Suggested title: ...".
_TITLE_PREFIX_RE = re.compile(
    r"^\s*(title|here'?s a title|the title is|suggested title)\s*[:\-]",
    re.IGNORECASE,
)

# The naming task leaking in as a label anywhere before a colon: "Session title:
# X", "Cron Stall Title: X", "Test title generation: X", "Name session: X".
_TASK_LABEL_RE = re.compile(r"^([^:]*\b(title|titles|name|naming)\b[^:]*):\s*(.*)$", re.IGNORECASE | re.DOTALL)

# ...or as the end of the name: "Deploy rows bug title", "Page title needed".
_TASK_ENDING_RE = re.compile(r"\btitle(\s+(needed|request))?\s*$", re.IGNORECASE)
_TASK_OPENING_RE = re.compile(r"^(session\s+)?title\s+(generation|request)\b", re.IGNORECASE)

# A reply, not a name: "Context check: I'll fetch the page first", "Let me look".
_REPLY_RE = re.compile(
    r"\b(i'll|i\u2019ll|i'm|i\u2019m|i am|i will|let me|your message|could you|would you)\b"
    r"|^(i|sure|okay|sorry|here's|here is|it looks)\b",
    re.IGNORECASE,
)

# Phrases lifted from the system prompt or instruction stream — never
# legitimate user-facing titles.
_PROMPT_ECHO_PHRASES = (
    "generate a title",
    "coding chat session",
    "very short title",
    "the format you showed",
    "following the format",
    "<paste>",
    "<example>",
    "your title",
    "your session",
)


MAX_TITLE_WORDS = 5
# The free local path keeps the person's own whole clause, so it may run longer.
HEURISTIC_MAX_WORDS = 8


def _is_prompt_echo(text: str) -> bool:
    """True if *text* contains any literal system-prompt phrase."""
    lower = text.lower()
    return any(p in lower for p in _PROMPT_ECHO_PHRASES)


def _validate_llm_title(title: str, source_texts: list, max_words: int | None = None) -> str | None:
    """Apply quality checks to an LLM-generated title. Returns the title or None.

    Negative patterns rejected (each one corresponds to a phantom-title shape
    observed in production):

      - Empty / ``len <= 2`` / ``len >= 80`` — too short or too long.
      - Single-word titles — too vague.
      - All-caps strings longer than 4 chars — gibberish.
      - More than 5 words — system prompt asks for 3-4 words MAX.
      - A line break, or a closing ``?`` / ``!`` — a reply, not a name.
      - The naming task as a label or an ending (``"Session title: X"``,
        ``"Deploy rows bug title"``); ``_first_valid_title`` peels a label.
      - First-person replies (``"Context check: I'll fetch the page first"``).
      - Zero word-overlap with the source text — title is unrelated.
      - Numbered-list items (``"1. Refactor X"``) — LLM produced a list
        instead of a single title.
      - Instruction-style prefixes (``"Title: ..."``, ``"Here's a title: ..."``)
        — LLM echoed its task back.
      - System-prompt echoes (``"generate a title"``, ``"coding chat session"``,
        etc.) — LLM repeated the prompt instead of obeying it.
      - Markdown-emphasis pair with "title" or "session" anywhere in the
        string (``"**Generate a title** for this session"``) — the original
        production phantom shape.
    """
    title = title.strip().strip("\"'").rstrip(".")
    if not title or len(title) <= 2 or len(title) >= 80:
        return None
    if "\n" in title or "\r" in title or title[-1] in "?!":
        log.debug("LLM title rejected (not a single name): %r", title)
        return None
    if _TASK_LABEL_RE.match(title) or _TASK_ENDING_RE.search(title) or _TASK_OPENING_RE.match(title):
        log.debug("LLM title rejected (naming task leaked): %r", title)
        return None
    if _REPLY_RE.search(title):
        log.debug("LLM title rejected (a reply): %r", title)
        return None
    words = title.split()
    # Reject single-word titles — too vague to be useful
    if len(words) < 2:
        log.debug("LLM title rejected (single word): %r", title)
        return None
    # Reject titles past 5 words — system prompt asks for 3-4 MAX
    if len(words) > (max_words or MAX_TITLE_WORDS):
        log.debug("LLM title rejected (too many words: %d): %r", len(words), title)
        return None
    # Reject all-caps gibberish (e.g. "FOOT")
    if title.isupper() and len(title) > 4:
        log.debug("LLM title rejected (all caps): %r", title)
        return None
    # Reject numbered list items (1. Foo / 2) Bar)
    if _LIST_PREFIX_RE.match(title):
        log.debug("LLM title rejected (numbered list): %r", title)
        return None
    # Reject instruction prefixes ("Title: ..." / "Here's a title: ...")
    if _TITLE_PREFIX_RE.match(title):
        log.debug("LLM title rejected (instruction prefix): %r", title)
        return None
    # Reject system-prompt echoes
    if _is_prompt_echo(title):
        log.debug("LLM title rejected (prompt echo): %r", title)
        return None
    # Reject markdown-emphasis pairs that wrap "title" or "session" — the
    # most common phantom-title shape.
    if "**" in title and ("title" in title.lower() or "session" in title.lower()):
        # Only reject if there's an actual emphasis pair (open + close)
        if title.count("**") >= 2:
            log.debug("LLM title rejected (markdown emphasis around title/session): %r", title)
            return None
    # Reject titles with no meaningful word overlap with the source text
    if not _has_word_overlap(title, source_texts):
        log.debug("LLM title rejected (no overlap): %r", title)
        return None
    return title


def _first_valid_title(raw: str, texts: list) -> str | None:
    """The first acceptable name in a model's reply.

    Tries the whole reply, then each line, and on a line carrying a label or a
    list number tries what follows it ("Session title: Fix pill padding" and
    "1. Frontend polish pass" both hold a real name).
    """
    raw = (raw or "").strip()
    title = _validate_llm_title(raw, texts)
    if title:
        return title
    for line in raw.split("\n"):
        line = line.strip().strip("#*-").strip()
        if not line:
            continue
        title = _validate_llm_title(line, texts)
        if title:
            return title
        m = _LIST_PREFIX_RE.match(line)
        if m:
            line = line[m.end():].strip().strip("#*-").strip()
            title = _validate_llm_title(line, texts)
            if title:
                return title
        m = _TITLE_PREFIX_RE.match(line) or _TASK_LABEL_RE.match(line)
        if m:
            rest = line.split(":", 1)[-1] if ":" in line else line[m.end():]
            title = _validate_llm_title(rest.strip().strip("#*-").strip(), texts)
            if title:
                return title
    return None


def _llm_title(messages: list, avoid=()) -> str | None:
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
            system=_title_system(avoid),
            messages=[{"role": "user", "content": msg_block}],
        )
        return _first_valid_title(resp.content[0].text, texts)
    except Exception as e:
        log.debug("LLM title generation failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Daemon title generation — routes through the existing daemon connection
# so it doesn't spawn a competing CLI process that steals the TCP socket.
# ---------------------------------------------------------------------------

def _daemon_title(messages: list, avoid=()) -> str | None:
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
    from .config import _SYSTEM_UTILITY_CWD, _encode_cwd
    _Path(_SYSTEM_UTILITY_CWD).mkdir(parents=True, exist_ok=True)
    # Compute the system utility project name ONCE at spawn time so cleanup
    # always targets the correct directory, even if _active_project changes
    # while the title session is in flight (project switch race — see
    # test_title_gen_project_switch_race).
    utility_project = _encode_cwd(_SYSTEM_UTILITY_CWD)
    result = sm.start_session(
        session_id=sid,
        prompt=msg_block,
        cwd=_SYSTEM_UTILITY_CWD,
        system_prompt=_title_system(avoid) + "\n\nIMPORTANT: Do NOT use any tools. Just reply with the title text directly.",
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
            _dispose_title_session(sm, sid)
            _cleanup_title_jsonl(sid, sm, utility_project)
            return title

    # Timeout — grab whatever we have
    entries = sm.get_entries(sid, since=0)
    _dispose_title_session(sm, sid)
    _cleanup_title_jsonl(sid, sm, utility_project)
    return _extract_title_from_entries(entries, texts)


def _dispose_title_session(sm, sid: str) -> None:
    """Shut the title session's CLI down, THEN forget the session.

    An IDLE session keeps its ``claude`` process alive waiting for the next
    turn.  This used to call only ``remove_session``, which drops the daemon's
    record but never stops the process, so every generated title leaked one
    idle ``claude --model haiku --effort low`` until the daemon restarted.
    Same close-then-remove contract
    every other remove_session caller in sessions_api.py follows.
    """
    try:
        sm.close_session_sync(sid)
    except Exception as e:
        log.debug("_dispose_title_session: close %s failed: %s", sid, e)
    try:
        sm.remove_session(sid)
    except Exception as e:
        log.debug("_dispose_title_session: remove %s failed: %s", sid, e)


# First-line signature used to identify title-utility JSONL files in the
# content-scan fallback. Matches the system prompt's opening rule line.
_TITLE_JSONL_SIGNATURE = "You generate very short titles"


def _cleanup_title_jsonl(sid: str, sm=None, project: str = ""):
    """Delete the leftover JSONL file from a title-generation session.

    *project* must be the encoded form of ``_SYSTEM_UTILITY_CWD`` — the
    daemon writes the JSONL into the system project, NOT into the user's
    active project. Passing the active project here (the old bug) made
    cleanup silently miss the file and leak it.

    Strategies, tried in order:
      1. ``<system_project>/<sid>.jsonl`` — original spawn-time sid.
      2. ``<system_project>/<remapped_sid>.jsonl`` — via ``sm._id_aliases``
         (only present in the same-process case; cross-restart misses).
      3. Content-scan fallback: walk the system project's ``*.jsonl`` files
         and unlink any whose first JSON line carries the title system-prompt
         signature. There is no size cap: Haiku occasionally upgrades to
         Sonnet for thinking and produces multi-KB responses, so size is not
         a reliable filter.

    After deletion, tombstone the sid in the system utility project so a
    late-flushed JSONL with the same name stays hidden.
    """
    try:
        from web.config import _sessions_dir
        from web.session_store import _mark_deleted
        sd = _sessions_dir(project)
        cleaned_ids: list[str] = []
        # Strategy 1: original ID in the system project
        jsonl = sd / f"{sid}.jsonl"
        if jsonl.exists():
            try:
                jsonl.unlink()
                cleaned_ids.append(sid)
            except Exception as e:
                log.debug("_cleanup_title_jsonl: unlink %s failed: %s", jsonl, e)
        # Strategy 2: in-memory alias (same-process only)
        if sm is not None and hasattr(sm, '_id_aliases'):
            remapped = sm._id_aliases.get(sid)
            if remapped:
                rjsonl = sd / f"{remapped}.jsonl"
                if rjsonl.exists():
                    try:
                        rjsonl.unlink()
                        cleaned_ids.append(remapped)
                    except Exception as e:
                        log.debug("_cleanup_title_jsonl: unlink %s failed: %s",
                                  rjsonl, e)
        # Strategy 3: content-scan fallback (no size cap — see docstring)
        import json as _json
        for f in sd.glob("*.jsonl"):
            try:
                first_line = f.read_text(encoding="utf-8", errors="replace").split("\n", 1)[0]
                if not first_line:
                    continue
                first = _json.loads(first_line)
                content = first.get("content", "") or first.get("system", "")
                if isinstance(content, str) and _TITLE_JSONL_SIGNATURE in content:
                    try:
                        f.unlink()
                        cleaned_ids.append(f.stem)
                    except Exception as e:
                        log.debug("_cleanup_title_jsonl: unlink %s failed: %s",
                                  f, e)
            except Exception:
                # Malformed first line or non-JSON — skip silently
                pass
        # Tombstone every cleaned id in the system project so a late flush
        # with the same name stays hidden. Defense in depth — today nothing
        # lists the system project, but registry-driven filtering does.
        for cleaned in cleaned_ids:
            try:
                _mark_deleted(cleaned, project)
            except Exception as e:
                log.debug("_cleanup_title_jsonl: tombstone %s failed: %s",
                          cleaned, e)
    except Exception as e:
        log.info("_cleanup_title_jsonl: %s", e)


def _extract_title_from_entries(entries, texts):
    """Extract a valid title from daemon session entries.

    The SDK wraps responses in agent behavior, so Haiku may return a long
    response instead of just a title. Try the full text first, then try
    each line individually (the title is usually the first non-empty line).

    When a line is a numbered-list item ("1. Frontend polish pass"), peel
    the leading number+separator and try the remainder as a candidate. This
    rescues real titles from list-shaped responses; the bare numbered line
    itself is rejected by ``_validate_llm_title`` (a negative pattern).
    """
    for e in entries:
        if not isinstance(e, dict) or e.get("kind") != "asst":
            continue
        title = _first_valid_title(e.get("text") or "", texts)
        if title:
            return title
    return None


# ---------------------------------------------------------------------------
# CLI title generation — uses `claude -p` with --no-session-persistence
# to avoid writing JSONL files, and --effort low to disable extended thinking
# so the API actually uses Haiku instead of silently upgrading to Sonnet.
# ---------------------------------------------------------------------------

def _cli_title(messages: list, avoid=()) -> str | None:
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
        _title_system(avoid) + "\n\n"
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
        title = _first_valid_title(raw, texts)
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

def collect_title_messages(path, session: dict | None = None) -> list:
    """Return the messages a transcript offers for titling.

    This is the exact source ``POST /api/autonname`` uses when no prompt text
    is supplied: ``load_session()`` messages that carry content, and, when
    that yields nothing, a raw scan of the JSONL for the first user text
    (``queue-operation`` entries or a user entry's text blocks).  Exposed so
    the background namer (``src/web/auto_namer.py``) titles from the same input
    as the route.  *session* is an already-loaded ``load_session()`` result,
    to spare a second parse.  Returns ``[]`` for an unreadable or empty
    transcript.
    """
    import json as _json
    from pathlib import Path as _Path
    from .sessions import load_session

    path = _Path(path)
    messages: list = []
    try:
        if session is None:
            session = load_session(path)
        messages = [m for m in session.get("messages", []) if m.get("content")]
    except Exception as e:
        log.debug("collect_title_messages: load_session failed for %s: %s", path, e)
        messages = []
    if messages:
        return messages
    try:
        with open(path, encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line:
                    continue
                try:
                    _entry = _json.loads(_line)
                except Exception:
                    continue
                _t = _entry.get("type", "")
                # queue-operation entries contain the original user prompt
                if _t == "queue-operation" and _entry.get("content"):
                    messages.append({"role": "user", "content": _entry["content"]})
                    break
                # user entries may have content in message.content blocks
                if _entry.get("role") == "user":
                    mc = _entry.get("message", {}).get("content", [])
                    if isinstance(mc, list):
                        for block in mc:
                            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                                messages.append({"role": "user", "content": block["text"]})
                                break
                    if messages:
                        break
    except Exception:
        pass
    return messages


def cheap_title(messages: list) -> str | None:
    """The free path: the local heuristic, accepted only when it passes the
    same validator an LLM title must pass (2-8 words, under 80 chars, shares
    a word with the source).  Returns ``None`` when the heuristic produces
    something loose, so the caller falls back to ``smart_title`` (daemon /
    API / CLI).  Used by the bulk namer, where a thousand LLM calls is the
    cost being avoided."""
    texts = _extract_user_texts(messages)
    if not texts:
        return None
    title = _heuristic_title(messages)
    if not title or title == "Untitled Session":
        return None
    # A first clause the heuristic had to cut at its character cap is a
    # sentence fragment ("You are the drafting agent of a <pipeline>"),
    # not a title, even when the fragment fits the validator's 8 words.  And
    # every prompt sharing that opening would get the same fragment.
    first = _to_title(texts[0])
    if first and title.startswith(first) and first != _to_title(texts[0], max_chars=10**6):
        return None
    return _validate_llm_title(title, texts, max_words=HEURISTIC_MAX_WORDS)


def transcript_cli_title(path) -> str | None:
    """The newest title Claude Code wrote into a transcript (``ai-title``)."""
    import json as _json
    latest = None
    try:
        with open(path, "rb") as f:
            for raw in f:
                if b'"ai-title"' not in raw:
                    continue
                try:
                    obj = _json.loads(raw)
                except Exception:
                    continue
                if obj.get("type") == "ai-title" and obj.get("aiTitle"):
                    latest = obj["aiTitle"]
    except Exception:
        return None
    return latest


def smart_title(messages: list, backup: str | None = None, avoid=()) -> str:
    """Generate a session title.

    Tries strategies in order:
      1. Session daemon          (uses existing daemon connection, effort=low)
      2. Direct Anthropic API    (needs ANTHROPIC_API_KEY)
      3. Claude CLI              (uses CLI auth, --no-session-persistence)
      4. *backup*                (Claude Code's own title for the session), if it
                                 passes the same check
      5. Local heuristic         (instant, no API), only a whole clause that
                                 passes the same check

    Returns "Untitled Session" (never saved) rather than a sentence cut short:
    an untitled session shows its first message anyway and gets named later.
    *avoid* holds names taken in the project (lowercase); none is returned.
    """
    taken = {a.lower() for a in avoid}
    extra = {"avoid": taken} if taken else {}
    for source in (_daemon_title, _llm_title, _cli_title):
        title = source(messages, **extra)
        if title and title.lower() not in taken:
            log.debug("Title from %s: %r", source.__name__, title)
            return title
    texts = _extract_user_texts(messages)
    if backup:
        title = _validate_llm_title(backup, texts or [backup])
        if title and title.lower() not in taken:
            log.debug("Title from the transcript: %r", title)
            return title
    title = cheap_title(messages)
    if title and title.lower() not in taken:
        log.debug("Title from heuristic: %r", title)
        return title
    return "Untitled Session"
