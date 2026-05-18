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


# Negative-pattern set for LLM titles. Each pattern below corresponds to an
# observed phantom-title shape that polluted ``_session_names.json`` in
# production (see docs/plans/phantom-sessions-fix-spec.md, LEAK B). The
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


def _is_prompt_echo(text: str) -> bool:
    """True if *text* contains any literal system-prompt phrase."""
    lower = text.lower()
    return any(p in lower for p in _PROMPT_ECHO_PHRASES)


def _validate_llm_title(title: str, source_texts: list) -> str | None:
    """Apply quality checks to an LLM-generated title. Returns the title or None.

    Negative patterns rejected (each one corresponds to a phantom-title shape
    observed in production — see docs/plans/phantom-sessions-fix-spec.md
    LEAK B):

      - Empty / ``len <= 2`` / ``len >= 80`` — too short or too long.
      - Single-word titles — too vague.
      - All-caps strings longer than 4 chars — gibberish.
      - More than 8 words — system prompt asks for 3-4 words MAX; anything
        much longer is almost certainly an instruction-style response, not a
        title.
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
    words = title.split()
    # Reject single-word titles — too vague to be useful
    if len(words) < 2:
        log.debug("LLM title rejected (single word): %r", title)
        return None
    # Reject titles past 8 words — system prompt asks for 3-4 MAX
    if len(words) > 8:
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
    # exact shape of the 169-phantom production bug.
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
            _cleanup_title_jsonl(sid, sm, utility_project)
            return title

    # Timeout — grab whatever we have
    entries = sm.get_entries(sid, since=0)
    sm.remove_session(sid)
    _cleanup_title_jsonl(sid, sm, utility_project)
    return _extract_title_from_entries(entries, texts)


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
         signature. The 5 KB size cap from the previous implementation has
         been removed — Haiku occasionally upgrades to Sonnet for thinking
         and produces multi-KB responses; size is not a reliable filter.

    After deletion, tombstone the sid in the system utility project so a
    late-flushed JSONL with the same name stays hidden.
    """
    try:
        from app.config import _sessions_dir
        from app.session_store import _mark_deleted
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
    itself is rejected by ``_validate_llm_title`` (LEAK B negative pattern).
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
            # Numbered-list peel: "1. Frontend polish pass" -> "Frontend polish pass"
            m = _LIST_PREFIX_RE.match(line)
            if m:
                peeled = line[m.end():].strip().strip("#*-").strip()
                if peeled:
                    title = _validate_llm_title(peeled, texts)
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
