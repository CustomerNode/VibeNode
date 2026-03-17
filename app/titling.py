"""
Smart title generation -- scoring and titling logic for sessions.
"""

import re

# ---------------------------------------------------------------------------
# Trivial-message detection
# ---------------------------------------------------------------------------

_TRIVIAL = {
    "yes","no","ok","okay","sure","thanks","thank","good","great","cool","done",
    "right","fine","got","gotcha","perfect","awesome","nice","yep","nope","hi",
    "hello","hey","continue","go","next","more","please","again","back","stop",
    "that","this","it","so","and","but","or","the","a","an",
}

# Filler prefixes to strip from the start of a message before titling
_STRIP_PREFIXES = re.compile(
    r"^(can you|could you|please|i need (you )?to|i want (you )?to|"
    r"help me (to )?|i'd like (you )?to|i would like (you )?to|"
    r"how do i|how can i|what is|what are|can we|let's|lets|"
    r"i have a|i've got a|i got a|i'm trying to|i am trying to)\s+",
    re.IGNORECASE
)


def _clean_message(text: str) -> str:
    """Strip system tags, continuation preambles, and normalise whitespace."""
    text = re.sub(r"<[^>]{1,60}>.*?</[^>]{1,60}>", " ", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]{1,60}/?>", " ", text)
    # Strip continuation preambles
    text = re.sub(r"^This (session is being continued|is a continuation).*?\n", "", text, flags=re.DOTALL)
    text = re.sub(r"\*\*What we were working on:\*\*.*?(?=\n\n|\Z)", "", text, flags=re.DOTALL)
    text = re.sub(r"\*\*Key context.*?\*\*.*?(?=\n\n|\Z)", "", text, flags=re.DOTALL)
    text = re.sub(r"\*\*Most recent exchanges.*?\*\*.*?(?=\n\n|\Z)", "", text, flags=re.DOTALL)
    return " ".join(text.split())


def _is_trivial(text: str) -> bool:
    words = text.lower().split()
    return not words or (len(words) <= 2 and all(w.strip(".,!?") in _TRIVIAL for w in words))


def _score(text: str) -> float:
    """Score a message by how topic-rich it is."""
    words = text.split()
    if len(words) < 3:
        return 0.0
    score = 0.0
    score += min(len(text), 250) / 12          # length value (capped)
    score += sum(1 for w in words if len(w) > 6)   # specific/longer words
    score += text.count("\n") * 0.5             # structured content
    # Penalise if it looks like a system prompt or pasted code block
    if text.strip().startswith(("```", "import ", "def ", "class ", "SELECT ", "<")):
        score *= 0.2
    if "continuation" in text.lower() or "previous session" in text.lower():
        score *= 0.05
    return score


def _to_title(text: str, max_chars: int = 65) -> str:
    """Turn raw message text into a clean readable title."""
    # Strip leading filler phrases
    text = _STRIP_PREFIXES.sub("", text).strip()
    # Take only the first sentence/line (stop at newline or sentence end)
    for sep in ("\n", ". ", "? ", "! "):
        if sep in text[:120]:
            text = text[:text.index(sep)].strip()
            break
    # Collapse whitespace
    text = " ".join(text.split())
    # Trim to max_chars at a word boundary
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0].rstrip(".,;:!?") + "\u2026"
    else:
        text = text.rstrip(".,;:!?")
    return text[:1].upper() + text[1:] if text else ""


def smart_title(messages: list) -> str:
    """Derive a descriptive title by scoring all user messages."""
    user_msgs = [(i, m) for i, m in enumerate(messages) if m.get("role") == "user"]
    scored = []
    n = len(user_msgs) or 1
    for rank, (orig_idx, m) in enumerate(user_msgs):
        text = _clean_message(m.get("content", ""))
        if not text or _is_trivial(text):
            continue
        s = _score(text)
        if s <= 0:
            continue
        pos = rank / max(n - 1, 1)   # 0.0 = first message, 1.0 = last
        scored.append((s, pos, text))

    if not scored:
        return "Untitled Session"

    scored.sort(key=lambda x: x[0], reverse=True)

    # Outlier detection (on raw scores): a single high-scoring message in the back
    # 40% of a long session is often a side topic (e.g. "write me a cover email").
    # Remove it if it dominates by >1.4x AND sits past 60% of the session.
    while len(scored) > 1:
        top_s, top_pos, _ = scored[0]
        second_s = scored[1][0]
        if top_pos > 0.60 and top_s > 1.4 * second_s:
            scored.pop(0)
        else:
            break

    # After outlier removal, apply a small bonus for early messages (state the purpose)
    for i, (s, pos, text) in enumerate(scored):
        if pos < 0.10:
            scored[i] = (s * 1.25, pos, text)
    scored.sort(key=lambda x: x[0], reverse=True)

    best = scored[0][2]
    title = _to_title(best)

    # If result is still short and there's a runner-up, append context from it
    if len(title.rstrip("\u2026")) < 30 and len(scored) > 1:
        runner = _to_title(scored[1][2], max_chars=35)
        if runner and runner.lower() not in title.lower():
            title = title.rstrip("\u2026") + " \u2014 " + runner

    return title or "Untitled Session"
