"""
Code block extraction from session JSONL files.
"""

import difflib
import json
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Language -> default filename mapping
# ---------------------------------------------------------------------------

_LANG_DEFAULTS = {
    "python": "script.py",
    "py": "script.py",
    "javascript": "script.js",
    "js": "script.js",
    "typescript": "script.ts",
    "ts": "script.ts",
    "html": "index.html",
    "css": "styles.css",
    "bash": "setup.sh",
    "sh": "setup.sh",
    "shell": "setup.sh",
    "zsh": "setup.sh",
    "cmd": "setup.bat",
    "powershell": "setup.ps1",
    "ps1": "setup.ps1",
    "sql": "query.sql",
    "json": "data.json",
    "yaml": "config.yaml",
    "yml": "config.yaml",
    "xml": "data.xml",
    "markdown": "README.md",
    "md": "README.md",
    "rust": "main.rs",
    "go": "main.go",
    "java": "Main.java",
    "c": "main.c",
    "cpp": "main.cpp",
    "ruby": "script.rb",
    "rb": "script.rb",
    "php": "script.php",
    "swift": "script.swift",
    "kotlin": "script.kt",
    "r": "script.r",
    "dockerfile": "Dockerfile",
    "toml": "config.toml",
    "ini": "config.ini",
}

_SHELL_LANGS = {"bash", "sh", "shell", "cmd", "powershell", "zsh", "ps1"}

_FILENAME_PATTERNS = [
    re.compile(r'["`\']([A-Za-z0-9_\-\.]+\.[a-zA-Z0-9]+)["`\']'),
    re.compile(r'(?:save as|create|file|write to|named?)\s+["`\']?([A-Za-z0-9_\-\.]+\.[a-zA-Z0-9]+)["`\']?', re.IGNORECASE),
]


def _infer_filename(lang: str, surrounding_text: str) -> str | None:
    """Try to find an explicit filename in surrounding text, fall back to lang default."""
    for pat in _FILENAME_PATTERNS:
        m = pat.search(surrounding_text)
        if m:
            fname = m.group(1)
            # Filter out very generic matches like "etc."
            if len(fname) > 3 and "." in fname:
                return fname
    lang_key = (lang or "").lower()
    return _LANG_DEFAULTS.get(lang_key)


def _block_similarity(a: str, b: str) -> float:
    """Return similarity ratio between two strings (0.0-1.0)."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _extract_code_blocks(path: Path) -> list:
    """
    Read a session .jsonl file and extract all markdown code fence blocks
    from user and assistant messages.
    """
    CODE_FENCE = re.compile(r'```([^\n`]*)\n(.*?)```', re.DOTALL)

    raw_messages = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                t = obj.get("type", "")
                if t in ("user", "assistant"):
                    role = t
                    msg = obj.get("message", {})
                    raw = msg.get("content", "")
                    if isinstance(raw, str):
                        content = raw
                    elif isinstance(raw, list):
                        parts = []
                        for block in raw:
                            if isinstance(block, dict) and block.get("type") == "text":
                                parts.append(block.get("text", ""))
                        content = "\n".join(parts)
                    else:
                        content = ""
                    if content.strip():
                        raw_messages.append({"role": role, "content": content})
    except Exception:
        return []

    blocks = []
    # Track filename usage counts for deduplication
    filename_counts: dict[str, int] = {}

    for msg_index, msg in enumerate(raw_messages):
        content = msg["content"]
        role = msg["role"]
        for m in CODE_FENCE.finditer(content):
            lang = (m.group(1) or "").strip().lower()
            code = m.group(2)
            # Get surrounding text (text before this match in the message)
            surrounding = content[:m.start()] + content[m.end():]
            base_filename = _infer_filename(lang, surrounding)

            is_shell = lang in _SHELL_LANGS

            blocks.append({
                "language": lang,
                "content": code,
                "msg_index": msg_index,
                "role": role,
                "inferred_filename": base_filename,
                "is_shell": is_shell,
                "duplicate_of": None,
                "_base_filename": base_filename,
            })

    # Detect near-duplicates
    for i in range(len(blocks)):
        if blocks[i]["duplicate_of"] is not None:
            continue
        for j in range(i + 1, len(blocks)):
            if blocks[j]["duplicate_of"] is not None:
                continue
            if blocks[i]["language"] == blocks[j]["language"]:
                ratio = _block_similarity(blocks[i]["content"], blocks[j]["content"])
                if ratio > 0.85:
                    blocks[j]["duplicate_of"] = i

    # Assign unique filenames (append _2, _3 for same-named blocks)
    filename_counts = {}
    for b in blocks:
        base = b["_base_filename"]
        if base is None:
            b["inferred_filename"] = None
            continue
        if base not in filename_counts:
            filename_counts[base] = 1
            b["inferred_filename"] = base
        else:
            filename_counts[base] += 1
            # Insert suffix before extension
            parts = base.rsplit(".", 1)
            if len(parts) == 2:
                b["inferred_filename"] = f"{parts[0]}_{filename_counts[base]}.{parts[1]}"
            else:
                b["inferred_filename"] = f"{base}_{filename_counts[base]}"

    # Clean up internal key
    for b in blocks:
        b.pop("_base_filename", None)

    return blocks
