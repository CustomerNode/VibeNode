"""
Session Manager — view, rename, auto-name, and delete Claude Code sessions
Run with: python scripts/session_manager.py
Then open: http://localhost:5050
"""

import difflib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import webbrowser
import zipfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime, timezone

from flask import Flask, jsonify, request, render_template, send_file

_CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
_active_project: str = ""   # encoded dir name; empty = auto-detect
_CLAUDECODEGUI_DIR = Path(__file__).resolve().parent  # always the ClaudeCodeGUI repo

def _sessions_dir() -> Path:
    """Return the active project's session directory, auto-detecting if needed."""
    global _active_project
    if _active_project:
        p = _CLAUDE_PROJECTS / _active_project
        if p.is_dir():
            return p
    # Auto-detect: pick the project with the most recent .jsonl file
    best, best_ts = None, 0.0
    for d in _CLAUDE_PROJECTS.iterdir():
        if not d.is_dir() or d.name.startswith("subagents"):
            continue
        for f in d.glob("*.jsonl"):
            if f.stat().st_mtime > best_ts:
                best_ts = f.stat().st_mtime
                best = d
    if best:
        _active_project = best.name
        return best
    return _CLAUDE_PROJECTS

def _names_file() -> Path:
    return _sessions_dir() / "_session_names.json"

def _decode_project(encoded: str) -> str:
    """Convert C--Users-donca-Documents-FileTaskNode → C:/Users/donca/Documents/FileTaskNode (display only)."""
    if "--" in encoded:
        drive, rest = encoded.split("--", 1)
        return drive + ":/" + rest.replace("-", "/")
    return encoded

# Legacy aliases so existing code keeps working (replaced below via find/replace in routes)
SESSIONS_DIR = _sessions_dir()
NAMES_FILE   = _names_file()

app = Flask(__name__,
           template_folder=str(_CLAUDECODEGUI_DIR / "templates"),
           static_folder=str(_CLAUDECODEGUI_DIR / "static"))


# ---------------------------------------------------------------------------
# User-set name store — survives Claude Code's own auto-naming
# ---------------------------------------------------------------------------

def _load_names() -> dict:
    """Return {session_id: name} for all user-manually-set names."""
    try:
        return json.loads(_names_file().read_text(encoding="utf-8"))
    except Exception:
        return {}

def _save_name(session_id: str, name: str) -> None:
    """Persist a user-set name. Creates or updates _session_names.json."""
    names = _load_names()
    names[session_id] = name
    _names_file().write_text(json.dumps(names, indent=2, ensure_ascii=False), encoding="utf-8")

