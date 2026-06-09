"""
SpeechNode post-processing — clean up the raw transcript.

Whisper already gives us punctuation and capitalization, so this layer focuses
on the things a speech model leaves behind:

* **Restart / repetition disfluencies** ("let's open the — let's open the
  config" -> "let's open the config"). This is the user's "looking for repeats"
  idea: when you pause to think and then re-say the same words to get rolling
  again, we collapse the duplicate.
* **Filler words** ("um", "uh", "er") stripped.
* **Alias correction** — a small, safe map of phrases the bias prompt can still
  miss ("quad code" -> "Claude Code"), plus any caller-supplied aliases (the
  learning flywheel feeds this over time).

Everything here is pure string work — no model, no dependency. It never raises;
on any error it returns its input unchanged.

``correct_terms`` (phonetic Soundex matching against project vocab) is included
but OFF by default: at the recognition stage the Knowledge Layer bias prompt is
the primary term fix, and aggressive phonetic replacement risks turning normal
words into jargon. It is here for the opt-in flywheel/backstop phase.
"""

from __future__ import annotations

import re

# Standalone filler tokens to drop.
_FILLERS = {"um", "umm", "uh", "uhh", "uhm", "erm", "er", "hmm", "mmm", "mhm"}

# Single-word doublings that are usually intentional emphasis — don't collapse.
_EMPHASIS_OK = {"no", "yeah", "yes", "very", "really", "so", "go", "ha",
                "hey", "ok", "okay", "bye", "now", "right"}

# Small, safe built-in alias map for the worst offenders the bias prompt misses.
_BUILTIN_ALIASES = {
    "quad code": "Claude Code",
    "cloud code": "Claude Code",
    "claude code": "Claude Code",
    "clod": "Claude",
    "claud": "Claude",
    "vibe node": "VibeNode",
    "vibenode": "VibeNode",
    "speech node": "SpeechNode",
    "speechnode": "SpeechNode",
}


def _norm(tok: str) -> str:
    """Lowercase a token with surrounding punctuation stripped (for comparison)."""
    return re.sub(r"^[^\w]+|[^\w]+$", "", tok).lower()


def strip_fillers(text: str) -> str:
    out = [t for t in text.split() if _norm(t) not in _FILLERS]
    return " ".join(out)


def collapse_repeats(text: str) -> str:
    """
    Collapse consecutive repeated word-spans (restart disfluencies).

    Handles multi-word restarts and triples. Single-word doublings are only
    collapsed when they are NOT in the emphasis allowlist, so "no no no" and
    "very very" survive while "the the" / "let's let's" do not.
    """
    tokens = text.split()
    if len(tokens) < 2:
        return text
    norm = [_norm(t) for t in tokens]
    max_n = min(6, len(tokens) // 2)

    changed = True
    while changed:
        changed = False
        for n in range(max_n, 0, -1):
            i = 0
            while i + 2 * n <= len(tokens):
                a = norm[i:i + n]
                b = norm[i + n:i + 2 * n]
                if a == b and all(a):
                    if n == 1 and a[0] in _EMPHASIS_OK:
                        i += 1
                        continue
                    # Drop the FIRST copy (keep the recovered/second phrasing).
                    del tokens[i:i + n]
                    del norm[i:i + n]
                    changed = True
                    # stay at i to catch triples (a a a -> a)
                else:
                    i += 1
            if changed:
                break
    return " ".join(tokens)


def apply_aliases(text: str, aliases: dict[str, str]) -> str:
    """Case-insensitive, whole-phrase replacement (longest phrases first)."""
    for phrase in sorted(aliases, key=len, reverse=True):
        repl = aliases[phrase]
        pattern = re.compile(r"\b" + re.escape(phrase) + r"\b", re.IGNORECASE)
        text = pattern.sub(repl, text)
    return text


def _tidy(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)  # no space before punctuation
    if text:
        text = text[0].upper() + text[1:]
    return text


# --------------------------------------------------------------------------- #
# Optional phonetic backstop (OFF by default — opt-in flywheel phase)
# --------------------------------------------------------------------------- #
def soundex(word: str) -> str:
    word = re.sub(r"[^A-Za-z]", "", word).upper()
    if not word:
        return ""
    codes = {**dict.fromkeys("BFPV", "1"), **dict.fromkeys("CGJKQSXZ", "2"),
             **dict.fromkeys("DT", "3"), "L": "4",
             **dict.fromkeys("MN", "5"), "R": "6"}
    first = word[0]
    tail = []
    prev = codes.get(first, "")
    for ch in word[1:]:
        c = codes.get(ch, "")
        if c and c != prev:
            tail.append(c)
        if ch not in "HW":
            prev = c
    return (first + "".join(tail) + "000")[:4]


def correct_terms(text: str, vocab_terms) -> str:
    """
    Conservative phonetic correction against project vocab. OFF by default.

    Only replaces a token when exactly one vocab term shares its Soundex key,
    the token is reasonably long, and the vocab term differs only in spelling.
    """
    try:
        index: dict[str, set] = {}
        for term in vocab_terms or []:
            index.setdefault(soundex(term), set()).add(term)

        def fix(m):
            tok = m.group(0)
            if len(tok) < 5 or not tok.isalpha():
                return tok
            cands = index.get(soundex(tok), set())
            cands = {c for c in cands if c.lower() != tok.lower()}
            if len(cands) == 1:
                return next(iter(cands))
            return tok

        return re.sub(r"[A-Za-z]{5,}", fix, text)
    except Exception:
        return text


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
def clean(text: str, aliases: dict[str, str] | None = None,
          vocab_terms=None, enable_term_correction: bool = False) -> str:
    """Full cleanup pipeline. Never raises."""
    try:
        if not text or not text.strip():
            return text or ""
        t = strip_fillers(text)
        t = collapse_repeats(t)
        merged = dict(_BUILTIN_ALIASES)
        if aliases:
            merged.update(aliases)
        t = apply_aliases(t, merged)
        if enable_term_correction and vocab_terms:
            t = correct_terms(t, vocab_terms)
        return _tidy(t)
    except Exception:
        return text or ""
