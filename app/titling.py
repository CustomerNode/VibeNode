"""
Session title generation — uses Claude Haiku for concise, high-quality names
with a fast heuristic fallback if the API is unavailable.
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
    r"how do i|how can i|what is|what are|can we|let's|lets|"
    r"i have a|i've got a|i got a|i'm trying to|i am trying to)\s+",
    re.IGNORECASE
)

_ARROW = "\u2192"   # → (Read tool line-number prefix)

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
        if not raw or _is_trivial(raw) or _is_system_junk(raw):
            continue
        texts.append(raw[:max_chars])
        if len(texts) >= max_msgs:
            break
    return texts


# ---------------------------------------------------------------------------
# LLM title generation (primary)
# ---------------------------------------------------------------------------

def _llm_title(messages: list) -> str | None:
    """Ask Claude Haiku for a concise session title. Returns None on failure."""
    try:
        import anthropic
    except ImportError:
        return None

    texts = _extract_user_texts(messages)
    if not texts:
        return None

    # Build a compact prompt — just the user's opening messages
    msg_block = "\n".join(f"- {t}" for t in texts)
    prompt = (
        "Here are the user's messages from the start of a coding chat session:\n\n"
        f"{msg_block}\n\n"
        "Write a short title (3-8 words) that captures what this session is about. "
        "Focus on the user's intent/goal, not file names or implementation details. "
        "Reply with ONLY the title, no quotes, no punctuation at the end."
    )

    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model="claude-haiku-4-20250414",
            max_tokens=30,
            messages=[{"role": "user", "content": prompt}],
        )
        title = resp.content[0].text.strip().strip("\"'").rstrip(".")
        if title and 2 < len(title) < 80:
            return title
        return None
    except Exception as e:
        log.debug("LLM title generation failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Heuristic fallback
# ---------------------------------------------------------------------------

def _to_title(text: str, max_chars: int = 65) -> str:
    """Turn raw message text into a clean readable title."""
    text = _STRIP_PREFIXES.sub("", text).strip()
    text = re.sub("^(//|/\\*|\\*/|#!?\\s|" + _ARROW + ")\\s*", "", text).strip()
    for sep in ("\n", ". ", "? ", "! "):
        if sep in text[:120]:
            text = text[:text.index(sep)].strip()
            break
    text = " ".join(text.split())
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0].rstrip(".,;:!?") + "\u2026"
    else:
        text = text.rstrip(".,;:!?")
    return text[:1].upper() + text[1:] if text else ""


def _heuristic_title(messages: list) -> str:
    """Fast, local title from the first non-trivial user message."""
    texts = _extract_user_texts(messages)
    if not texts:
        return "Untitled Session"

    title = _to_title(texts[0])

    if len(title) < 15 and len(texts) > 1:
        runner = _to_title(texts[1], max_chars=45)
        if runner and runner.lower() not in title.lower():
            title = title.rstrip("\u2026") + " \u2014 " + runner

    return title or "Untitled Session"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def smart_title(messages: list) -> str:
    """Generate a session title. Tries LLM first, falls back to heuristic."""
    title = _llm_title(messages)
    if title:
        return title
    return _heuristic_title(messages)
