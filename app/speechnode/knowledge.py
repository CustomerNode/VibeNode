"""
SpeechNode Codebase Knowledge Layer.

This is the part that makes SpeechNode smarter than any generic recognizer:
it reads the *project you are working in* and produces a compact "bias prompt"
of the vocabulary that actually matters — identifiers, file names, proper nouns
— so the Whisper decoder is primed to spell them correctly ("Claude",
"VibeNode", "SpeechNode", your function names) instead of guessing.

Cheap and self-contained:
* Vocabulary comes from ``git ls-files`` (falls back to a bounded walk).
* Only a capped sample of source files is scanned, with a byte cap per file.
* Results are cached per directory with a short TTL.

Whisper's ``initial_prompt`` is capped (~224 tokens) and weights the END of
the prompt most heavily, so we emit a compact, comma-separated list with the
highest-value terms last.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections import Counter
from pathlib import Path

# Cache: {root_dir: (timestamp, bias_prompt)}
_CACHE: dict[str, tuple[float, str]] = {}
_CACHE_TTL = 300.0  # seconds

_MAX_FILES = 350          # cap files scanned per build
_MAX_BYTES_PER_FILE = 60_000
_MAX_TERMS = 60           # terms in the bias prompt
_PROMPT_CHAR_BUDGET = 850  # rough proxy for the ~224-token initial_prompt cap

# Source-ish extensions worth scanning for identifiers.
_SCAN_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".md", ".json",
    ".java", ".go", ".rs", ".rb", ".php", ".c", ".h", ".cpp", ".cs", ".swift",
    ".kt", ".sh", ".yml", ".yaml", ".toml", ".sql", ".vue", ".svelte",
}
_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".cache", "venv", ".venv", "env",
    "dist", "build", "vendor", "site-packages", ".idea", ".vscode", "backups",
    "logs", "screenshots",
}

# Programming keywords + ultra-common English that are noise for biasing.
_STOPWORDS = set("""
the and for are but not you your with this that have has from will would can could
def class return self import export const let var function async await async if else
elif while for try except catch finally with as in is of to on at by it its was were
true false null none void int str bool list dict set new value name type data id key
get set add new use run make new test file path dir name text item list main app api
""".split())

# Universally mistranscribed dev/brand terms — seeded near the END (highest weight).
_SEED_TERMS = [
    "API", "UI", "UX", "JSON", "async", "kanban", "repo",
    "Whisper", "Anthropic", "Claude", "VibeNode", "SpeechNode",
]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolve_root(cwd: str | None) -> Path:
    """Pick the directory to learn vocabulary from."""
    if cwd:
        try:
            p = Path(cwd).expanduser()
            if p.is_dir():
                return p
        except Exception:
            pass
    return _repo_root()


def _list_files(root: Path) -> list[Path]:
    """Project files via ``git ls-files``; fall back to a bounded walk."""
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=str(root), capture_output=True,
            text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            files = []
            for line in out.stdout.splitlines():
                ext = os.path.splitext(line)[1].lower()
                if ext in _SCAN_EXTS:
                    files.append(root / line)
                if len(files) >= _MAX_FILES:
                    break
            if files:
                return files
    except Exception:
        pass
    # Fallback: bounded walk
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in _SCAN_EXTS:
                files.append(Path(dirpath) / fn)
                if len(files) >= _MAX_FILES:
                    return files
    return files


def _split_identifier(tok: str) -> list[str]:
    """Split snake_case / kebab / camelCase into component words."""
    out: list[str] = []
    for part in re.split(r"[_\-]", tok):
        out.extend(re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", part))
    return out


_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def _build_counter(root: Path) -> Counter:
    counter: Counter = Counter()
    for fp in _list_files(root):
        # File name itself (e.g. "session_manager" -> session, manager)
        stem = fp.stem
        for w in _split_identifier(stem):
            if len(w) >= 3 and w.lower() not in _STOPWORDS:
                counter[w] += 2  # filenames are high-signal
        try:
            with open(fp, "r", encoding="utf-8", errors="ignore") as fh:
                blob = fh.read(_MAX_BYTES_PER_FILE)
        except Exception:
            continue
        for m in _IDENT_RE.finditer(blob):
            tok = m.group(0)
            low = tok.lower()
            if low in _STOPWORDS or tok.isdigit():
                continue
            # Keep the canonical spelling (so the prompt biases toward "SpeechNode").
            counter[tok] += 1
    return counter


def build_bias_prompt(cwd: str | None = None, extra_terms: list[str] | None = None) -> str:
    """
    Return a compact ``initial_prompt`` string of project vocabulary.

    Highest-value terms (project-distinctive identifiers + seeds + any caller
    ``extra_terms``) are placed LAST, where Whisper weights the prompt most.
    Cached per directory with a short TTL. Never raises — returns "" on failure.
    """
    try:
        root = _resolve_root(cwd)
        key = str(root)
        now = time.time()
        cached = _CACHE.get(key)
        if cached and (now - cached[0]) < _CACHE_TTL and not extra_terms:
            return cached[1]

        counter = _build_counter(root)
        # Top distinctive terms by frequency (already stop-filtered).
        ranked = [t for t, _ in counter.most_common(_MAX_TERMS)]

        # Order: frequent terms first, then seeds + extras near the end (high weight).
        tail = []
        for t in (extra_terms or []) + _SEED_TERMS:
            if t and t not in tail:
                tail.append(t)
        # De-dupe ranked against tail (tail wins the prime end slot).
        head = [t for t in ranked if t not in tail]

        terms = head + tail
        # Build under the char budget.
        prompt = ""
        for t in terms:
            candidate = (prompt + ", " + t) if prompt else t
            if len(candidate) > _PROMPT_CHAR_BUDGET:
                break
            prompt = candidate
        prompt = (prompt + ".") if prompt else ""

        if not extra_terms:
            _CACHE[key] = (now, prompt)
        return prompt
    except Exception:
        return ""