def _delete_name(session_id: str) -> None:
    """Remove a session from the user-names store (e.g. on delete)."""
    names = _load_names()
    if session_id in names:
        names.pop(session_id)
        _names_file().write_text(json.dumps(names, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_summary_cache: dict = {}  # key: (path_str, mtime, size) -> summary dict
_names_cache: dict = {"data": {}, "mtime": 0}  # cached session names

def _load_names_cached() -> dict:
    """Load session names with caching based on file mtime."""
    nf = _names_file()
    try:
        mt = nf.stat().st_mtime
    except Exception:
        return {}
    if mt != _names_cache["mtime"]:
        _names_cache["data"] = _load_names()
        _names_cache["mtime"] = mt
    return _names_cache["data"]

def _format_size(file_bytes: int) -> str:
    if file_bytes < 1024:
        return f"{file_bytes} B"
    elif file_bytes < 1024 * 1024:
        return f"{file_bytes / 1024:.1f} KB"
    return f"{file_bytes / (1024*1024):.1f} MB"

def load_session_summary(path: Path) -> dict:
    """Fast cached summary — seeks head + tail only, never reads entire file."""
    _err = {"id": path.stem, "error": "", "custom_title": None,
            "display_title": path.stem, "date": "", "last_activity": "", "preview": "",
            "last_activity_ts": 0, "sort_ts": 0, "size": "0 B", "file_bytes": 0, "message_count": 0}
    try:
        st = path.stat()
    except Exception:
        return _err

    cache_key = (str(path), st.st_mtime, st.st_size)
    cached = _summary_cache.get(cache_key)
    if cached is not None:
        return cached

    custom_title = None
    first_user_content = ""
    first_ts = None
    last_ts = None
    message_count = 0
    HEAD_SIZE = 16384
    TAIL_SIZE = 8192

    try:
        file_size = st.st_size
        with open(path, "rb") as f:
            head = f.read(HEAD_SIZE)
            # Estimate message count from file size for large files
            if file_size > HEAD_SIZE + TAIL_SIZE:
                # Read tail by seeking
                f.seek(max(0, file_size - TAIL_SIZE))
                tail = f.read()
                # Estimate message count: count in head+tail, scale by file proportion
                sampled = head + tail
                sample_count = (sampled.count(b'"type":"user"') + sampled.count(b'"type":"assistant"')
                              + sampled.count(b'"type": "user"') + sampled.count(b'"type": "assistant"'))
                sample_bytes = len(sampled)
                message_count = max(sample_count, int(sample_count * file_size / sample_bytes)) if sample_bytes else 0
            else:
                tail = head[HEAD_SIZE:]  # empty if file < HEAD_SIZE
                if file_size > HEAD_SIZE:
                    f.seek(max(0, file_size - TAIL_SIZE))
                    tail = f.read()
                message_count = (head.count(b'"type":"user"') + head.count(b'"type":"assistant"')
                               + head.count(b'"type": "user"') + head.count(b'"type": "assistant"')
                               + tail.count(b'"type":"user"') + tail.count(b'"type":"assistant"')
                               + tail.count(b'"type": "user"') + tail.count(b'"type": "assistant"'))

        head_str = head.decode("utf-8", errors="replace")
        for line in head_str.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            t = obj.get("type", "")
            if t == "custom-title":
                custom_title = obj.get("customTitle", "")
            elif t in ("user", "assistant"):
                ts_str = obj.get("timestamp", "")
                if ts_str and first_ts is None:
                    try:
                        first_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except Exception:
                        pass
                if t == "user" and not first_user_content:
                    msg = obj.get("message", {})
                    raw_c = msg.get("content", "")
                    if isinstance(raw_c, str):
                        first_user_content = raw_c.strip()
                    elif isinstance(raw_c, list):
                        parts = [b.get("text", "") for b in raw_c
                                 if isinstance(b, dict) and b.get("type") == "text"]
                        first_user_content = " ".join(parts).strip()
                if first_ts and first_user_content:
                    break

        # Scan tail for last timestamp and any late custom-title
        if file_size > HEAD_SIZE:
            tail_str = tail.decode("utf-8", errors="replace")
            for line in reversed(tail_str.splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                t = obj.get("type", "")
                if t == "custom-title":
                    custom_title = obj.get("customTitle", "")
                if t in ("user", "assistant") and last_ts is None:
                    ts_str = obj.get("timestamp", "")
                    if ts_str:
                        try:
                            last_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        except Exception:
                            pass
                if last_ts and custom_title is not None:
                    break

    except Exception:
        return _err

    if first_ts:
        date_str = first_ts.strftime("%b %d, %Y  %I:%M %p")
    else:
        date_str = datetime.fromtimestamp(st.st_mtime).strftime("%b %d, %Y  %I:%M %p")
        first_ts = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)

    if last_ts is None:
        last_ts = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    last_activity_str = last_ts.strftime("%b %d, %Y  %I:%M %p")

    preview = first_user_content[:120] + ("…" if len(first_user_content) > 120 else "")

    names = _load_names_cached()
    user_set_name = names.get(path.stem)
    effective_title = user_set_name or custom_title

    result = {
        "id": path.stem,
        "custom_title": effective_title,
        "display_title": effective_title if effective_title else (first_user_content[:60] + ("…" if len(first_user_content) > 60 else "")) or path.stem,
        "date": date_str,
        "last_activity": last_activity_str,
        "last_activity_ts": last_ts.timestamp() if last_ts else 0,
        "sort_ts": first_ts.timestamp() if first_ts else 0,
        "file_bytes": st.st_size,
        "size": _format_size(st.st_size),
        "preview": preview,
        "message_count": message_count,
    }
    _summary_cache[cache_key] = result
    return result


def load_session(path: Path) -> dict:
    """Parse a .jsonl session file and return a summary dict."""
    messages = []
    custom_title = None
    first_ts = None

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue   # skip partial/corrupt lines (e.g. mid-write)
                t = obj.get("type", "")

                if t == "custom-title":
                    custom_title = obj.get("customTitle", "")

                elif t in ("user", "assistant"):
                    role = t
                    content = ""
                    msg = obj.get("message", {})
                    raw = msg.get("content", "")
                    if isinstance(raw, str):
                        content = raw
                    elif isinstance(raw, list):
                        parts = []
                        for block in raw:
                            if isinstance(block, dict) and block.get("type") == "text":
                                parts.append(block.get("text", ""))
                        content = " ".join(parts)

                    ts_str = obj.get("timestamp", "")
                    ts = None
                    if ts_str:
                        try:
                            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        except Exception:
                            pass

                    if ts and first_ts is None:
                        first_ts = ts

                    messages.append({"role": role, "content": content.strip(), "ts": ts_str})

    except Exception as e:
        return {"id": path.stem, "error": str(e), "messages": [], "custom_title": None,
                "display_title": path.stem, "date": "", "last_activity": "", "preview": "",
                "last_activity_ts": 0, "sort_ts": 0, "size": "0 B", "file_bytes": 0, "message_count": 0}

    # Date: prefer first message timestamp, fall back to file mtime
    if first_ts:
        date_str = first_ts.strftime("%b %d, %Y  %I:%M %p")
    else:
        mtime = path.stat().st_mtime
        date_str = datetime.fromtimestamp(mtime).strftime("%b %d, %Y  %I:%M %p")
        first_ts = datetime.fromtimestamp(mtime, tz=timezone.utc)

    first_user = next((m["content"] for m in messages if m["role"] == "user" and m["content"]), "")
    preview = first_user[:120] + ("…" if len(first_user) > 120 else "")

    # Last activity: latest message timestamp or file mtime
    last_ts = None
    for m in reversed(messages):
        ts_str = m.get("ts", "")
        if ts_str:
            try:
                last_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                break
            except Exception:
                pass
    if last_ts is None:
        last_ts = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    last_activity_str = last_ts.strftime("%b %d, %Y  %I:%M %p")

    # File size (bytes of the .jsonl file only)
    file_bytes = path.stat().st_size
    if file_bytes < 1024:
        size_str = f"{file_bytes} B"
    elif file_bytes < 1024 * 1024:
        size_str = f"{file_bytes / 1024:.1f} KB"
    else:
        size_str = f"{file_bytes / (1024*1024):.1f} MB"

    # User-set names in _session_names.json always win over anything in the .jsonl
    user_set_name = _load_names().get(path.stem)
    effective_title = user_set_name or custom_title

    return {
        "id": path.stem,
        "custom_title": effective_title,
        "display_title": effective_title if effective_title else (first_user[:60] + ("…" if len(first_user) > 60 else "")) or path.stem,
        "date": date_str,
        "last_activity": last_activity_str,
        "last_activity_ts": last_ts.timestamp() if last_ts else 0,
        "sort_ts": first_ts.timestamp() if first_ts else 0,
        "file_bytes": file_bytes,
        "size": size_str,
        "preview": preview,
        "message_count": len(messages),
        "messages": messages,
    }


def all_sessions(summary_only: bool = False) -> list:
    files = list(_sessions_dir().glob("*.jsonl"))
    loader = load_session_summary if summary_only else load_session
    if summary_only and len(files) > 10:
        with ThreadPoolExecutor(max_workers=min(16, len(files))) as pool:
            sessions = list(pool.map(loader, files))
    else:
        sessions = [loader(f) for f in files]
    sessions.sort(key=lambda x: x["sort_ts"], reverse=True)
    return sessions


# ---------------------------------------------------------------------------
# Code extraction utility
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
    """Return similarity ratio between two strings (0.0–1.0)."""
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


# ---------------------------------------------------------------------------
# Smart title generation
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
    """Strip system tags and normalise whitespace."""
    text = re.sub(r"<[^>]{1,60}>.*?</[^>]{1,60}>", " ", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]{1,60}/?>", " ", text)
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
        text = text[:max_chars].rsplit(" ", 1)[0].rstrip(".,;:!?") + "…"
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
    # Remove it if it dominates by >1.4× AND sits past 60% of the session.
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
    if len(title.rstrip("…")) < 30 and len(scored) > 1:
        runner = _to_title(scored[1][2], max_chars=35)
        if runner and runner.lower() not in title.lower():
            title = title.rstrip("…") + " — " + runner

    return title or "Untitled Session"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/projects")
def api_projects():
    docs = str(Path.home() / "Documents").replace("\\", "/").lower()
    project_names = _load_project_names()
    results = []
    for d in sorted(_CLAUDE_PROJECTS.iterdir()):
        if not d.is_dir() or d.name.startswith("subagents"):
            continue
        display = _decode_project(d.name)
        # Only show projects that live inside the user's Documents folder
        if not display.replace("\\", "/").lower().startswith(docs + "/"):
            continue
        count = sum(1 for _ in d.glob("*.jsonl"))
        results.append({
            "encoded": d.name,
            "display": display,
            "custom_name": project_names.get(d.name, ""),
            "session_count": count,
            "active": d.name == _active_project,
        })
    return jsonify(results)


@app.route("/api/set-project", methods=["POST"])
def api_set_project():
    global _active_project
    encoded = (request.get_json() or {}).get("project", "").strip()
    target = _CLAUDE_PROJECTS / encoded
    if not target.is_dir():
        return jsonify({"error": "Not found"}), 404
    _active_project = encoded
    return jsonify({"ok": True, "project": encoded, "display": _decode_project(encoded)})


# ---- Project display-name store (separate from session names) ----
_PROJECT_NAMES_FILE = _CLAUDE_PROJECTS / "_project_names.json"

def _load_project_names() -> dict:
    try:
        return json.loads(_PROJECT_NAMES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _save_project_names(names: dict):
    _PROJECT_NAMES_FILE.write_text(json.dumps(names, indent=2), encoding="utf-8")


@app.route("/api/rename-project", methods=["POST"])
def api_rename_project():
    data = request.get_json() or {}
    encoded = data.get("encoded", "").strip()
    name = data.get("name", "").strip()
    if not encoded:
        return jsonify({"ok": False, "error": "Missing project"}), 400
    names = _load_project_names()
    if name:
        names[encoded] = name
    else:
        names.pop(encoded, None)
    _save_project_names(names)
    return jsonify({"ok": True})


@app.route("/api/delete-project", methods=["POST"])
def api_delete_project():
    global _active_project
    data = request.get_json() or {}
    encoded = data.get("encoded", "").strip()
    target = _CLAUDE_PROJECTS / encoded
    if not target.is_dir():
        return jsonify({"ok": False, "error": "Project not found"}), 404
    try:
        shutil.rmtree(target)
    except Exception as e:
        return jsonify({"ok": False, "error": "Could not delete: " + str(e)}), 500
    # If we deleted the active project, reset
    if _active_project == encoded:
        _active_project = ""
    # Clean up project name
    names = _load_project_names()
    names.pop(encoded, None)
    _save_project_names(names)
    return jsonify({"ok": True})


@app.route("/api/add-project", methods=["POST"])
def api_add_project():
    """Add a project via browse (folder picker), path, or create new."""
    data = request.get_json() or {}
    mode = data.get("mode", "browse")

    if mode == "browse":
        ps_script = r'''
Add-Type -AssemblyName System.Windows.Forms
$fb = New-Object System.Windows.Forms.FolderBrowserDialog
$fb.Description = "Select a project folder"
$fb.RootFolder = [System.Environment+SpecialFolder]::MyComputer
$fb.ShowNewFolderButton = $true
$result = $fb.ShowDialog()
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
    Write-Output $fb.SelectedPath
} else {
    Write-Output "::CANCELLED::"
}
'''
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True, text=True, timeout=120)
            chosen = r.stdout.strip()
            if not chosen or chosen == "::CANCELLED::":
                return jsonify({"ok": False, "cancelled": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        path = chosen

    elif mode == "path":
        path = data.get("path", "").strip()
        if not path or not Path(path).is_dir():
            return jsonify({"ok": False, "error": "Invalid path"}), 400

    elif mode == "create":
        name = data.get("name", "").strip()
        if not name:
            return jsonify({"ok": False, "error": "No name provided"}), 400
        path = str(Path.home() / "Documents" / name)
        Path(path).mkdir(parents=True, exist_ok=True)

    else:
        return jsonify({"ok": False, "error": "Unknown mode"}), 400

    encoded = path.replace("\\", "/").replace(":", "-").replace("/", "-")
    target = _CLAUDE_PROJECTS / encoded
    target.mkdir(parents=True, exist_ok=True)
    return jsonify({"ok": True, "encoded": encoded, "path": path})


@app.route("/api/find-projects")
def api_find_projects():
    """Scan common directories for code projects not yet registered."""
    existing = {d.name for d in _CLAUDE_PROJECTS.iterdir() if d.is_dir()}
    indicators = [".git", "package.json", "Cargo.toml", "go.mod", "pyproject.toml",
                  "requirements.txt", "pom.xml", "build.gradle", "Makefile",
                  ".sln", ".csproj", "CMakeLists.txt", "Gemfile", "composer.json"]
    scan_roots = [
        Path.home() / "Documents",
        Path.home() / "Desktop",
        Path.home() / "source" / "repos",  # Visual Studio default
    ]
    found = []
    seen_paths = set()
    for root in scan_roots:
        if not root.is_dir():
            continue
        try:
            for child in sorted(root.iterdir()):
                if not child.is_dir() or child.name.startswith("."):
                    continue
                encoded = str(child).replace("\\", "/").replace(":", "-").replace("/", "-")
                if encoded in existing or str(child) in seen_paths:
                    continue
                # Check for code project indicators
                detected = []
                for ind in indicators:
                    if (child / ind).exists():
                        detected.append(ind)
                if detected:
                    proj_type = detected[0].replace(".", "").replace("_", " ").title()
                    if ".git" in detected:
                        proj_type = "Git repo"
                    found.append({
                        "path": str(child),
                        "name": child.name,
                        "encoded": encoded,
                        "type": proj_type,
                        "indicators": detected,
                    })
                    seen_paths.add(str(child))
        except PermissionError:
            continue
    return jsonify({"projects": found})


_git_cache = {"ahead": 0, "behind": 0, "uncommitted": False, "has_git": False, "ready": False}
_git_fetch_lock = threading.Lock()

@app.route("/api/project-chat", methods=["POST"])
def api_project_chat():
    """AI-assisted project finder. Searches filesystem based on user description."""
    data = request.get_json() or {}
    user_msg = data.get("message", "").strip().lower()
    if not user_msg:
        return jsonify({"content": "Tell me what kind of project you're looking for.", "suggestions": []})

    # Search for projects matching the description
    search_roots = [
        Path.home() / "Documents",
        Path.home() / "Desktop",
        Path.home() / "source" / "repos",
        Path.home(),
    ]
    indicators = {
        ".git": "Git",
        "package.json": "Node.js",
        "pyproject.toml": "Python",
        "requirements.txt": "Python",
        "Cargo.toml": "Rust",
        "go.mod": "Go",
        "pom.xml": "Java/Maven",
        "build.gradle": "Java/Gradle",
        ".sln": ".NET",
        "Gemfile": "Ruby",
        "composer.json": "PHP",
        "CMakeLists.txt": "C/C++",
    }
    # Keywords to match against directory names
    keywords = [w for w in re.split(r'\W+', user_msg) if len(w) > 2]

    existing = {d.name for d in _CLAUDE_PROJECTS.iterdir() if d.is_dir()}
    matches = []
    max_depth = 2

    def _scan(root, depth=0):
        if depth > max_depth or len(matches) >= 15:
            return
        try:
            for child in sorted(root.iterdir()):
                if not child.is_dir() or child.name.startswith(".") or child.name in ("node_modules", "__pycache__", ".git", "venv", ".venv"):
                    continue
                name_lower = child.name.lower()
                # Check if directory name matches any keyword
                name_match = any(kw in name_lower for kw in keywords)
                # Check for project indicators
                detected = [ind for ind, label in indicators.items() if (child / ind).exists()]
                encoded = str(child).replace("\\", "/").replace(":", "-").replace("/", "-")

                if (name_match or detected) and encoded not in existing:
                    tech = ", ".join(indicators[d] for d in detected if d in indicators) or "Folder"
                    score = (2 if name_match else 0) + len(detected)
                    matches.append({"path": str(child), "name": child.name, "type": tech, "encoded": encoded, "score": score})

                if depth < max_depth:
                    _scan(child, depth + 1)
        except PermissionError:
            pass

    for root in search_roots:
        if root.is_dir():
            _scan(root)

    matches.sort(key=lambda x: x["score"], reverse=True)
    matches = matches[:10]

    if matches:
        lines = ["I found these projects that might match:\n"]
        for i, m in enumerate(matches):
            lines.append(f"**{m['name']}** ({m['type']})")
            lines.append(f"`{m['path']}`\n")
        content = "\n".join(lines) + "\nClick a suggestion below to add one, or describe more specifically what you're looking for."
        suggestions = [m["name"] + " — Add" for m in matches[:5]]
    else:
        content = "I couldn't find any projects matching that description. Try different keywords, or use **Browse** to pick a folder manually."
        suggestions = ["Browse for folder"]

    return jsonify({
        "content": content,
        "suggestions": suggestions,
        "matches": matches,
    })


def _bg_git_fetch():
    """Run git fetch + status in background, update cache when done."""
    proj = _CLAUDECODEGUI_DIR
    if not (proj / ".git").is_dir():
        _git_cache.update({"has_git": False, "ready": True})
        return
    try:
        subprocess.run(["git", "-C", str(proj), "fetch", "--quiet"],
                       capture_output=True, timeout=15)
    except Exception:
        pass
    ahead = behind = 0
    try:
        r = subprocess.run(
            ["git", "-C", str(proj), "rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            parts = r.stdout.strip().split()
            if len(parts) == 2:
                ahead, behind = int(parts[0]), int(parts[1])
    except Exception:
        pass
    uncommitted = False
    try:
        dirty = subprocess.run(["git", "-C", str(proj), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=5)
        uncommitted = bool(dirty.stdout.strip())
    except Exception:
        pass
    _git_cache.update({"has_git": True, "ahead": ahead, "behind": behind,
                       "uncommitted": uncommitted, "ready": True})

# Kick off first fetch immediately at startup
threading.Thread(target=_bg_git_fetch, daemon=True).start()

@app.route("/api/git-status")
def api_git_status():
    # Return cached result instantly; trigger a refresh in background
    if not _git_fetch_lock.locked():
        def _refresh():
            with _git_fetch_lock:
                _bg_git_fetch()
        threading.Thread(target=_refresh, daemon=True).start()
    return jsonify(_git_cache)


@app.route("/api/git-sync", methods=["POST"])
def api_git_sync():
    proj = _CLAUDECODEGUI_DIR
    if not (proj / ".git").is_dir():
        return jsonify({"ok": False, "messages": ["ClaudeCodeGUI has no git repo."]})
    action = (request.get_json() or {}).get("action", "both")
    messages = []
    ok = True

    if action in ("pull", "both"):
        stash = subprocess.run(["git", "-C", str(proj), "stash", "--include-untracked"],
                               capture_output=True, text=True, timeout=15)
        stashed = "No local changes" not in stash.stdout
        pull = subprocess.run(
            ["git", "-C", str(proj), "pull", "--rebase", "-X", "theirs"],
            capture_output=True, text=True, timeout=30)
        if pull.returncode != 0:
            subprocess.run(["git", "-C", str(proj), "rebase", "--abort"], capture_output=True)
            pull2 = subprocess.run(["git", "-C", str(proj), "pull", "-X", "theirs"],
                                   capture_output=True, text=True, timeout=30)
            if pull2.returncode != 0:
                ok = False
                messages.append("Could not pull: " + pull2.stderr.strip())
                return jsonify({"ok": ok, "messages": messages})
            out = pull2.stdout.strip()
        else:
            out = pull.stdout.strip()
        if stashed:
            subprocess.run(["git", "-C", str(proj), "stash", "pop"], capture_output=True)
        if "Already up to date" in out:
            messages.append("Claude Code GUI is already up to date.")
        else:
            messages.append("Pulled latest Claude Code GUI updates from remote.")

    if action in ("push", "both") and ok:
        # Auto-commit any uncommitted changes before pushing
        dirty = subprocess.run(["git", "-C", str(proj), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=5)
        if dirty.stdout.strip():
            from datetime import datetime as _dt
            subprocess.run(["git", "-C", str(proj), "add", "-A"], capture_output=True)
            msg = "Update Claude Code GUI " + _dt.now().strftime("%Y-%m-%d %H:%M")
            subprocess.run(["git", "-C", str(proj), "commit", "-m", msg],
                           capture_output=True, text=True, timeout=10)
            messages.append("Saved your local changes as a new version.")
        push = subprocess.run(["git", "-C", str(proj), "push"],
                               capture_output=True, text=True, timeout=30)
        if push.returncode != 0:
            ok = False
            messages.append("Could not push: " + (push.stderr.strip() or push.stdout.strip()))
        else:
            messages.append("Your Claude Code GUI changes have been pushed to remote.")

    # Update git cache immediately so the next pollGitStatus gets fresh data
    try:
        r = subprocess.run(
            ["git", "-C", str(proj), "rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
            capture_output=True, text=True, timeout=5)
        a = b = 0
        if r.returncode == 0:
            parts = r.stdout.strip().split()
            if len(parts) == 2:
                a, b = int(parts[0]), int(parts[1])
        d = subprocess.run(["git", "-C", str(proj), "status", "--porcelain"],
                           capture_output=True, text=True, timeout=5)
        _git_cache.update({"has_git": True, "ahead": a, "behind": b,
                           "uncommitted": bool(d.stdout.strip()), "ready": True})
    except Exception:
        pass

    return jsonify({"ok": ok, "messages": messages})


@app.route("/api/sessions")
def api_sessions():
    return jsonify(all_sessions(summary_only=True))


@app.route("/api/session/<session_id>")
def api_session(session_id):
    path = _sessions_dir() / f"{session_id}.jsonl"
    if not path.exists():
        return jsonify({"error": "Not found"}), 404
    return jsonify(load_session(path))


@app.route("/api/rename/<session_id>", methods=["POST"])
def api_rename(session_id):
    path = _sessions_dir() / f"{session_id}.jsonl"
    if not path.exists():
        return jsonify({"error": "Not found"}), 404

    data = request.json
    new_title = data.get("title", "").strip()
    if not new_title:
        return jsonify({"error": "Title cannot be empty"}), 400

    # Save to the persistent names store — this survives Claude Code's own auto-naming
    _save_name(session_id, new_title)

    # Also write to the .jsonl so Claude Code's own UI sees the name
    entry = json.dumps({"type": "custom-title", "customTitle": new_title, "sessionId": session_id})
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n" + entry + "\n")

    return jsonify({"ok": True, "title": new_title})


@app.route("/api/autonname/<session_id>", methods=["POST"])
def api_autoname(session_id):
    path = _sessions_dir() / f"{session_id}.jsonl"
    if not path.exists():
        return jsonify({"error": "Not found"}), 404

    # Never override a name the user manually set
    existing = _load_names().get(session_id)
    if existing:
        return jsonify({"ok": True, "title": existing, "skipped": True,
                        "reason": "User-set name preserved"})

    session = load_session(path)
    messages = [m for m in session["messages"] if m["content"]]

    if not messages:
        all_s = all_sessions(summary_only=True)
        empty_count = sum(
            1 for s in all_s
            if (s.get("custom_title") or "").startswith("Empty Session")
            or (not s.get("custom_title") and s.get("message_count", 0) == 0)
        )
        title = f"Empty Session ({empty_count})"
        entry = json.dumps({"type": "custom-title", "customTitle": title, "sessionId": session_id})
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n" + entry + "\n")
        return jsonify({"ok": True, "title": title})

    try:
        title = smart_title(session["messages"])

        entry = json.dumps({"type": "custom-title", "customTitle": title, "sessionId": session_id})
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n" + entry + "\n")

        return jsonify({"ok": True, "title": title})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/delete-empty", methods=["DELETE"])
def api_delete_empty():
    deleted = []
    for f in _sessions_dir().glob("*.jsonl"):
        s = load_session(f)
        if s.get("message_count", 0) == 0:
            folder = _sessions_dir() / f.stem
            f.unlink()
            if folder.exists() and folder.is_dir():
                shutil.rmtree(folder)
            deleted.append(f.stem)
    return jsonify({"ok": True, "deleted": len(deleted)})


@app.route("/api/summary/<session_id>")
def api_summary(session_id):
    path = _sessions_dir() / f"{session_id}.jsonl"
    if not path.exists():
        return jsonify({"error": "Not found"}), 404

    session = load_session(path)
    messages = session.get("messages", [])
    user_msgs  = [m for m in messages if m.get("role") == "user"      and m.get("content","").strip()]
    asst_msgs  = [m for m in messages if m.get("role") == "assistant"  and m.get("content","").strip()]

    if not user_msgs:
        return jsonify({"html": "<p>No content in this session.</p>"})

    topic = smart_title(messages)

    stop_words = {"i","the","a","an","it","is","are","was","were","to","do","can","could",
                  "would","should","please","me","my","we","our","you","this","that","have",
                  "has","had","be","will","just","so","and","or","but","if","at","in","on",
                  "of","for","with","about","from","make","let","now","there","here","then",
                  "also","get","use","into","by","up","out","its","not","no","yes","ok","add",
                  "when","how","what","why","where","which","who","still","same","each","some"}

    def _make_label(text):
        """2-3 word topic label from most meaningful words in the message."""
        text = _STRIP_PREFIXES.sub("", text).strip()
        words = [w.strip(".,!?\"'()[]{}") for w in text.split()]
        # Prefer words >3 chars that aren't stop words (more likely to be nouns/topics)
        content = [w for w in words if w.lower() not in stop_words and len(w) > 3]
        label_words = content[:3] or words[:3]
        label = " ".join(label_words)
        return label[:1].upper() + label[1:] if label else ""

    def _make_desc(user_text):
        """Use the user's own words — naturally the right level of abstraction."""
        clean = _clean_message(user_text).strip()
        if not clean:
            return ""
        clean = clean[:1].upper() + clean[1:]
        if len(clean) <= 130:
            return clean
        return clean[:127].rsplit(" ", 1)[0] + "…"

    # Collect all meaningful user messages (preserving conversation order)
    meaningful = []
    for m in messages:
        if m.get("role") != "user":
            continue
        text = _clean_message(m.get("content", ""))
        if text and not _is_trivial(text):
            meaningful.append(text)

    bullets = []
    seen_labels = set()

    if meaningful:
        # Divide into 5 sections; pick highest-scoring message from each
        num_sections = min(5, len(meaningful))
        sz = len(meaningful) / num_sections
        for sec in range(num_sections):
            section = meaningful[int(sec * sz):int((sec + 1) * sz)]
            if not section:
                continue
            best = max(section, key=_score)
            label = _make_label(best)
            desc  = _make_desc(best)
            if not label or not desc:
                continue
            key = label.lower()[:12]
            if key in seen_labels:
                continue
            seen_labels.add(key)
            bullets.append(f"<li><strong>{label}:</strong> {desc}</li>")

    # Overview = topic title + first substantive user message as 1-2 line paragraph
    overview_parts = []
    for t in meaningful[:2]:
        if len(t) > 20:
            clean = t[:1].upper() + t[1:]
            if len(clean) > 180:
                clean = clean[:177].rsplit(" ", 1)[0] + "…"
            overview_parts.append(clean)
            if len(" ".join(overview_parts)) > 200:
                break
    overview_text = " — ".join(overview_parts) if overview_parts else ""

    # Recent focus = last 3 meaningful user requests
    recent_items = "".join(
        f"<li>{t[:1].upper()}{t[1:130]}{'…' if len(t)>130 else ''}</li>"
        for t in meaningful[-3:]
    )
    recent_html = (f'<div class="sum-section"><div class="sum-label">Recent focus</div>'
                   f'<ul>{recent_items}</ul></div>') if recent_items else ""

    stats = (f"{len(user_msgs)} messages &nbsp;·&nbsp; {session['size']} &nbsp;·&nbsp; "
             f"Last active: {session['last_activity']}")

    bullets_html = "".join(bullets) if bullets else "<li>—</li>"

    html = f"""
<div class="sum-topic">{topic}</div>
<div class="sum-stats">{stats}</div>
{"<div class='sum-section'><div class='sum-label'>Overview</div><p style='font-size:13px;color:#ccc;line-height:1.6'>" + overview_text + "</p></div>" if overview_text else ""}
<div class="sum-section">
  <div class="sum-label">Key topics covered</div>
  <ul>{bullets_html}</ul>
</div>
{recent_html}
"""
    return jsonify({"html": html})


@app.route("/api/open/<session_id>", methods=["POST"])
def api_open(session_id):
    path = _sessions_dir() / f"{session_id}.jsonl"
    if not path.exists():
        return jsonify({"error": "Not found"}), 404
    try:
        proj_dir = _decode_project(_active_project) if _active_project else str(Path.home())
        subprocess.Popen(
            f'start cmd /k "cd /d {proj_dir} && claude --resume {session_id}"',
            shell=True
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/duplicate/<session_id>", methods=["POST"])
def api_duplicate(session_id):
    import uuid as uuid_mod
    src = _sessions_dir() / f"{session_id}.jsonl"
    if not src.exists():
        return jsonify({"error": "Not found"}), 404

    new_id = str(uuid_mod.uuid4())
    dst = _sessions_dir() / f"{new_id}.jsonl"

    # Copy file, rewriting sessionId in every line
    lines_out = []
    with open(src, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if "sessionId" in obj:
                obj["sessionId"] = new_id
            lines_out.append(json.dumps(obj))

    with open(dst, "w", encoding="utf-8") as f:
        f.write("\n".join(lines_out) + "\n")

    return jsonify({"ok": True, "new_id": new_id})


@app.route("/api/continue/<session_id>", methods=["POST"])
def api_continue(session_id):
    import uuid as uuid_mod
    from datetime import datetime, timezone as tz

    src = _sessions_dir() / f"{session_id}.jsonl"
    if not src.exists():
        return jsonify({"error": "Not found"}), 404

    session = load_session(src)
    messages = session.get("messages", [])

    # Build context: topic from smart_title, last 6 exchanges for recent state
    topic = smart_title(messages)
    user_msgs = [m for m in messages if m.get("role") == "user" and m.get("content")]
    asst_msgs = [m for m in messages if m.get("role") == "assistant" and m.get("content")]

    # Recent exchanges (last 3 user + last 3 assistant, interleaved)
    recent = messages[-12:] if len(messages) > 12 else messages
    recent_text = "\n".join(
        f"{'User' if m['role']=='user' else 'Claude'}: {m['content'][:300]}"
        for m in recent if m.get("content")
    )

    # Key facts from early in the session (first 3 user messages)
    early_context = "\n".join(
        f"- {m['content'][:200]}"
        for m in user_msgs[:3]
    )

    handoff = (
        f"This is a continuation of a previous session that got too long.\n\n"
        f"**What we were working on:** {topic}\n\n"
        f"**Key context from the start of that session:**\n{early_context}\n\n"
        f"**Most recent exchanges:**\n{recent_text}\n\n"
        f"Please pick up right where we left off. "
        f"You have full context above — continue helping me with this work."
    )

    new_id = str(uuid_mod.uuid4())
    now = datetime.now(tz.utc).isoformat().replace("+00:00", "Z")
    msg_uuid = str(uuid_mod.uuid4())

    snapshot = {"type": "file-history-snapshot", "messageId": msg_uuid,
                "snapshot": {"messageId": msg_uuid, "trackedFileBackups": {}, "timestamp": now},
                "isSnapshotUpdate": False}
    user_entry = {"parentUuid": None, "isSidechain": False, "userType": "external",
                  "cwd": _decode_project(_active_project).replace("/", "\\"),
                  "sessionId": new_id, "version": "2.1.71", "gitBranch": "main",
                  "type": "user", "message": {"role": "user", "content": handoff},
                  "uuid": msg_uuid, "timestamp": now}
    title_entry = {"type": "custom-title", "customTitle": f"[cont] {topic[:55]}", "sessionId": new_id}

    dst = _sessions_dir() / f"{new_id}.jsonl"
    with open(dst, "w", encoding="utf-8") as f:
        f.write(json.dumps(snapshot) + "\n")
        f.write(json.dumps(user_entry) + "\n")
        f.write(json.dumps(title_entry) + "\n")

    return jsonify({"ok": True, "new_id": new_id, "title": f"[cont] {topic[:55]}"})


@app.route("/api/delete/<session_id>", methods=["DELETE"])
def api_delete(session_id):
    path = _sessions_dir() / f"{session_id}.jsonl"
    folder = _sessions_dir() / session_id

    if not path.exists():
        return jsonify({"error": "Not found"}), 404

    path.unlink()
    if folder.exists() and folder.is_dir():
        shutil.rmtree(folder)

    return jsonify({"ok": True})


@app.route("/api/extract-code/<session_id>")
def api_extract_code(session_id):
    path = _sessions_dir() / f"{session_id}.jsonl"
    if not path.exists():
        return jsonify({"error": "Not found"}), 404
    blocks = _extract_code_blocks(path)
    languages = sorted(set(b["language"] for b in blocks if b["language"]))
    return jsonify({"blocks": blocks, "count": len(blocks), "languages": languages})


@app.route("/api/export-project/<session_id>")
def api_export_project(session_id):
    path = _sessions_dir() / f"{session_id}.jsonl"
    if not path.exists():
        return jsonify({"error": "Not found"}), 404

    session = load_session(path)
    blocks = _extract_code_blocks(path)
    # Skip duplicates
    non_dup = [b for b in blocks if b["duplicate_of"] is None]
    if not non_dup:
        return jsonify({"error": "No code blocks found"})

    # Build zip in memory
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        used_names: dict[str, int] = {}
        file_entries = []
        for b in non_dup:
            base = b["inferred_filename"] or f"block_{len(file_entries)+1}.txt"
            if base not in used_names:
                used_names[base] = 1
                fname = base
            else:
                used_names[base] += 1
                parts = base.rsplit(".", 1)
                if len(parts) == 2:
                    fname = f"{parts[0]}_{used_names[base]}.{parts[1]}"
                else:
                    fname = f"{base}_{used_names[base]}"
            zf.writestr(fname, b["content"])
            file_entries.append((fname, b["language"], b["msg_index"]))

        # Build README
        title = session.get("display_title", session_id)
        last_activity = session.get("last_activity", "")
        file_list = "\n".join(
            f"- {fname} — {lang or 'code'} (message {mi + 1})"
            for fname, lang, mi in file_entries
        )
        readme = (
            f"# Claude Session Export\n\n"
            f"Exported from session: {title}\n"
            f"Date: {last_activity}\n\n"
            f"## Files\n{file_list}\n"
        )
        zf.writestr("README.md", readme)

    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name="session_export.zip"
    )


@app.route("/api/compare/<id1>/<id2>")
def api_compare(id1, id2):
    path1 = _sessions_dir() / f"{id1}.jsonl"
    path2 = _sessions_dir() / f"{id2}.jsonl"
    if not path1.exists():
        return jsonify({"error": f"Session {id1} not found"}), 404
    if not path2.exists():
        return jsonify({"error": f"Session {id2} not found"}), 404

    s1 = load_session(path1)
    s2 = load_session(path2)
    blocks1 = _extract_code_blocks(path1)
    blocks2 = _extract_code_blocks(path2)

    def _session_meta(s):
        return {
            "title": s.get("display_title", ""),
            "date": s.get("last_activity", ""),
            "size": s.get("size", ""),
            "message_count": s.get("message_count", 0),
        }

    # Build lookup by inferred_filename for each session
    def _build_lookup(blocks):
        d = {}
        for b in blocks:
            key = b.get("inferred_filename") or b.get("language") or "unknown"
            if key not in d:
                d[key] = b
        return d

    lookup1 = _build_lookup(blocks1)
    lookup2 = _build_lookup(blocks2)

    all_keys = sorted(set(list(lookup1.keys()) + list(lookup2.keys())))
    code_diff = []
    added = removed = changed = same_count = 0

    for key in all_keys:
        b1 = lookup1.get(key)
        b2 = lookup2.get(key)
        c1 = b1["content"] if b1 else ""
        c2 = b2["content"] if b2 else ""
        lang = (b1 or b2).get("language", "")

        if b1 and not b2:
            status = "removed"
            removed += 1
        elif b2 and not b1:
            status = "added"
            added += 1
        else:
            ratio = _block_similarity(c1, c2)
            if ratio > 0.98:
                status = "same"
                same_count += 1
            else:
                status = "changed"
                changed += 1

        code_diff.append({
            "filename": key,
            "language": lang,
            "status": status,
            "content1": c1,
            "content2": c2,
        })

    return jsonify({
        "session1": _session_meta(s1),
        "session2": _session_meta(s2),
        "code_diff": code_diff,
        "stats": {
            "s1_blocks": len(blocks1),
            "s2_blocks": len(blocks2),
            "added": added,
            "removed": removed,
            "changed": changed,
        },
    })


@app.route("/api/session-log/<session_id>")
def api_session_log(session_id):
    """Return structured log entries for the live terminal panel."""
    path = _sessions_dir() / f"{session_id}.jsonl"
    if not path.exists():
        return jsonify({"error": "Not found"}), 404
    since = int(request.args.get("since", 0))
    try:
        raw_lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    except Exception:
        return jsonify({"entries": [], "total_lines": 0})
    total = len(raw_lines)
    entries = []
    for raw in raw_lines[since:]:
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        t = obj.get("type", "")
        if t in ("file-history-snapshot", "custom-title", "progress"):
            continue
        if t == "user":
            msg = obj.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                entries.append({"kind": "user", "text": content.strip()[:2000]})
            elif isinstance(content, list):
                for block in content:
                    bt = block.get("type", "")
                    if bt == "text" and block.get("text", "").strip():
                        entries.append({"kind": "user", "text": block["text"].strip()[:2000]})
                    elif bt == "tool_result":
                        rc = block.get("content", "")
                        if isinstance(rc, list):
                            rt = " ".join(b.get("text", "") for b in rc if isinstance(b, dict) and b.get("type") == "text")
                        else:
                            rt = str(rc)
                        entries.append({
                            "kind": "tool_result",
                            "tool_use_id": block.get("tool_use_id", ""),
                            "text": rt[:600],
                            "is_error": bool(block.get("is_error"))
                        })
        elif t == "assistant":
            msg = obj.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                entries.append({"kind": "asst", "text": content.strip()[:3000]})
            elif isinstance(content, list):
                for block in content:
                    bt = block.get("type", "")
                    if bt == "text" and block.get("text", "").strip():
                        entries.append({"kind": "asst", "text": block["text"].strip()[:3000]})
                    elif bt == "tool_use":
                        inp = block.get("input") or {}
                        if "command" in inp:
                            desc = inp["command"][:300]
                        elif "path" in inp:
                            desc = inp["path"]
                            if "content" in inp:
                                desc += f" (write {len(str(inp.get('content','')))} chars)"
                        elif "pattern" in inp:
                            desc = inp["pattern"][:200]
                        elif inp:
                            first_key = next(iter(inp))
                            desc = f"{first_key}: {str(inp[first_key])[:200]}"
                        else:
                            desc = ""
                        entries.append({
                            "kind": "tool_use",
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "desc": desc
                        })
    return jsonify({"entries": entries, "total_lines": total})


@app.route("/api/close/<session_id>", methods=["POST"])
def api_close_session(session_id):
    """Terminate the running Claude process and its parent cmd window."""
    running = _get_running_session_ids()
    pid = running.get(session_id)
    if not pid:
        return jsonify({"ok": False, "error": "Session not running"})
    try:
        # Get parent PID before killing (wmic query while process still exists)
        parent_pid = None
        try:
            r = subprocess.run(
                ["wmic", "process", "where", f"ProcessId={pid}", "get", "ParentProcessId"],
                capture_output=True, text=True, timeout=5)
            lines = [l.strip() for l in r.stdout.strip().splitlines() if l.strip() and not l.strip().startswith("Parent")]
            if lines:
                parent_pid = int(lines[0])
        except Exception:
            pass
        # Kill the Claude process
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=5)
        # Kill the parent cmd window if it exists
        if parent_pid:
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(parent_pid)], capture_output=True, timeout=5)
            except Exception:
                pass
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ---------------------------------------------------------------------------
# HTML — now served from templates/index.html + static/js/*.js
# ---------------------------------------------------------------------------
# (The inline HTML string has been removed. See templates/index.html and static/js/*.js)

# ---------------------------------------------------------------------------
# Waiting-for-input detection
# ---------------------------------------------------------------------------

def _get_running_session_ids():
    """Return {session_id: pid} for any claude sessions currently running."""
    import time
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-command",
             "Get-WmiObject Win32_Process | Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=8
        )
        data = json.loads(result.stdout or "[]")
        if isinstance(data, dict):
            data = [data]

        running = {}
        resume_pids = []
        uuid_re = re.compile(r"(?:--resume|-r)\s+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)

        for proc in data:
            cmdline = proc.get("CommandLine") or ""
            name = (proc.get("Name") or "").lower()
            if name not in ("node.exe", "claude.exe", "claude"):
                continue
            m = uuid_re.search(cmdline)
            if m:
                running[m.group(1)] = proc.get("ProcessId")
            elif re.search(r"\b(--resume|resume)\b", cmdline):
                # claude --resume: UUID not in command line — resolve by recent file activity
                resume_pids.append(proc.get("ProcessId"))

        # For --resume processes, match to the most recently active .jsonl not already claimed.
        # No time limit — --resume can resume sessions that have been idle for hours.
        if resume_pids:
            candidates = sorted(
                [(f.stat().st_mtime, f.stem) for f in _sessions_dir().glob("*.jsonl")
                 if f.stem not in running],
                reverse=True
            )
            for pid, (_, sid) in zip(resume_pids, candidates):
                running[sid] = pid

        return running
    except Exception:
        return {}


def _parse_waiting_state(path: Path) -> dict | None:
    """
    Return a dict describing what Claude is waiting on, or None if not waiting.
    Only returns a state when the LAST meaningful message is from the assistant
    (meaning Claude sent something and is now blocked waiting for the user).
    If the last meaningful message is from the user (tool results, etc.),
    Claude is processing — not waiting.
    Dict: {question, options: list|None, kind: 'tool'|'text'}
    """
    import time
    stat = path.stat()
    now = time.time()
    idle_seconds = now - stat.st_mtime

    # If file was written less than 6 seconds ago, Claude is actively running — not waiting
    # 6s gives Claude time to finish generating text before we flag it as a question
    if idle_seconds < 6:
        return None

    try:
        lines = [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    except Exception:
        return None

    last_text = None
    last_tool_name = None
    last_tool_input = None
    last_entry_role = None   # 'user' or 'assistant'

    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        t = obj.get("type", "")
        if t in ("progress", "file-history-snapshot", "custom-title"):
            continue
        if t in ("user", "assistant"):
            last_entry_role = t
            msg = obj.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, list):
                for block in content:
                    bt = block.get("type", "")
                    if bt == "tool_result":
                        # Claude just received tool output and is processing it — not waiting
                        return None
                    if bt == "text":
                        text = (block.get("text") or "").strip()
                        if text:
                            if len(text) > 1400:
                                last_text = "…" + text[-1400:].lstrip()
                            else:
                                last_text = text
                        break
                    elif bt == "tool_use":
                        last_tool_name = block.get("name", "unknown")
                        inp = block.get("input") or {}
                        if "command" in inp:
                            last_tool_input = inp["command"][:200]
                        elif "prompt" in inp:
                            last_tool_input = inp["prompt"][:200]
                        elif "description" in inp:
                            last_tool_input = inp["description"][:200]
                        elif inp:
                            first_val = next(iter(inp.values()), "")
                            last_tool_input = str(first_val)[:200]
                        break
            elif isinstance(content, str) and content.strip():
                text = content.strip()
                if len(text) > 1400:
                    last_text = "…" + text[-1400:].lstrip()
                else:
                    last_text = text
            break
        else:
            break  # unknown entry type — stop scanning

    # Only flag as waiting if the LAST message was from the assistant
    # (Claude said/asked something and is now blocked on user input)
    if last_entry_role != "assistant":
        return None

    def _detect_options(text):
        """Return list of option strings if question has explicit choices."""
        tl = text.lower()
        if re.search(r'\[y/n/a\]|\(y/n/a\)|yes.?no.?all', tl):
            return ["y", "n", "a"]
        if re.search(r'\[y/n\]|\(y/n\)|yes.?or.?no|\[yes/no\]|\(yes/no\)', tl):
            return ["y", "n"]
        if re.search(r'\[yes\]|\[no\]', tl):
            return ["yes", "no"]
        items = re.findall(r'(?:^|\n)\s*(\d+)[.)]\s+(.+?)(?=\n\s*\d+[.)]|\n\n|$)', text, re.MULTILINE)
        if len(items) >= 2:
            return [f"{n}. {v.strip()[:60]}" for n, v in items[:6]]
        return None

    if last_text:
        opts = _detect_options(last_text)
        # Only flag as a question if the text is genuinely interrogative.
        # A plain completion message ("Got it.", "Done!", "I've saved the file.") is
        # Claude finishing a task — it's idle, not asking anything.
        # We require EITHER: an explicit option list, OR a "?" somewhere in the text.
        if opts is None and "?" not in last_text:
            return None
        return {"question": last_text, "options": opts, "kind": "text"}

    if last_tool_name:
        tool_q = f"Allow tool: {last_tool_name}"
        if last_tool_input:
            tool_q += f"\n\n{last_tool_input}"
        return {"question": tool_q, "options": ["y", "n", "a"], "kind": "tool"}

    return None


def _parse_session_kind(path: Path) -> str:
    """
    For a running session that is NOT waiting for user input, return 'working' or 'idle'.
    working = Claude is mid-execution (tool pending, processing results, file recently written)
    idle    = Claude finished responding, ready for next user message
    """
    import time
    file_age = time.time() - path.stat().st_mtime

    # Recent file activity always means working
    if file_age < 10:
        return 'working'

    # If the file hasn't been touched in >30s and a process is running,
    # Claude is sitting idle at the prompt waiting for input — regardless of
    # what the last entry pattern looks like (e.g. resumed sessions, finished tasks)
    if file_age > 30:
        return 'idle'

    try:
        lines = [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    except Exception:
        return 'working'

    skip = {"progress", "file-history-snapshot", "custom-title", "system",
            "debug", "meta", "info", "event"}

    # Collect last few meaningful entries
    entries = []
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type", "") in skip:
            continue
        entries.append(obj)
        if len(entries) >= 5:
            break

    if not entries:
        return 'working'

    last = entries[0]
    t = last.get("type", "")

    if t == "user":
        content = last.get("message", {}).get("content", "")
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_result":
                    return 'working'   # got tool result, Claude processing it
        # Last entry is a user text message — Claude is actively responding
        return 'working'

    if t == "assistant":
        content = last.get("message", {}).get("content", "")
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_use":
                    return 'working'   # Claude fired a tool, awaiting result

        # Last entry is assistant text (no tool_use).
        # Idle only when Claude genuinely finished answering the user:
        #   pattern = user+text → assistant+text (direct Q&A, no tools)
        # Everything else is mid-task:
        #   tool_result before it  → Claude just got results, writing next step
        #   assistant before it    → Claude sent multiple messages in a row (announcing work)
        #   thinking before it     → Claude is still reasoning
        if len(entries) > 1:
            prev = entries[1]
            pt = prev.get("type", "")
            pc = prev.get("message", {}).get("content", "")
            prev_is_user_text = (
                pt == "user" and
                isinstance(pc, list) and
                all(b.get("type") != "tool_result" for b in pc)
            ) or (pt == "user" and isinstance(pc, str))
            if not prev_is_user_text:
                return 'working'

        return 'idle'

    return 'working'


@app.route("/api/waiting")
def api_waiting():
    """Return all running sessions with kind: 'question' | 'working' | 'idle'."""
    running = _get_running_session_ids()
    result = []
    for sid, pid in running.items():
        path = _sessions_dir() / f"{sid}.jsonl"
        if not path.exists():
            continue
        state = _parse_waiting_state(path)
        if state is not None:
            # Normalise kind to 'question' so JS state machine has consistent values
            result.append({"id": sid, "pid": pid,
                           "question": state["question"],
                           "options":  state["options"],
                           "kind":     "question"})
        else:
            kind = _parse_session_kind(path)
            result.append({"id": sid, "pid": pid, "question": None, "options": None, "kind": kind})
    return jsonify(result)


@app.route("/api/respond/<session_id>", methods=["POST"])
def api_respond(session_id):
    """Send text to a waiting Claude session."""
    import tempfile, os as _os, base64 as _b64
    data = request.get_json() or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "No text provided"}), 400

    running = _get_running_session_ids()
    pid = running.get(session_id)

    if pid:
        # SendKeys via PowerShell — works with Windows Terminal (ConPTY) and classic conhost.
        # Briefly brings the target terminal window to the foreground, types the text + Enter,
        # then the user's browser regains focus automatically.
        b64 = _b64.b64encode(text.encode("utf-8")).decode("ascii")
        ps_script = f"""$inputBytes = [System.Convert]::FromBase64String('{b64}')
$inputText  = [System.Text.Encoding]::UTF8.GetString($inputBytes)

function Find-Window([int]$Pid) {{
    $visited = @{{}}
    $cur = $Pid
    while ($cur -gt 4) {{
        if ($visited.ContainsKey($cur)) {{ break }}
        $visited[$cur] = 1
        try {{
            $p = Get-Process -Id $cur -EA Stop
            if ($p.MainWindowHandle -ne [IntPtr]::Zero) {{ return $p }}
        }} catch {{}}
        try {{
            $cur = [int](Get-CimInstance Win32_Process -Filter "ProcessId=$cur" -EA Stop).ParentProcessId
        }} catch {{ break }}
    }}
    return $null
}}

$wp = Find-Window {pid}
if (-not $wp) {{ Write-Error "No window for pid {pid}"; exit 1 }}

Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices;
public class WU {{
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
}}
'@

[WU]::ShowWindow($wp.MainWindowHandle, 9)
[WU]::SetForegroundWindow($wp.MainWindowHandle)
Start-Sleep -Milliseconds 300

Add-Type -AssemblyName System.Windows.Forms
$specialChars = [char[]]@('+','^','%','~','(',')','{{'[0],'}}'[0],'[',']')
foreach ($ch in $inputText.ToCharArray()) {{
    $str = [string]$ch
    if ($specialChars -contains $ch) {{
        [System.Windows.Forms.SendKeys]::SendWait("{{" + $str + "}}")
    }} else {{
        [System.Windows.Forms.SendKeys]::SendWait($str)
    }}
    Start-Sleep -Milliseconds 20
}}
[System.Windows.Forms.SendKeys]::SendWait("{{ENTER}}")
"""
        tmp_ps = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.ps1', delete=False, encoding='utf-8') as f:
                f.write(ps_script); tmp_ps = f.name
            res = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", tmp_ps],
                capture_output=True, timeout=12
            )
            if res.returncode == 0:
                return jsonify({"ok": True, "method": "sent"})
            sk_err = res.stderr.decode("utf-8", errors="ignore").strip()[:300]
            return jsonify({"ok": False, "method": "failed",
                            "rc": res.returncode, "err": sk_err})
        except subprocess.TimeoutExpired:
            return jsonify({"ok": False, "method": "timeout"})
        except Exception as e:
            return jsonify({"ok": False, "method": "error", "err": str(e)})
        finally:
            if tmp_ps:
                try: _os.unlink(tmp_ps)
                except: pass

    # Fallback: clipboard
    clip = text.replace("'", "''")
    subprocess.run(["powershell", "-NoProfile", "-command", f"Set-Clipboard '{clip}'"],
                   capture_output=True, timeout=5)
    return jsonify({"ok": True, "method": "clipboard",
                    "message": "Session not running — copied to clipboard."})


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------

def open_browser():
    import time
    time.sleep(0.8)
    webbrowser.open("http://localhost:5050")


if __name__ == "__main__":
    # Suppress Flask/Werkzeug request logging and startup banner
    import logging
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.ERROR)
    cli = sys.modules.get("flask.cli")
    if cli:
        cli.show_server_banner = lambda *a, **k: None

    print("\n  ClaudeCodeGUI is running.\n"
          "  Open your browser to: http://localhost:5050\n\n"
          "  This is a local server for personal use.\n"
          "  Leave this window open while using ClaudeCodeGUI.\n"
          "  Close it or press Ctrl+C to stop.\n", flush=True)

    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host="0.0.0.0", port=5050, debug=False, threaded=True)
