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
from pathlib import Path
from datetime import datetime, timezone

from flask import Flask, jsonify, request, render_template_string, send_file

_CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
_active_project: str = ""   # encoded dir name; empty = auto-detect
_CLAUDEGUI_DIR = Path(__file__).resolve().parent  # always the ClaudeGUI repo

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

app = Flask(__name__)


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


def all_sessions() -> list:
    sessions = []
    for f in _sessions_dir().glob("*.jsonl"):
        s = load_session(f)
        sessions.append(s)
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
    return render_template_string(HTML)


@app.route("/api/projects")
def api_projects():
    docs = str(Path.home() / "Documents").replace("\\", "/").lower()
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


@app.route("/api/git-status")
def api_git_status():
    proj = _CLAUDEGUI_DIR
    if not (proj / ".git").is_dir():
        return jsonify({"has_git": False})
    subprocess.run(["git", "-C", str(proj), "fetch", "--quiet"],
                   capture_output=True, timeout=12)
    r = subprocess.run(
        ["git", "-C", str(proj), "rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
        capture_output=True, text=True, timeout=5)
    ahead = behind = 0
    if r.returncode == 0:
        parts = r.stdout.strip().split()
        if len(parts) == 2:
            ahead, behind = int(parts[0]), int(parts[1])
    dirty = subprocess.run(["git", "-C", str(proj), "status", "--porcelain"],
                           capture_output=True, text=True, timeout=5)
    uncommitted = bool(dirty.stdout.strip())
    return jsonify({"has_git": True, "ahead": ahead, "behind": behind,
                    "uncommitted": uncommitted})


@app.route("/api/git-sync", methods=["POST"])
def api_git_sync():
    proj = _CLAUDEGUI_DIR
    if not (proj / ".git").is_dir():
        return jsonify({"ok": False, "messages": ["ClaudeGUI has no git repo."]})
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
            messages.append("Claude GUI is already up to date.")
        else:
            messages.append("Pulled latest Claude GUI updates from remote.")

    if action in ("push", "both") and ok:
        # Auto-commit any uncommitted changes before pushing
        dirty = subprocess.run(["git", "-C", str(proj), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=5)
        if dirty.stdout.strip():
            from datetime import datetime as _dt
            subprocess.run(["git", "-C", str(proj), "add", "-A"], capture_output=True)
            msg = "Update Claude GUI " + _dt.now().strftime("%Y-%m-%d %H:%M")
            subprocess.run(["git", "-C", str(proj), "commit", "-m", msg],
                           capture_output=True, text=True, timeout=10)
            messages.append("Saved your local changes as a new version.")
        push = subprocess.run(["git", "-C", str(proj), "push"],
                               capture_output=True, text=True, timeout=30)
        if push.returncode != 0:
            ok = False
            messages.append("Could not push: " + (push.stderr.strip() or push.stdout.strip()))
        else:
            messages.append("Your Claude GUI changes have been pushed to remote.")

    return jsonify({"ok": ok, "messages": messages})


@app.route("/api/sessions")
def api_sessions():
    sessions = all_sessions()
    # Strip messages from list view to keep it light
    for s in sessions:
        s.pop("messages", None)
    return jsonify(sessions)


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
        all_s = all_sessions()
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
# HTML (single-page app)
# ---------------------------------------------------------------------------

HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Code GUI</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #0d0d0d;
    color: #e8e8e8;
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  header {
    background: #161616;
    border-bottom: 1px solid #2a2a2a;
    padding: 14px 20px;
    display: flex;
    align-items: center;
    gap: 10px;
    flex-shrink: 0;
  }
  #project-picker {
    background: #1e1e1e; border: 1px solid #333; border-radius: 6px;
    color: #aaa; font-size: 11px; padding: 4px 8px; cursor: pointer;
    max-width: 200px; flex-shrink: 1;
  }
  #project-picker:focus { outline: none; border-color: #7c7cff; color: #fff; }
  header h1 { font-size: 15px; font-weight: 600; color: #fff; }
  header .sub { font-size: 12px; color: #666; margin-left: 4px; }
  .hdr-spacer { flex: 1; }
  .hdr-sys { position: relative; }
  .hdr-sys-btn {
    background: #1e1e1e; border: 1px solid #333; border-radius: 6px;
    color: #aaa; font-size: 11px; padding: 4px 10px; cursor: pointer;
    white-space: nowrap;
  }
  .hdr-sys-btn:hover { border-color: #555; color: #fff; }
  .hdr-sys-dropdown {
    display: none; position: absolute; top: calc(100% + 4px); left: 0;
    background: #1e1e1e; border: 1px solid #333; border-radius: 8px;
    padding: 4px; min-width: 180px; z-index: 200;
    box-shadow: 0 4px 16px rgba(0,0,0,0.5);
  }
  .hdr-sys-dropdown.open { display: block; }
  .hdr-sys-dropdown button {
    display: block; width: 100%; text-align: left; background: none;
    border: none; color: #ccc; font-size: 12px; padding: 7px 12px;
    cursor: pointer; border-radius: 5px;
  }
  .hdr-sys-dropdown button:hover { background: #2a2a2a; color: #fff; }
  #btn-git-publish, #btn-git-update {
    display: none; background: #1e1e1e; border: 1px solid #333; border-radius: 6px;
    color: #aaa; font-size: 11px; padding: 4px 10px; cursor: pointer;
    white-space: nowrap; align-items: center; gap: 5px;
  }
  #btn-git-publish:hover, #btn-git-update:hover { border-color: #555; color: #fff; }
  #btn-git-publish:disabled, #btn-git-update:disabled { opacity: 0.5; cursor: default; }
  #git-badge-push, #git-badge-pull {
    color: #fff; font-size: 10px; padding: 1px 4px;
    border-radius: 8px; font-weight: 700; line-height: 1.4;
  }
  #git-badge-push { background: #4a6abf; }
  #git-badge-pull { background: #3a8a5a; }

  .layout {
    display: flex;
    flex: 1;
    overflow: hidden;
  }

  /* ---- Sidebar ---- */
  .sidebar {
    width: var(--sidebar-w, 320px);
    min-width: 180px;
    max-width: 600px;
    flex-shrink: 0;
    background: #111;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .resize-handle {
    width: 5px;
    flex-shrink: 0;
    background: #1c1c1c;
    cursor: col-resize;
    transition: background 0.15s;
    position: relative;
    z-index: 10;
  }
  .resize-handle:hover, .resize-handle.dragging { background: #7c7cff; }

  .sidebar-toolbar {
    padding: 8px 12px;
    border-bottom: 1px solid #222;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .sidebar-toolbar input {
    width: 100%;
    background: #1e1e1e;
    border: 1px solid #333;
    border-radius: 6px;
    padding: 6px 10px;
    color: #e8e8e8;
    font-size: 12px;
    outline: none;
  }
  .sidebar-toolbar input:focus { border-color: #555; }
  .sort-row { display: flex; gap: 6px; }
  .sort-row .btn { flex: 1; font-size: 11px; padding: 4px 8px; }
  .sort-row .btn.active { background: #2a2a4a; border-color: #5555aa; color: #aaaaff; }

  /* ---- Session table ---- */
  .session-list {
    flex: 1;
    overflow-y: auto;
    overflow-x: hidden;
    display: flex;
    flex-direction: column;
  }
  .session-list::-webkit-scrollbar { width: 4px; }
  .session-list::-webkit-scrollbar-track { background: transparent; }
  .session-list::-webkit-scrollbar-thumb { background: #333; border-radius: 2px; }

  .col-header-row, .session-item {
    display: grid;
    grid-template-columns: var(--col-name,1fr) var(--col-date,130px) var(--col-size,62px);
    align-items: center;
  }

  .col-header-row {
    position: sticky; top: 0; z-index: 5;
    background: #161616;
    border-bottom: 1px solid #2a2a2a;
    flex-shrink: 0;
  }
  .col-header {
    padding: 7px 8px;
    font-size: 12px; font-weight: 400; letter-spacing: normal;
    text-transform: none; color: #ccc;
    white-space: nowrap; overflow: hidden;
    position: relative; user-select: none;
  }
  .col-header.sortable { cursor: pointer; }
  .col-header.sortable:hover { color: #fff; }
  .col-header.sort-active { color: #aaaaff; font-weight: 600; }
  .col-resize-grip {
    position: absolute; right: 0; top: 0; bottom: 0;
    width: 5px; cursor: col-resize; z-index: 2;
    background: transparent;
  }
  .col-resize-grip:hover, .col-resize-grip.dragging { background: #7c7cff55; }

  .session-item {
    border-bottom: 1px solid #1a1a1a;
    cursor: pointer;
    transition: background 0.1s;
  }
  .session-item:hover { background: #191919; }
  .session-item.active { background: #1e1e2e; border-left: 2px solid #7c7cff; }

  .session-item > div {
    padding: 7px 8px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    font-size: 12px;
  }
  .session-col-name { color: #ccc; }
  .session-item.active .session-col-name { color: #fff; font-weight: 500; }
  .session-col-date { color: #ccc; }
  .session-col-size { color: #ccc; text-align: right; }
  .session-item.active .session-col-date,
  .session-item.active .session-col-size { color: #fff; }

  .has-title-dot {
    display: inline-block; width: 5px; height: 5px;
    background: #7c7cff; border-radius: 50%;
    margin-right: 5px; vertical-align: middle;
    position: relative; top: -1px;
  }

  /* ---- Main panel ---- */
  .main {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .main-toolbar {
    padding: 10px 20px;
    border-bottom: 1px solid #222;
    background: #111;
    display: flex;
    align-items: center;
    gap: 8px;
    flex-shrink: 0;
    flex-wrap: wrap;
  }
  .main-toolbar .session-name {
    flex: 1;
    min-width: 0;
    max-width: 340px;
    font-size: 14px;
    font-weight: 600;
    color: #fff;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .main-toolbar .session-name.untitled { color: #555; font-weight: 400; font-style: italic; }
  .main-toolbar .session-name[data-editable="true"] { cursor: text; }
  .main-toolbar .session-name[data-editable="true"]:hover { color: #fff; text-decoration: underline dotted #555; }
  .main-toolbar .btn { flex-shrink: 0; }
  #inline-rename-input {
    flex: 1; min-width: 0; max-width: 340px;
    background: #1a1a2e; border: 1px solid #7c7cff;
    border-radius: 5px; padding: 3px 8px;
    color: #fff; font-size: 14px; font-weight: 600;
    outline: none;
  }

  .btn {
    background: #252535;
    border: 1px solid #44447a;
    color: #c8c8ff;
    padding: 5px 12px;
    border-radius: 6px;
    font-size: 12px;
    cursor: pointer;
    white-space: nowrap;
    transition: background 0.15s, border-color 0.15s;
  }
  .btn:hover { background: #32325a; border-color: #6666bb; color: #fff; }
  .btn.primary { background: #2e2e6e; border-color: #4040aa; color: #aaaaff; }
  .btn.primary:hover { background: #3a3a8a; border-color: #5555cc; color: #ccccff; }
  .btn.danger { color: #ff6b6b; border-color: #4a2020; }
  .btn.danger:hover { background: #2e1515; border-color: #883333; }
  .btn:disabled { opacity: 0.4; cursor: default; }

  #main-body {
    flex: 1;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }

  .conversation {
    flex: 1;
    overflow-y: auto;
    padding: 24px 32px;
    min-height: 0;
  }
  .conversation::-webkit-scrollbar { width: 6px; }
  .conversation::-webkit-scrollbar-track { background: transparent; }
  .conversation::-webkit-scrollbar-thumb { background: #333; border-radius: 3px; }
  .conversation::-webkit-scrollbar-thumb:hover { background: #555; }

  .empty-state {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    color: #333;
    gap: 8px;
    font-size: 14px;
  }
  .empty-state .icon { font-size: 36px; margin-bottom: 4px; }

  .msg {
    margin-bottom: 20px;
    max-width: 760px;
  }
  .msg-role {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    margin-bottom: 5px;
    color: #555;
  }
  .msg.user .msg-role { color: #7c7cff; }
  .msg.assistant .msg-role { color: #44aa88; }
  /* markdown inside assistant messages */
  .msg.assistant .msg-body h1,.msg.assistant .msg-body h2,.msg.assistant .msg-body h3 { color:#88ddbb; margin:.6em 0 .3em; font-weight:600; }
  .msg.assistant .msg-body h1 { font-size:1.15em; }
  .msg.assistant .msg-body h2 { font-size:1.05em; }
  .msg.assistant .msg-body h3 { font-size:.95em; }
  .msg.assistant .msg-body p { margin:.4em 0; }
  .msg.assistant .msg-body ul,.msg.assistant .msg-body ol { padding-left:1.4em; margin:.3em 0; }
  .msg.assistant .msg-body li { margin:.15em 0; }
  .msg.assistant .msg-body code { background:#0d0d1a; border:1px solid #333; border-radius:3px; padding:1px 5px; font-size:12px; color:#b8ffc8; font-family:monospace; }
  .msg.assistant .msg-body pre { background:#0d0d1a; border:1px solid #333; border-radius:6px; padding:10px 14px; overflow-x:auto; margin:.5em 0; }
  .msg.assistant .msg-body pre code { background:none; border:none; padding:0; color:#b8ffc8; }
  .msg.assistant .msg-body blockquote { border-left:3px solid #4a4a8a; margin:.4em 0; padding:.2em .8em; color:#999; }
  .msg.assistant .msg-body table { border-collapse:collapse; margin:.8em 0; font-size:12px; width:100%; border-radius:6px; overflow:hidden; border:1px solid #1e3a2e; }
  .msg.assistant .msg-body th { background:#0e2a1e; color:#88ddbb; font-weight:600; padding:8px 14px; text-align:left; border-bottom:2px solid #2a5a3a; border-right:1px solid #1e3a2e; font-size:11px; letter-spacing:.04em; text-transform:uppercase; }
  .msg.assistant .msg-body td { padding:7px 14px; border-bottom:1px solid #222; border-right:1px solid #222; color:#ccc; vertical-align:top; }
  .msg.assistant .msg-body tr:last-child td { border-bottom:none; }
  .msg.assistant .msg-body th:last-child,.msg.assistant .msg-body td:last-child { border-right:none; }
  .msg.assistant .msg-body tr:nth-child(even) td { background:#0f0f1e; }
  .msg.assistant .msg-body tr:hover td { background:#1a1a2e; }
  .msg.assistant .msg-body a { color:#7c7cff; }
  .msg.assistant .msg-body hr { border:none; border-top:1px solid #2a2a2a; margin:.6em 0; }

  .msg-body {
    font-size: 13px;
    line-height: 1.65;
    color: #ccc;
    border-radius: 10px;
    padding: 12px 16px;
    word-break: break-word;
  }
  /* User messages — right-aligned blue bubble */
  .msg.user { display:flex; flex-direction:column; align-items:flex-end; }
  .msg.user .msg-body {
    background: #1a1a40;
    border: 1px solid #3a3a80;
    color: #d0d0ff;
    max-width: 85%;
  }
  /* Assistant messages — left-aligned, subtle dark card */
  .msg.assistant .msg-body {
    background: #131318;
    border: 1px solid #2a2a35;
    color: #d0d0d8;
  }

  /* ---- Rename modal ---- */
  .overlay {
    display: none;
    position: fixed; inset: 0;
    background: rgba(0,0,0,0.7);
    z-index: 100;
    align-items: center;
    justify-content: center;
  }
  .overlay.show { display: flex; }
  .modal {
    background: #1a1a1a;
    border: 1px solid #333;
    border-radius: 10px;
    padding: 24px;
    width: 400px;
    max-width: 90vw;
  }
  .modal h2 { font-size: 15px; margin-bottom: 14px; }
  .modal input {
    width: 100%;
    background: #111;
    border: 1px solid #444;
    border-radius: 6px;
    padding: 8px 12px;
    color: #e8e8e8;
    font-size: 13px;
    outline: none;
    margin-bottom: 14px;
  }
  .modal input:focus { border-color: #7c7cff; }
  .modal-actions { display: flex; gap: 8px; justify-content: flex-end; }

  .toast {
    position: fixed;
    bottom: 20px; right: 20px;
    background: #1e2e1e;
    border: 1px solid #2a5a2a;
    color: #88cc88;
    padding: 10px 16px;
    border-radius: 8px;
    font-size: 13px;
    opacity: 0;
    transition: opacity 0.3s;
    pointer-events: none;
    z-index: 200;
  }
  .toast.show { opacity: 1; }
  .toast.error { background: #2e1e1e; border-color: #5a2a2a; color: #cc8888; }

  .spinner {
    display: inline-block;
    width: 10px; height: 10px;
    border: 2px solid #555;
    border-top-color: #aaa;
    border-radius: 50%;
    animation: spin 0.6s linear infinite;
    margin-right: 5px;
    vertical-align: middle;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* ---- Waiting-for-input sessions ---- */
  @keyframes waitpulse {
    0%, 100% { background: #1a1a1a; border-left-color: #ff9500; }
    50%       { background: #2a1800; border-left-color: #ffb700; }
  }
  .session-item.si-working { border-left: 3px solid #5555bb; }
  .session-item.si-working .session-col-name { color: #aaaaee; }
  .session-item.si-working .session-col-date,
  .session-item.si-working .session-col-size { color: #7777aa; }

  .session-item.si-idle { border-left: 3px solid #33884a; }
  .session-item.si-idle .session-col-name { color: #77cc99; }
  .session-item.si-idle .session-col-date,
  .session-item.si-idle .session-col-size { color: #448855; }

  .session-item.si-question {
    border-left: 3px solid #ff9500;
    animation: waitpulse 1.4s ease-in-out infinite;
  }
  .session-item.si-question .session-col-name { color: #ffb700; font-weight: 600; }
  .session-item.si-question .session-col-date,
  .session-item.si-question .session-col-size { color: #cc8800; }

  /* ---- Respond popup ---- */
  #respond-overlay {
    display:none; position:fixed; inset:0; background:rgba(0,0,0,.7);
    z-index:400; align-items:center; justify-content:center;
  }
  #respond-overlay.open { display:flex; }
  #respond-box {
    background:#1a1a2e; border:1px solid #ff9500; border-radius:10px;
    padding:22px 24px; width:520px; max-width:92vw; max-height:80vh;
    overflow-y:auto; box-shadow:0 8px 40px rgba(0,0,0,.6);
  }
  #respond-box h3 { margin:0 0 10px; font-size:14px; color:#ffb700; }
  #respond-question {
    background:#111; border-radius:6px; padding:12px; margin-bottom:14px;
    font-size:12px; color:#ccc; line-height:1.6; height:260px;
    overflow-y:auto; white-space:pre-wrap; word-break:break-word;
  }
  #respond-input {
    width:100%; box-sizing:border-box; background:#111; border:1px solid #444;
    border-radius:6px; padding:10px; color:#fff; font-size:13px;
    resize:vertical; min-height:70px; outline:none; font-family:inherit;
  }
  #respond-input:focus { border-color:#ff9500; }
  .respond-btns { display:flex; gap:8px; margin-top:10px; justify-content:flex-end; }
  .respond-btns button { padding:7px 18px; border-radius:6px; border:none;
    font-size:13px; cursor:pointer; }
  #respond-send { background:#ff9500; color:#000; font-weight:700; }
  #respond-send:hover { background:#ffb700; }
  #respond-cancel { background:#2a2a3a; color:#aaa; }
  #respond-cancel:hover { background:#3a3a4a; color:#fff; }
  #respond-options { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px; }
  .respond-opt {
    padding:7px 18px; border-radius:6px; border:1px solid #ff9500;
    background:#1a1000; color:#ffb700; font-size:13px; font-weight:600;
    cursor:pointer; transition:background .15s, color .15s;
  }
  .respond-opt:hover { background:#ff9500; color:#000; }
  #respond-custom-row { display:flex; flex-direction:column; gap:6px; margin-top:4px; }
  #respond-or { font-size:11px; color:#555; text-align:center; margin:4px 0 2px; }

  /* ---- Summary modal ---- */
  .sum-topic { font-size:15px; font-weight:600; color:#fff; margin-bottom:6px; line-height:1.4; }
  .sum-stats { font-size:11px; color:#555; margin-bottom:14px; }
  .sum-section { margin-bottom:14px; }
  .sum-label { font-size:11px; font-weight:700; letter-spacing:.06em; text-transform:uppercase; color:#7c7cff; margin-bottom:5px; }
  #summary-body ul { padding-left:18px; }
  #summary-body li { font-size:13px; color:#ccc; line-height:1.65; margin-bottom:3px; }

  /* ---- Session hover tooltip ---- */
  /* Question text bubble in live panel */
  .live-question-text {
    background: #1a1200; border: 1px solid #553300; border-radius: 6px;
    padding: 8px 10px; font-size: 12px; color: #ddaa55; line-height: 1.5;
    margin-bottom: 7px; white-space: pre-wrap; word-break: break-word; max-height: 120px;
    overflow-y: auto;
  }
  .live-option-btns {
    display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 7px;
  }
  .live-opt-btn {
    background: #1a1a2e; border: 1px solid #4444aa; color: #aaaaff;
    border-radius: 5px; padding: 4px 10px; font-size: 11px; cursor: pointer;
    transition: background .1s, border-color .1s;
  }
  .live-opt-btn:hover { background: #2a2a4a; border-color: #7c7cff; color: #fff; }

  #session-tooltip {
    position:fixed; z-index:9999; pointer-events:none;
    background:#1e1e1e; border:1px solid #3a3a3a; border-radius:7px;
    padding:9px 12px; max-width:280px; min-width:160px;
    box-shadow:0 4px 18px rgba(0,0,0,.6);
    opacity:0; transition:opacity .12s;
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  }
  #session-tooltip.visible { opacity:1; }
  .tt-title { font-size:12px; font-weight:600; color:#e8e8e8; margin-bottom:5px; line-height:1.3; }
  .tt-preview { font-size:11px; color:#888; line-height:1.5; margin-bottom:6px; }
  .tt-meta { font-size:10px; color:#555; display:flex; gap:8px; flex-wrap:wrap; }
  .tt-state { font-size:10px; font-weight:700; letter-spacing:.05em; text-transform:uppercase; }
  .tt-state.question { color:#ff9500; }
  .tt-state.working  { color:#7c7cff; }
  .tt-state.idle     { color:#44aa66; }
  .tt-state.sleeping { color:#444; }

  .count-badge {
    background: #222;
    color: #555;
    border-radius: 10px;
    padding: 1px 7px;
    font-size: 10px;
    margin-left: 4px;
  }

  /* ---- Extract Code drawer ---- */
  #extract-drawer {
    position:fixed; top:0; right:-520px; width:500px; height:100vh;
    background:#111; border-left:1px solid #2a2a3a; z-index:300;
    transition:right .25s ease; overflow-y:auto; display:flex; flex-direction:column;
  }
  #extract-drawer.open { right:0; }
  #extract-drawer-header { padding:16px 18px; border-bottom:1px solid #1e1e2e;
    display:flex; justify-content:space-between; align-items:center; flex-shrink:0; }
  .code-block-card { margin:10px; background:#0d0d1a; border:1px solid #2a2a3a;
    border-radius:8px; overflow:hidden; }
  .code-block-header { padding:8px 12px; background:#1a1a2e; display:flex;
    justify-content:space-between; align-items:center; font-size:11px; }
  .code-lang-badge { background:#7c7cff22; color:#7c7cff; border-radius:4px;
    padding:2px 7px; font-size:10px; font-weight:700; text-transform:uppercase; }
  .code-shell-badge { background:#ff950022; color:#ff9500; border-radius:4px;
    padding:2px 7px; font-size:10px; font-weight:700; }
  .code-dup-badge { background:#55555522; color:#888; border-radius:4px;
    padding:2px 7px; font-size:10px; }
  .code-filename { color:#aaa; font-size:11px; font-family:monospace; }
  .code-block-pre { margin:0; padding:12px; overflow-x:auto; font-size:11px;
    line-height:1.5; color:#e0e0e0; font-family:'Consolas','Courier New',monospace;
    max-height:240px; overflow-y:auto; white-space:pre; }
  .code-copy-btn { background:#2a2a3a; border:none; color:#aaa; border-radius:4px;
    padding:3px 10px; font-size:11px; cursor:pointer; }
  .code-copy-btn:hover { background:#3a3a4a; color:#fff; }
  #extract-copy-all { background:#7c7cff; color:#fff; border:none; border-radius:6px;
    padding:7px 16px; font-size:12px; font-weight:600; cursor:pointer; margin:10px; }
  #extract-copy-all:hover { background:#9a9aff; }

  /* ---- Find bar ---- */
  #find-bar {
    display:none; padding:6px 10px; background:#0d0d1a; border-bottom:1px solid #1e1e2e;
    align-items:center; gap:8px;
  }
  #find-bar.open { display:flex; }
  #find-input { flex:1; background:#111; border:1px solid #333; border-radius:5px;
    padding:5px 10px; color:#fff; font-size:12px; outline:none; }
  #find-input:focus { border-color:#7c7cff; }
  #find-count { font-size:11px; color:#666; min-width:60px; }
  .find-nav-btn { background:#2a2a3a; border:none; color:#aaa; border-radius:4px;
    padding:4px 10px; cursor:pointer; font-size:12px; }
  .find-nav-btn:hover { background:#3a3a4a; color:#fff; }
  mark.find-match { background:#7c7cff44; color:inherit; border-radius:2px; }
  mark.find-match.current { background:#7c7cff; color:#fff; }

  /* ---- Compare modal ---- */
  #compare-overlay { display:none; position:fixed; inset:0; background:rgba(0,0,0,.75);
    z-index:400; align-items:flex-start; justify-content:center; padding-top:40px; }
  #compare-overlay.open { display:flex; }
  #compare-box { background:#111; border:1px solid #2a2a3a; border-radius:10px;
    width:90vw; max-width:1100px; max-height:85vh; overflow:hidden;
    display:flex; flex-direction:column; }
  #compare-box-header { padding:16px 18px; border-bottom:1px solid #1e1e2e;
    display:flex; justify-content:space-between; align-items:center; flex-shrink:0; }
  #compare-picker { padding:14px 18px; border-bottom:1px solid #1e1e2e;
    display:flex; align-items:center; gap:10px; flex-shrink:0; }
  #compare-picker select { background:#1a1a2e; border:1px solid #333; color:#ccc;
    border-radius:6px; padding:6px 10px; font-size:12px; flex:1; }
  #compare-run { background:#7c7cff; color:#fff; border:none; border-radius:6px;
    padding:7px 16px; font-size:12px; font-weight:600; cursor:pointer; }
  #compare-run:hover { background:#9a9aff; }
  #compare-body { overflow-y:auto; flex:1; padding:14px 18px; }
  .compare-meta { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:14px; }
  .compare-meta-card { background:#0d0d1a; border:1px solid #2a2a3a; border-radius:8px;
    padding:12px; font-size:12px; color:#ccc; }
  .compare-meta-card h4 { margin:0 0 6px; color:#7c7cff; font-size:12px; }
  .diff-row { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:8px; }
  .diff-cell { background:#0d0d1a; border-radius:6px; padding:10px; font-size:11px;
    font-family:monospace; white-space:pre-wrap; max-height:200px; overflow-y:auto; color:#ccc; }
  .diff-added .diff-cell:last-child { border:1px solid #22aa4444; background:#00200a; }
  .diff-removed .diff-cell:first-child { border:1px solid #aa222244; background:#200000; }
  .diff-changed { }
  .diff-cell-label { font-size:10px; font-weight:700; text-transform:uppercase;
    color:#555; margin-bottom:4px; }
  .diff-status-badge { font-size:10px; border-radius:4px; padding:2px 6px; font-weight:700; }
  .diff-added .diff-status-badge { background:#00440020; color:#44cc88; }
  .diff-removed .diff-status-badge { background:#44000020; color:#cc4444; }
  .diff-changed .diff-status-badge { background:#44440020; color:#cccc44; }
  .diff-same .diff-status-badge { background:#22222220; color:#666; }

  /* ---- View mode toggle ---- */
  .view-toggle { display:flex; gap:4px; flex-shrink:0; }
  .view-toggle-btn {
    background:#1e1e1e; border:1px solid #333; color:#666;
    padding:4px 8px; border-radius:5px; font-size:13px; cursor:pointer;
    transition:background .15s, border-color .15s, color .15s;
    line-height:1;
  }
  .view-toggle-btn:hover { background:#2a2a2a; color:#aaa; border-color:#444; }
  .view-toggle-btn.active { background:#2a2a4a; border-color:#5555aa; color:#aaaaff; }

  /* ---- Workforce sort bar ---- */
  .wf-sort-bar { display:flex; gap:6px; padding:6px 10px; border-bottom:1px solid #222; flex-shrink:0; }
  .wf-sort-btn {
    background:#1e1e1e; border:1px solid #333; color:#888;
    padding:3px 10px; border-radius:12px; font-size:11px; cursor:pointer;
    transition:background .15s, border-color .15s, color .15s;
  }
  .wf-sort-btn:hover { background:#2a2a2a; color:#ccc; border-color:#444; }
  .wf-sort-btn.active { background:#2a2a4a; border-color:#5555aa; color:#aaaaff; }

  /* ---- Workforce grid ---- */
  #workforce-grid {
    display:none; flex-wrap:wrap; gap:10px;
    padding:12px; overflow-y:auto; flex:1; align-content:flex-start;
  }
  #workforce-grid.visible { display:flex; }
  #workforce-grid::-webkit-scrollbar { width:4px; }
  #workforce-grid::-webkit-scrollbar-track { background:transparent; }
  #workforce-grid::-webkit-scrollbar-thumb { background:#333; border-radius:2px; }

  .wf-card {
    width:100px; height:110px;
    background:#161616; border:1px solid #2a2a2a; border-radius:10px;
    display:flex; flex-direction:column; align-items:center; justify-content:center;
    gap:4px; cursor:pointer; padding:8px 6px; transition:background .15s, border-color .15s;
    position:relative; overflow:hidden; flex-shrink:0;
  }
  .wf-card:hover { background:#1e1e1e; border-color:#444; }
  .wf-card.wf-selected { border-color:#7c7cff; background:#1e1e2e; }

  .wf-card.wf-sleeping { border-color:#222; }
  .wf-card.wf-sleeping .wf-avatar { filter:grayscale(1) opacity(.4); }

  .wf-card.wf-working {
    border-color:#4a4aaa;
    animation:wf-work-pulse 2s ease-in-out infinite;
  }
  @keyframes wf-work-pulse {
    0%, 100% { background:#161616; border-color:#4a4aaa; }
    50%       { background:#12122a; border-color:#6a6acc; }
  }

  .wf-card.wf-idle { border-color:#2a5a3a; background:#0d1a12; }

  .wf-card.wf-question {
    border-color:#ff9500;
    animation:wf-pulse 1.4s ease-in-out infinite;
  }
  @keyframes wf-pulse {
    0%, 100% { background:#161616; border-color:#ff9500; }
    50%       { background:#2a1800; border-color:#ffb700; }
  }

  .wf-avatar { font-size:28px; line-height:1; }
  .wf-status-label {
    font-size:9px; font-weight:700; letter-spacing:.06em; text-transform:uppercase;
    padding:1px 6px; border-radius:8px; white-space:nowrap;
  }
  .wf-sleeping  .wf-status-label { background:#222;    color:#444; }
  .wf-working   .wf-status-label { background:#2a2a4a; color:#7c7cff; }
  .wf-idle      .wf-status-label { background:#1a3a22; color:#44aa66; }
  .wf-question  .wf-status-label { background:#3a1800; color:#ff9500; }
  .wf-name {
    font-size:10px; color:#888; text-align:center;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
    max-width:90px;
  }
  .wf-card.wf-selected .wf-name { color:#fff; }
  .wf-meta { font-size:9px; color:#444; text-align:center; max-width:90px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }

  /* ---- Button groups ---- */
  .main-toolbar {
    display:flex; align-items:center; gap:4px;
    height:38px; padding:0 10px; overflow:visible; flex-shrink:0;
  }
  #main-title {
    flex:1; min-width:0; margin-right:6px;
  }
  .btn-group { display:flex; align-items:center; flex-shrink:0; position:relative; }
  .btn-group-inner { display:none; } /* always hidden — buttons live in dropdown popups */
  .btn-group-label {
    display:inline-flex; align-items:center; gap:3px;
    background:#1e1e1e; border:1px solid #333; border-radius:6px;
    padding:4px 10px; color:#bbb; font-size:11px; font-weight:600;
    letter-spacing:.03em; cursor:pointer; white-space:nowrap; user-select:none;
    transition:background .12s, border-color .12s, color .12s;
  }
  .btn-group-label:hover { background:#2a2a2a; border-color:#555; color:#fff; }
  .btn-group-label.grp-open { background:#1a1a2e; border-color:#7c7cff; color:#aaaaff; }
  .btn-group-divider { width:1px; height:18px; background:#252525; flex-shrink:0; margin:0 2px; }

  /* ---- Group dropdown popup ---- */
  .grp-popup {
    position:fixed; background:#1a1a1a; border:1px solid #333; border-radius:8px;
    padding:7px; display:flex; flex-direction:column; gap:4px;
    z-index:2000; min-width:150px; box-shadow:0 6px 24px rgba(0,0,0,.7);
  }
  .grp-popup .btn { text-align:left; width:100%; justify-content:flex-start; }

  /* ---- Live terminal panel ---- */
  .live-panel {
    display:flex; flex-direction:column; flex:1; overflow:hidden;
    background:#0a0a0a; min-height:0;
  }
  .live-log {
    flex:1; overflow-y:auto; padding:10px 14px;
    font-size:12px; line-height:1.5; min-height:0;
    font-family:'Consolas','Courier New',monospace;
  }
  .live-log::-webkit-scrollbar { width:4px; }
  .live-log::-webkit-scrollbar-track { background:transparent; }
  .live-log::-webkit-scrollbar-thumb { background:#333; border-radius:2px; }
  .live-entry { margin-bottom:10px; }
  .live-label {
    font-size:9px; font-weight:700; letter-spacing:.08em; text-transform:uppercase;
    margin-bottom:2px; font-family:-apple-system,sans-serif;
  }
  .live-entry-user   .live-label { color:#7c7cff; }
  .live-entry-asst   .live-label { color:#44aa88; }
  .live-entry-tool   .live-label { color:#ff9500; }
  .live-entry-result .live-label { color:#444; }
  .live-text { color:#ccc; word-break:break-word; font-size:12px; }
  .live-entry-user .live-text { color:#aaaaee; white-space:pre-wrap; }
  .live-entry-asst .live-text p { margin:.3em 0; }
  .live-entry-asst .live-text h1,.live-entry-asst .live-text h2,.live-entry-asst .live-text h3 { color:#88ddbb; margin:.5em 0 .2em; }
  .live-entry-asst .live-text code { background:#0d0d1a; border:1px solid #333; border-radius:3px; padding:1px 4px; color:#b8ffc8; font-size:11px; }
  .live-entry-asst .live-text pre { background:#0d0d1a; border:1px solid #333; border-radius:5px; padding:8px 12px; overflow-x:auto; margin:.4em 0; }
  .live-entry-asst .live-text pre code { background:none; border:none; padding:0; }
  .live-entry-asst .live-text ul,.live-entry-asst .live-text ol { padding-left:1.3em; margin:.3em 0; }
  .live-entry-asst .live-text table { border-collapse:collapse; margin:.6em 0; font-size:11px; width:100%; border:1px solid #1e3a2e; border-radius:6px; overflow:hidden; }
  .live-entry-asst .live-text th { background:#0e2a1e; color:#88ddbb; font-weight:600; padding:6px 12px; text-align:left; border-bottom:2px solid #2a5a3a; border-right:1px solid #1e3a2e; font-size:10px; text-transform:uppercase; letter-spacing:.04em; }
  .live-entry-asst .live-text td { padding:5px 12px; border-bottom:1px solid #222; border-right:1px solid #222; color:#ccc; vertical-align:top; }
  .live-entry-asst .live-text tr:last-child td { border-bottom:none; }
  .live-entry-asst .live-text th:last-child,.live-entry-asst .live-text td:last-child { border-right:none; }
  .live-entry-asst .live-text tr:nth-child(even) td { background:#0f0f1e; }
  .live-entry-asst .live-text tr:hover td { background:#1a1a2e; }
  .live-entry-user .live-text { color:#aaaaee; }
  .live-expand-btn {
    background:none; border:none; color:#444; cursor:pointer;
    font-size:10px; padding:0 3px; line-height:1; vertical-align:middle;
    font-family:-apple-system,sans-serif;
  }
  .live-expand-btn:hover { color:#888; }
  .live-tool-line { display:flex; align-items:baseline; gap:5px; cursor:pointer; user-select:none; }
  .live-tool-icon { color:#ff9500; flex-shrink:0; font-size:11px; }
  .live-tool-name { color:#ffb700; font-weight:600; font-size:11px; flex-shrink:0; }
  .live-tool-desc { color:#555; font-size:11px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; flex:1; min-width:0; }
  .live-tool-detail {
    margin-top:3px; padding:5px 8px; background:#0d0d0d; border-left:2px solid #2a2a2a;
    font-size:11px; color:#666; white-space:pre-wrap; word-break:break-word;
    max-height:200px; overflow-y:auto; display:none;
  }
  .live-tool-detail.open { display:block; }
  .live-result-line { font-size:11px; cursor:pointer; user-select:none; }
  .live-result-ok  { color:#2a4a2a; }
  .live-result-err { color:#4a2a2a; }

  /* ---- Live input bar ---- */
  .live-input-bar {
    border-top:1px solid #181818; padding:8px 12px;
    background:#0d0d0d; flex-shrink:0;
  }
  .live-working {
    display:flex; align-items:center; gap:8px; color:#444;
    font-size:12px; font-family:-apple-system,sans-serif;
  }
  .live-waiting-label {
    font-size:10px; font-weight:700; color:#ff9500;
    letter-spacing:.06em; text-transform:uppercase;
    font-family:-apple-system,sans-serif; margin-bottom:5px;
  }
  .live-textarea {
    width:100%; background:#111; border:1px solid #2a2a2a;
    border-radius:5px; padding:7px 10px; color:#e0e0e0;
    font-size:12px; resize:none; outline:none;
    font-family:'Consolas','Courier New',monospace;
    min-height:48px; max-height:100px; display:block;
  }
  .live-textarea:focus { border-color:#444; }
  .live-textarea.waiting-focus:focus { border-color:#ff9500; }
  .live-bar-row {
    display:flex; justify-content:flex-end; align-items:center; gap:6px; margin-top:5px;
  }
  .live-send-btn {
    background:#2a2a4a; color:#aaaaff; border:1px solid #4040aa;
    border-radius:5px; padding:4px 12px; font-size:12px; font-weight:600;
    cursor:pointer; white-space:nowrap;
  }
  .live-send-btn:hover { background:#3a3a6a; color:#ccccff; }
  .live-send-btn:disabled { opacity:0.4; cursor:default; }
  .live-send-btn.waiting { background:#ff9500; color:#000; border-color:#ff9500; }
  .live-send-btn.waiting:hover { background:#ffb700; }
  .live-ended {
    color:#444; font-size:12px; font-family:-apple-system,sans-serif;
    display:flex; align-items:center; gap:10px; justify-content:space-between;
  }
</style>
<script>
// Lightweight markdown renderer (no CDN dependency)
function mdParse(md) {
    if (!md) return '';
    let html = md;
    // Escape HTML in non-code regions (we'll handle code blocks first)
    const codeBlocks = [];
    // Fenced code blocks
    html = html.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
      const idx = codeBlocks.length;
      codeBlocks.push('<pre><code>' + code.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') + '</code></pre>');
      return '\x00CODE' + idx + '\x00';
    });
    // Inline code
    html = html.replace(/`([^`]+)`/g, (_, code) => {
      const idx = codeBlocks.length;
      codeBlocks.push('<code>' + code.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') + '</code>');
      return '\x00CODE' + idx + '\x00';
    });
    // Escape remaining HTML
    html = html.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    // Headers
    html = html.replace(/^######\s+(.+)$/gm, '<h6>$1</h6>');
    html = html.replace(/^#####\s+(.+)$/gm, '<h5>$1</h5>');
    html = html.replace(/^####\s+(.+)$/gm, '<h4>$1</h4>');
    html = html.replace(/^###\s+(.+)$/gm, '<h3>$1</h3>');
    html = html.replace(/^##\s+(.+)$/gm, '<h2>$1</h2>');
    html = html.replace(/^#\s+(.+)$/gm, '<h1>$1</h1>');
    // Bold / italic
    html = html.replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>');
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
    html = html.replace(/___(.+?)___/g, '<strong><em>$1</em></strong>');
    html = html.replace(/__(.+?)__/g, '<strong>$1</strong>');
    html = html.replace(/_(.+?)_/g, '<em>$1</em>');
    // Blockquotes
    html = html.replace(/^&gt;\s?(.+)$/gm, '<blockquote>$1</blockquote>');
    // Horizontal rule
    html = html.replace(/^[-*_]{3,}$/gm, '<hr>');
    // Lists — collect consecutive lines
    html = html.replace(/((?:^[-*+]\s+.+\n?)+)/gm, (block) => {
      const items = block.trim().split('\n').map(l => '<li>' + l.replace(/^[-*+]\s+/, '') + '</li>').join('');
      return '<ul>' + items + '</ul>\n';
    });
    html = html.replace(/((?:^\d+\.\s+.+\n?)+)/gm, (block) => {
      const items = block.trim().split('\n').map(l => '<li>' + l.replace(/^\d+\.\s+/, '') + '</li>').join('');
      return '<ol>' + items + '</ol>\n';
    });
    // Tables — | col | col | rows with a separator row of |---|---|
    html = html.replace(/((?:^\|.+\|\n?)+)/gm, (block) => {
      const rows = block.trim().split('\n').filter(r => r.trim());
      if (rows.length < 2) return block;
      const sepIdx = rows.findIndex(r => /^\|[\s\-|:]+\|$/.test(r.trim()));
      if (sepIdx < 0) return block;
      const headerRows = rows.slice(0, sepIdx);
      const bodyRows = rows.slice(sepIdx + 1);
      const parseRow = (r, tag) => '<tr>' + r.replace(/^\||\|$/g,'').split('|').map(c => `<${tag}>${c.trim()}</${tag}>`).join('') + '</tr>';
      const thead = '<thead>' + headerRows.map(r => parseRow(r,'th')).join('') + '</thead>';
      const tbody = bodyRows.length ? '<tbody>' + bodyRows.map(r => parseRow(r,'td')).join('') + '</tbody>' : '';
      return '<table>' + thead + tbody + '</table>\n';
    });
    // Links
    html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
    // Paragraphs — wrap double-newline separated blocks
    html = html.split(/\n{2,}/).map(para => {
      para = para.trim();
      if (!para) return '';
      if (/^<(h[1-6]|ul|ol|li|blockquote|hr|pre|table)/.test(para)) return para;
      if (para.includes('\x00CODE')) return para;
      return '<p>' + para.replace(/\n/g, '<br>') + '</p>';
    }).join('\n');
    // Restore code blocks
    codeBlocks.forEach((block, idx) => {
      html = html.replace('\x00CODE' + idx + '\x00', block);
    });
    return html;
}
</script>
</head>
<body>

<header>
  <h1>Claude Code GUI</h1>
  <div class="hdr-sys" id="hdr-sys">
    <button class="hdr-sys-btn" onclick="toggleHdrSys()">System &#9662;</button>
    <div class="hdr-sys-dropdown" id="hdr-sys-dropdown">
      <button onclick="deleteEmptySessions();closeHdrSys()">Delete Empty Sessions</button>
    </div>
  </div>
  <button id="btn-git-update" onclick="openGitUpdate()">Update App <span id="git-badge-pull"></span></button>
  <button id="btn-git-publish" onclick="openGitPublish()">Publish App Update <span id="git-badge-push"></span></button>
  <div class="hdr-spacer"></div>
  <select id="project-picker" title="Switch project"></select>
  <span class="sub" id="session-count"></span>
</header>

<div class="layout">
  <!-- Sidebar -->
  <div class="sidebar">
    <div class="sidebar-toolbar">
      <div style="display:flex;align-items:center;gap:6px;">
        <div class="view-toggle">
          <button class="view-toggle-btn active" id="btn-view-workforce" onclick="setViewMode('workforce')" title="Workforce view">&#128101;</button>
          <button class="view-toggle-btn" id="btn-view-list" onclick="setViewMode('list')" title="List view">&#9776;</button>
        </div>
        <input type="text" id="search" placeholder="Search sessions…" oninput="filterSessions()" style="flex:1;width:auto;">
      </div>
    </div>
    <div class="session-list" id="session-list">
      <div style="padding:20px;color:#444;font-size:12px;">Loading…</div>
    </div>
    <div class="wf-sort-bar" id="wf-sort-bar" style="display:none;">
      <button class="wf-sort-btn active" id="wf-btn-status" onclick="setWfSort('status')">Status</button>
      <button class="wf-sort-btn" id="wf-btn-recent" onclick="setWfSort('recent')">Recent</button>
      <button class="wf-sort-btn" id="wf-btn-name" onclick="setWfSort('name')">Name</button>
    </div>
    <div id="workforce-grid"></div>
  </div>

  <!-- Resize handle -->
  <div class="resize-handle" id="resize-handle"></div>

  <!-- Main -->
  <div class="main" id="main-panel">
    <div class="main-toolbar" id="main-toolbar">
      <div id="main-title" data-editable="false" data-custom-title=""></div>
      <div class="btn-group" id="grp-session">
        <span class="btn-group-label" onclick="toggleGrpDropdown('grp-session')">Session &#9662;</span>
        <div class="btn-group-inner">
          <button class="btn" id="btn-open-gui" disabled onclick="openInGUI(activeId)" title="View this session's live terminal and chat log inside the app">Open in Claude Code GUI</button>
          <button class="btn" id="btn-open" disabled onclick="openInClaude(activeId)" title="Launch this session in a separate Claude terminal window">Claude Terminal</button>
          <button class="btn" id="btn-continue" disabled onclick="continueSession(activeId)" title="Start a new Claude session that continues from where this one left off">Continue</button>
          <button class="btn danger" id="btn-close" disabled onclick="closeSession(activeId)" title="Close the running Claude process for this session">Close Session</button>
        </div>
      </div>
      <div class="btn-group-divider"></div>
      <div class="btn-group" id="grp-manage">
        <span class="btn-group-label" onclick="toggleGrpDropdown('grp-manage')">Manage &#9662;</span>
        <div class="btn-group-inner">
          <button class="btn" id="btn-autoname" disabled onclick="autoName(activeId)" title="Let Claude read the conversation and suggest a meaningful name">Auto-name</button>
          <button class="btn" id="btn-duplicate" disabled onclick="duplicateSession(activeId)" title="Create a copy of this session to branch off in a new direction">Duplicate</button>
          <button class="btn danger" id="btn-delete" disabled onclick="deleteSession(activeId)" title="Permanently delete this session and its history">Delete</button>
        </div>
      </div>
      <div class="btn-group-divider"></div>
      <div class="btn-group" id="grp-analyze">
        <span class="btn-group-label" onclick="toggleGrpDropdown('grp-analyze')">Analyze &#9662;</span>
        <div class="btn-group-inner">
          <button class="btn" id="btn-summary" disabled onclick="showSummary(activeId)" title="Generate an AI summary of what was accomplished in this session">Summary</button>
          <button class="btn" id="btn-find"    onclick="openFind()" title="Search for text within the session transcript">Find</button>
          <button class="btn" id="btn-extract" onclick="openExtract()" disabled title="Pull out all code blocks from this session into a downloadable file">Extract Code</button>
          <button class="btn" id="btn-export"  onclick="triggerExport()" disabled title="Export the full session as a zip file including all generated files">Export Project</button>
          <button class="btn" id="btn-compare" onclick="openCompare()" title="Diff two sessions side by side to see what changed">Compare</button>
        </div>
      </div>
    </div>
    <div id="find-bar">
      <input id="find-input" placeholder="Search in session…" oninput="runFind()" onkeydown="findKeyNav(event)">
      <span id="find-count"></span>
      <button class="find-nav-btn" onclick="findNav(-1)">↑</button>
      <button class="find-nav-btn" onclick="findNav(1)">↓</button>
      <button class="find-nav-btn" onclick="closeFind()">✕</button>
    </div>
    <div id="main-body">
      <div class="empty-state">
        <div class="icon">💬</div>
        <div>Select a session to preview it</div>
      </div>
    </div>
  </div>
</div>

<!-- Summary modal -->
<div class="overlay" id="summary-overlay">
  <div class="modal" style="width:520px;max-width:92vw;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
      <h2 style="font-size:14px;">Session Summary</h2>
      <button class="btn" onclick="closeSummary()" style="padding:3px 10px;">✕</button>
    </div>
    <div id="summary-body" style="min-height:80px;"></div>
  </div>
</div>

<!-- Respond-to-waiting modal -->
<div id="respond-overlay">
  <div id="respond-box">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
      <h3 style="margin:0;">&#x23F3; Session waiting for input</h3>
      <button class="btn" onclick="closeRespond()" style="padding:2px 9px;">✕</button>
    </div>
    <div id="respond-question"></div>
    <div id="respond-options"></div>
    <div id="respond-or" style="display:none;">— or type a custom response —</div>
    <div id="respond-custom-row">
      <textarea id="respond-input" placeholder="Type your response…"
        onkeydown="if(event.key==='Enter'&&(event.ctrlKey||event.metaKey))submitRespond()"></textarea>
      <div class="respond-btns">
        <button id="respond-cancel" onclick="closeRespond()">Cancel</button>
        <button id="respond-send" onclick="submitRespond()">Send ↵</button>
      </div>
    </div>
  </div>
</div>

<!-- Extract Code drawer -->
<div id="extract-drawer">
  <div id="extract-drawer-header">
    <span style="font-size:13px;font-weight:600;color:#fff;">Code Blocks</span>
    <div style="display:flex;gap:8px;align-items:center;">
      <button id="extract-copy-all" style="display:none;">Copy All</button>
      <button id="extract-export-btn" class="btn" style="padding:4px 10px;font-size:11px;">Export ZIP</button>
      <button class="btn" onclick="closeExtract()" style="padding:3px 10px;">✕</button>
    </div>
  </div>
  <div id="extract-body" style="flex:1;overflow-y:auto;"></div>
</div>

<!-- Compare modal -->
<div id="compare-overlay">
  <div id="compare-box">
    <div id="compare-box-header">
      <span style="font-size:13px;font-weight:600;color:#fff;">Compare Sessions</span>
      <button class="btn" onclick="closeCompare()" style="padding:3px 10px;">✕</button>
    </div>
    <div id="compare-picker">
      <span style="font-size:12px;color:#888;white-space:nowrap;">Compare with:</span>
      <select id="compare-select"></select>
      <button id="compare-run" onclick="runCompare()">Compare</button>
    </div>
    <div id="compare-body"><p style="color:#555;padding:20px;font-size:12px;">Select a session above and click Compare.</p></div>
  </div>
</div>

<!-- Git Sync modal -->
<div class="overlay" id="git-sync-overlay">
  <div class="modal" style="width:460px;max-width:92vw;">
    <h2 id="git-sync-title">Git Sync</h2>
    <div id="git-sync-body" style="color:#ccc;font-size:13px;line-height:1.6;margin-bottom:18px;"></div>
    <div class="modal-actions" id="git-sync-actions"></div>
  </div>
</div>

<!-- Rename modal -->
<div class="overlay" id="rename-overlay">
  <div class="modal">
    <h2>Rename Session</h2>
    <input type="text" id="rename-input" placeholder="Enter a name…" onkeydown="if(event.key==='Enter')submitRename(); if(event.key==='Escape')closeRename();">
    <div class="modal-actions">
      <button class="btn" onclick="closeRename()">Cancel</button>
      <button class="btn primary" onclick="submitRename()">Save</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let allSessions = [];
let activeId = null;
let renameTarget = null;
let sortMode = 'date';  // 'date' | 'size'
let sortAsc  = false;   // false = descending (newest/largest first)
let viewMode = localStorage.getItem('viewMode') || 'workforce';
let wfSort = localStorage.getItem('wfSort') || 'status';
let runningIds = new Set();

function setSort(mode) {
  if (sortMode === mode) {
    sortAsc = !sortAsc;   // same column — toggle direction
  } else {
    sortMode = mode;
    sortAsc = false;      // new column — default descending
  }
  filterSessions();
}

function sortedSessions(sessions) {
  const copy = [...sessions];
  const dir = sortAsc ? 1 : -1;
  if (sortMode === 'size') {
    copy.sort((a, b) => dir * ((a.file_bytes || 0) - (b.file_bytes || 0)));
  } else if (sortMode === 'name') {
    copy.sort((a, b) => dir * (a.display_title || '').localeCompare(b.display_title || ''));
  } else {
    copy.sort((a, b) => dir * ((a.last_activity_ts || a.sort_ts || 0) - (b.last_activity_ts || b.sort_ts || 0)));
  }
  return copy;
}

async function loadProjects() {
  const res = await fetch('/api/projects');
  const projects = await res.json();
  const sel = document.getElementById('project-picker');
  const saved = localStorage.getItem('activeProject');
  sel.innerHTML = projects.map(p => {
    const parts = p.display.replace(/\\/g, '/').split('/');
    const label = parts.slice(-2).join('/') + ' (' + p.session_count + ')';
    const selected = p.encoded === saved ? ' selected' : '';
    return '<option value="' + escHtml(p.encoded) + '"' + selected + '>' + escHtml(label) + '</option>';
  }).join('');
  // If saved project exists in list, activate it; otherwise use first
  const savedMatch = projects.find(p => p.encoded === saved);
  // If saved project has sessions use it; otherwise pick the project with the most sessions
  const target = (savedMatch && savedMatch.session_count > 0)
    ? saved
    : (projects.slice().sort((a,b) => b.session_count - a.session_count)[0] || {}).encoded;
  if (target) await setProject(target, true);
}

async function setProject(encoded, reload = true) {
  await fetch('/api/set-project', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({project: encoded})
  });
  localStorage.setItem('activeProject', encoded);
  if (reload) loadSessions();
}

document.getElementById('project-picker').addEventListener('change', e => {
  setProject(e.target.value);
});

async function loadSessions() {
  const resp = await fetch('/api/sessions');
  allSessions = await resp.json();
  document.getElementById('session-count').textContent = allSessions.length + ' sessions';
  setViewMode(viewMode);
}

function filterSessions() {
  const q = document.getElementById('search').value.toLowerCase();
  const filtered = q
    ? allSessions.filter(s =>
        (s.display_title||'').toLowerCase().includes(q) ||
        (s.preview||'').toLowerCase().includes(q)
      )
    : allSessions;
  if (viewMode === 'workforce') {
    renderWorkforce(wfSortedSessions(filtered));
  } else {
    renderList(sortedSessions(filtered));
  }
}

function renderList(sessions) {
  const el = document.getElementById('session-list');
  if (!sessions.length) {
    el.innerHTML = '<div style="padding:20px;color:#444;font-size:12px;">No sessions found</div>';
    return;
  }

  const arrow = sortAsc ? '↑' : '↓';
  const header = `
    <div class="col-header-row">
      <div class="col-header sortable ${sortMode==='name'?'sort-active':''}" id="col-h-name" onclick="setSort('name')" title="Sort by name">
        Name ${sortMode==='name' ? arrow : ''}
        <span class="col-resize-grip" data-col="name"></span>
      </div>
      <div class="col-header sortable ${sortMode==='date'?'sort-active':''}" id="col-h-date" onclick="setSort('date')" title="Sort by date">
        Date ${sortMode==='date' ? arrow : ''}
        <span class="col-resize-grip" data-col="date"></span>
      </div>
      <div class="col-header sortable ${sortMode==='size'?'sort-active':''}" id="col-h-size" onclick="setSort('size')" title="Sort by size">
        Size ${sortMode==='size' ? arrow : ''}
      </div>
    </div>`;

  const rows = sessions.map(s => {
    const isWaiting = !!waitingData[s.id];
    const isRunning = !isWaiting && runningIds.has(s.id);
    const stateClass = isWaiting ? ' waiting' : (isRunning ? ' running' : '');
    const activeClass = s.id === activeId ? ' active' : '';
    const colClick = `onclick="singleOrDouble('${s.id}',event)" style="cursor:pointer;"`;
    const icon = isWaiting
      ? '<span title="Waiting for input" style="color:#ff9500;margin-right:4px;">&#x23F3;</span>'
      : isRunning
      ? '<span title="Running" style="color:#44bb66;margin-right:5px;font-size:9px;">&#9679;</span>'
      : '';
    return `
    <div class="session-item${activeClass}${stateClass}" data-sid="${s.id}">
      <div class="session-col-name" onclick="handleNameClick('${s.id}')" style="cursor:text;" title="Click to rename">
        ${icon}${escHtml(s.display_title)}
      </div>
      <div class="session-col-date" ${colClick}>${escHtml(s.last_activity)}</div>
      <div class="session-col-size" ${colClick}>${escHtml(s.size)}</div>
    </div>`;
  }).join('');

  el.innerHTML = header + rows;
  initColResize();
  attachTooltipListeners();
}

/* ---- Hover tooltip ---- */
function attachTooltipListeners() {
  document.querySelectorAll('.session-item[data-sid]').forEach(row => {
    row.addEventListener('mouseenter', onRowEnter);
    row.addEventListener('mouseleave', onRowLeave);
    row.addEventListener('mousemove',  onRowMove);
  });
}

function onRowEnter(e) {
  const id = e.currentTarget.dataset.sid;
  if (!id) return;
  const s = allSessions.find(x => x.id === id);
  if (!s) return;

  const status = getSessionStatus(id);
  const stateLabels = { question:'🙋 Question', working:'⛏️ Working', idle:'💻 Idle', sleeping:'😴 Sleeping' };
  const stateLabel = stateLabels[status] || status;

  const tip = document.getElementById('session-tooltip');
  tip.innerHTML = `
    <div class="tt-title">${escHtml(s.display_title)}</div>
    <div class="tt-meta">
      <span class="tt-state ${status}">${stateLabel}</span>
      <span>${escHtml(s.last_activity)}</span>
      <span>${escHtml(s.size)}</span>
    </div>`;
  tip.classList.add('visible');
  positionTooltip(e);
}

function onRowLeave() {
  const tip = document.getElementById('session-tooltip');
  tip.classList.remove('visible');
}

function onRowMove(e) {
  positionTooltip(e);
}

function positionTooltip(e) {
  const tip = document.getElementById('session-tooltip');
  const margin = 12;
  const vw = window.innerWidth, vh = window.innerHeight;
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  let x = e.clientX + margin;
  let y = e.clientY + margin;
  if (x + tw > vw - 8) x = e.clientX - tw - margin;
  if (y + th > vh - 8) y = e.clientY - th - margin;
  tip.style.left = x + 'px';
  tip.style.top  = y + 'px';
}

function setToolbarSession(id, titleText, isUntitled, customTitle) {
  const titleEl = document.getElementById('main-title');
  titleEl.textContent = titleText;
  titleEl.className = 'session-name' + (isUntitled ? ' untitled' : '');
  titleEl.dataset.customTitle = customTitle || '';
  titleEl.dataset.editable = id ? 'true' : 'false';
  titleEl.title = id ? 'Click to rename' : '';
  ['btn-autoname','btn-open','btn-open-gui','btn-delete','btn-duplicate','btn-continue','btn-summary','btn-extract','btn-export'].forEach(b => {
    document.getElementById(b).disabled = !id;
  });
  // btn-close enabled when session is running or open in GUI
  const btnClose = document.getElementById('btn-close');
  if (btnClose) btnClose.disabled = !id || (!runningIds.has(id) && !guiOpenSessions.has(id));
}

function startListInlineRename() {
  if (!activeId) return;

  // Find the active row's name cell in the list
  const activeRow = document.querySelector('.session-item.active');
  if (!activeRow) return;
  const nameCell = activeRow.querySelector('.session-col-name');
  if (!nameCell) return;

  const s = allSessions.find(x => x.id === activeId);
  const current = (s && (s.custom_title || s.display_title)) || '';
  const originalHTML = nameCell.innerHTML;

  // Replace cell content with an input
  const input = document.createElement('input');
  input.style.cssText = 'width:100%;background:#1a1a2e;border:1px solid #7c7cff;border-radius:4px;padding:2px 6px;color:#fff;font-size:12px;outline:none;';
  input.value = current;
  input.placeholder = 'Enter a name…';
  nameCell.innerHTML = '';
  nameCell.appendChild(input);

  // Prevent row click from firing while editing
  activeRow.onclick = null;
  input.focus();
  input.select();  // all text selected — edit in place or Delete to clear

  let committed = false;
  async function commit() {
    if (committed) return;
    committed = true;
    const val = input.value.trim();
    // Restore click handler
    activeRow.onclick = () => handleSessionClick(activeId);

    if (!val || val === current) {
      nameCell.innerHTML = originalHTML;
      return;
    }

    const resp = await fetch('/api/rename/' + activeId, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({title: val})
    });
    const data = await resp.json();
    if (data.ok) {
      if (s) { s.custom_title = data.title; s.display_title = data.title; }
      setToolbarSession(activeId, data.title, false, data.title);
      nameCell.textContent = data.title;
      showToast('Renamed to "' + data.title + '"');
    } else {
      nameCell.innerHTML = originalHTML;
      showToast(data.error || 'Rename failed', true);
    }
  }

  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); commit(); }
    if (e.key === 'Escape') { committed = true; activeRow.onclick = () => handleSessionClick(activeId); nameCell.innerHTML = originalHTML; }
  });
  input.addEventListener('blur', commit);
}

async function showSummary(id) {
  document.getElementById('summary-body').innerHTML = '<div style="color:#555;font-size:13px;"><span class="spinner"></span> Building summary…</div>';
  document.getElementById('summary-overlay').classList.add('show');

  const resp = await fetch('/api/summary/' + id);
  const data = await resp.json();
  document.getElementById('summary-body').innerHTML = data.html || ('<p style="color:#888">' + (data.error||'No summary available') + '</p>');
}

function closeSummary() {
  document.getElementById('summary-overlay').classList.remove('show');
}

document.getElementById('summary-overlay').addEventListener('click', function(e) {
  if (e.target === this) closeSummary();
});

async function duplicateSession(id) {
  const resp = await fetch('/api/duplicate/' + id, { method: 'POST' });
  const data = await resp.json();
  if (data.ok) {
    await loadSessions();
    showToast('Session duplicated');
  } else {
    showToast('Duplicate failed: ' + (data.error || 'unknown'), true);
  }
}

async function continueSession(id) {
  const btn = document.getElementById('btn-continue');
  btn.disabled = true; btn.textContent = 'Building…';

  const resp = await fetch('/api/continue/' + id, { method: 'POST' });
  const data = await resp.json();

  btn.disabled = false; btn.textContent = 'Continue Session';

  if (data.ok) {
    await loadSessions();
    // Select and open the new session
    await selectSession(data.new_id);
    showToast('New continuation session created — open it in Claude to continue');
  } else {
    showToast('Failed: ' + (data.error || 'unknown'), true);
  }
}

async function openInClaude(id) {
  const resp = await fetch('/api/open/' + id, { method: 'POST' });
  const data = await resp.json();
  if (data.ok) showToast('Opening session in Claude…');
  else showToast('Failed to open: ' + (data.error || 'unknown'), true);
}

function handleSessionClick(id) {
  if (id === activeId) { startListInlineRename(); } else { selectSession(id); }
}

async function handleNameClick(id) {
  if (id !== activeId) {
    await selectSession(id);   // first click — just select
  } else {
    startListInlineRename();   // second click on already-active row — rename
  }
}

async function selectSession(id) {
  activeId = id;
  // Stop live panel for a different session
  if (liveSessionId && liveSessionId !== id) stopLivePanel();
  filterSessions();

  setToolbarSession(id, 'Loading…', true, '');
  document.getElementById('main-body').innerHTML =
    '<div class="empty-state"><div class="spinner"></div></div>';

  const resp = await fetch('/api/session/' + id);
  const s = await resp.json();

  const titleText = s.custom_title || s.display_title;
  setToolbarSession(id, titleText, !s.custom_title, s.custom_title || '');

  // Single click always shows static preview; double click / openInGUI starts live panel
  document.getElementById('main-body').innerHTML =
    '<div class="conversation" id="convo">' + renderMessages(s.messages) + '</div>';
  setTimeout(() => {
    const convo = document.getElementById('convo');
    if (convo) convo.scrollTop = convo.scrollHeight;
  }, 50);
}

function renderMessages(messages) {
  if (!messages || !messages.length) return '<div style="color:#444;font-size:13px;">No messages</div>';
  return messages.map(m => {
    let body;
    if (m.role === 'assistant') {
      body = mdParse(m.content || '');
    } else {
      body = '<pre style="white-space:pre-wrap;margin:0;">' + escHtml(m.content || '(empty)') + '</pre>';
    }
    return `<div class="msg ${m.role}">
      <div class="msg-role">${m.role}</div>
      <div class="msg-body msg-content">${body}</div>
    </div>`;
  }).join('');
}

function openRename(id, currentTitle) {
  renameTarget = id;
  const input = document.getElementById('rename-input');
  input.value = currentTitle || '';
  document.getElementById('rename-overlay').classList.add('show');
  setTimeout(() => { input.focus(); input.select(); }, 50);
}

function closeRename() {
  document.getElementById('rename-overlay').classList.remove('show');
  renameTarget = null;
}

async function submitRename() {
  const title = document.getElementById('rename-input').value.trim();
  if (!title || !renameTarget) return;

  const resp = await fetch('/api/rename/' + renameTarget, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({title})
  });
  const data = await resp.json();
  closeRename();

  if (data.ok) {
    // Update local list
    const s = allSessions.find(x => x.id === renameTarget);
    if (s) { s.custom_title = data.title; s.display_title = data.title; }
    filterSessions();
    // Update toolbar title
    const titleEl = document.getElementById('main-title');
    if (titleEl) { titleEl.textContent = data.title; titleEl.classList.remove('untitled'); }
    showToast('Renamed to "' + data.title + '"');
  } else {
    showToast(data.error || 'Rename failed', true);
  }
}

async function autoName(id) {
  const btn = document.getElementById('autoname-btn');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>Naming…'; }

  let data;
  try {
    const resp = await fetch('/api/autonname/' + id, { method: 'POST' });
    data = await resp.json();
  } catch(e) {
    if (btn) { btn.disabled = false; btn.textContent = 'Auto-name'; }
    showToast('Auto-name failed: ' + e.message, true);
    return;
  }

  if (btn) { btn.disabled = false; btn.textContent = 'Auto-name'; }

  if (data.ok) {
    const s = allSessions.find(x => x.id === id);
    if (s) { s.custom_title = data.title; s.display_title = data.title; }
    filterSessions();
    const titleEl = document.getElementById('main-title');
    if (titleEl) { titleEl.textContent = data.title; titleEl.classList.remove('untitled'); }
    showToast('Auto-named: "' + data.title + '"');
  } else {
    showToast('Auto-name failed: ' + (data.error || 'unknown error'), true);
  }
}

async function deleteSession(id) {
  const s = allSessions.find(x => x.id === id);
  const name = (s && s.display_title) || id.slice(0, 8);
  if (!confirm('Delete "' + name + '"?\n\nThis cannot be undone.')) return;

  const resp = await fetch('/api/delete/' + id, { method: 'DELETE' });
  const data = await resp.json();

  if (data.ok) {
    allSessions = allSessions.filter(x => x.id !== id);
    activeId = null;
    filterSessions();
    document.getElementById('session-count').textContent = allSessions.length + ' sessions';
    setToolbarSession(null, 'No session selected', true, '');
    document.getElementById('main-body').innerHTML =
      '<div class="empty-state"><div class="icon">🗑</div><div>Session deleted</div></div>';
    showToast('Session deleted');
  } else {
    showToast('Delete failed', true);
  }
}

function showToast(msg, isError=false) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show' + (isError ? ' error' : '');
  setTimeout(() => { t.classList.remove('show'); }, 3000);
}

function escHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}

// Close modal on overlay click
document.getElementById('rename-overlay').addEventListener('click', function(e) {
  if (e.target === this) closeRename();
});
document.getElementById('git-sync-overlay').addEventListener('click', function(e) {
  if (e.target === this) closeGitSyncModal();
});

// --- Header System dropdown ---
function toggleHdrSys() {
  document.getElementById('hdr-sys-dropdown').classList.toggle('open');
}
function closeHdrSys() {
  document.getElementById('hdr-sys-dropdown').classList.remove('open');
}
document.addEventListener('click', function(e) {
  if (!document.getElementById('hdr-sys').contains(e.target)) closeHdrSys();
});

// --- Git Sync ---
let _gitStatus = {};

async function pollGitStatus() {
  try {
    const res = await fetch('/api/git-status');
    const s = await res.json();
    _gitStatus = s;
    const hasPush = s.ahead > 0 || s.uncommitted;
    const btnUpdate  = document.getElementById('btn-git-update');
    const btnPublish = document.getElementById('btn-git-publish');
    if (s.behind > 0) {
      document.getElementById('git-badge-pull').textContent = '\u2193';
      btnUpdate.style.display = 'inline-flex';
    } else {
      btnUpdate.style.display = 'none';
    }
    if (hasPush) {
      document.getElementById('git-badge-push').textContent = '\u2191';
      btnPublish.style.display = 'inline-flex';
    } else {
      btnPublish.style.display = 'none';
    }
  } catch(e) {}
}

function openGitPublish() {
  const s = _gitStatus;
  const hasPush = s.ahead > 0 || s.uncommitted;
  if (!hasPush) {
    showGitSyncModal('Publish App Update', '<p style="color:#aaa">Nothing to publish \u2014 your app is already up to date on remote.</p>',
      [{label:'OK', onclick: closeGitSyncModal}]);
    return;
  }
  let body = '<p>Your local changes are ready to publish.</p>'
    + '<p style="color:#888;font-size:12px">They will be saved and uploaded to remote automatically.</p>';
  if (s.behind > 0) {
    body += '<p style="color:#aaa;font-size:12px;margin-top:8px">'
      + s.behind + ' remote update(s) will be pulled in first, then your changes pushed.</p>';
  }
  showGitSyncModal('Publish App Update', body, [
    {label: 'Publish Now', primary: true, onclick: () => executeGitAction('both', 'btn-git-publish', 'Publish App Update')},
    {label: 'Cancel', onclick: closeGitSyncModal}
  ]);
}

function openGitUpdate() {
  const s = _gitStatus;
  if (s.behind === 0) {
    showGitSyncModal('Update App', '<p style="color:#aaa">Your app is already up to date.</p>',
      [{label:'OK', onclick: closeGitSyncModal}]);
    return;
  }
  showGitSyncModal('Update App', '<p><b style="color:#fff">' + s.behind + ' update(s)</b> are available from remote.</p>'
    + '<p style="color:#888;font-size:12px">Your app will be updated to the latest version. Your local changes are safe.</p>', [
    {label: 'Update Now', primary: true, onclick: () => executeGitAction('pull', 'btn-git-update', 'Update App')},
    {label: 'Cancel', onclick: closeGitSyncModal}
  ]);
}

function showGitSyncModal(title, body, btns) {
  document.getElementById('git-sync-title').textContent = title;
  document.getElementById('git-sync-body').innerHTML = body;
  const acts = document.getElementById('git-sync-actions');
  acts.innerHTML = '';
  btns.forEach(b => {
    const el = document.createElement('button');
    el.className = 'btn' + (b.primary ? ' primary' : '');
    el.textContent = b.label;
    el.onclick = b.onclick;
    acts.appendChild(el);
  });
  document.getElementById('git-sync-overlay').classList.add('show');
}

function closeGitSyncModal() {
  document.getElementById('git-sync-overlay').classList.remove('show');
}

async function executeGitAction(action, btnId, btnLabel) {
  closeGitSyncModal();
  const btn = document.getElementById(btnId);
  btn.disabled = true;
  try {
    const res = await fetch('/api/git-sync', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({action})
    });
    const r = await res.json();
    const body = '<ul style="margin:10px 0 0 16px;color:#bbb;">'
      + r.messages.map(m => '<li>' + escHtml(m) + '</li>').join('') + '</ul>';
    showGitSyncModal(r.ok ? btnLabel + ' \u2713' : 'Problem', body,
      [{label:'OK', primary:true, onclick: closeGitSyncModal}]);
    await pollGitStatus();
  } catch(e) {
    showGitSyncModal('Error', '<p style="color:#f88">Could not complete. Try again.</p>',
      [{label:'OK', onclick: closeGitSyncModal}]);
  } finally {
    btn.disabled = false;
  }
}

async function deleteEmptySessions() {
  const empty = allSessions.filter(s => s.message_count === 0);
  if (!empty.length) { showToast('No empty sessions found'); return; }
  if (!confirm(`Delete ${empty.length} empty session${empty.length > 1 ? 's' : ''}?`)) return;

  const resp = await fetch('/api/delete-empty', { method: 'DELETE' });
  const data = await resp.json();

  if (data.ok) {
    allSessions = allSessions.filter(s => s.message_count > 0);
    if (empty.find(s => s.id === activeId)) {
      activeId = null;
      setToolbarSession(null, 'No session selected', true, '');
      document.getElementById('main-body').innerHTML =
        '<div class="empty-state"><div class="icon">🗑</div><div>Sessions deleted</div></div>';
    }
    filterSessions();
    document.getElementById('session-count').textContent = allSessions.length + ' sessions';
    showToast(`Deleted ${data.deleted} empty session${data.deleted !== 1 ? 's' : ''}`);
  } else {
    showToast('Delete failed', true);
  }
}

// ---- Column resize ----
function initColResize() {
  document.querySelectorAll('.col-resize-grip').forEach(grip => {
    grip.addEventListener('mousedown', e => {
      e.stopPropagation();
      const col = grip.dataset.col;
      const startX = e.clientX;
      const sidebar = document.querySelector('.sidebar');

      // Get current pixel widths from the computed grid
      const computed = getComputedStyle(sidebar);
      const gridCols = getComputedStyle(document.querySelector('.col-header-row'))
        .gridTemplateColumns.split(' ').map(v => parseFloat(v));
      const [wName, wDate, wSize] = gridCols;

      const startVal = col === 'name' ? wName : wDate;

      grip.classList.add('dragging');
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';

      function onMove(ev) {
        const delta = ev.clientX - startX;
        const newVal = Math.max(60, startVal + delta);
        if (col === 'name') {
          document.documentElement.style.setProperty('--col-name', newVal + 'px');
        } else {
          document.documentElement.style.setProperty('--col-date', newVal + 'px');
        }
      }
      function onUp() {
        grip.classList.remove('dragging');
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
      }
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  });
}

// ---- Sidebar resize ----
(function() {
  const handle = document.getElementById('resize-handle');
  const sidebar = document.querySelector('.sidebar');
  let dragging = false, startX = 0, startW = 0;

  handle.addEventListener('mousedown', e => {
    dragging = true;
    startX = e.clientX;
    startW = sidebar.offsetWidth;
    handle.classList.add('dragging');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  });

  document.addEventListener('mousemove', e => {
    if (!dragging) return;
    const newW = Math.min(600, Math.max(180, startW + e.clientX - startX));
    document.documentElement.style.setProperty('--sidebar-w', newW + 'px');
  });

  document.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove('dragging');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
  });
})();

// ---- Group dropdowns ----
let _activeGrpPopup = null;

function toggleGrpDropdown(grpId) {
  const grp = document.getElementById(grpId);
  const label = grp.querySelector('.btn-group-label');

  // Close any existing popup
  if (_activeGrpPopup) {
    _activeGrpPopup.remove();
    const prevLabel = document.querySelector('.btn-group-label.grp-open');
    if (prevLabel) prevLabel.classList.remove('grp-open');
    if (_activeGrpPopup._grpId === grpId) { _activeGrpPopup = null; return; }
    _activeGrpPopup = null;
  }

  label.classList.add('grp-open');

  // Clone the btn-group-inner buttons into a floating popup
  const inner = grp.querySelector('.btn-group-inner');
  const popup = document.createElement('div');
  popup.className = 'grp-popup';
  popup._grpId = grpId;

  Array.from(inner.children).forEach(btn => {
    const clone = btn.cloneNode(true);
    clone.style.removeProperty('display');
    // Wire the onclick — copy the attribute
    const oc = btn.getAttribute('onclick');
    if (oc) clone.setAttribute('onclick', oc);
    clone.addEventListener('click', () => { closeAllGrpDropdowns(); });
    popup.appendChild(clone);
  });

  // Position below the label
  const rect = label.getBoundingClientRect();
  popup.style.top  = (rect.bottom + 4) + 'px';
  popup.style.left = rect.left + 'px';
  document.body.appendChild(popup);
  _activeGrpPopup = popup;
}

function closeAllGrpDropdowns() {
  if (_activeGrpPopup) { _activeGrpPopup.remove(); _activeGrpPopup = null; }
  document.querySelectorAll('.btn-group-label.grp-open').forEach(l => l.classList.remove('grp-open'));
}

// Close popup when clicking outside
document.addEventListener('click', e => {
  if (!_activeGrpPopup) return;
  if (e.target.closest('.grp-popup') || e.target.closest('.btn-group-label')) return;
  closeAllGrpDropdowns();
});

// ---- Waiting-for-input polling ----
let waitingData = {};   // { session_id: {question, options, kind} }
let respondTarget = null;

async function pollWaiting() {
  try {
    const resp = await fetch('/api/waiting');
    const list = await resp.json();
    if (!Array.isArray(list)) throw new Error('bad response');
    const newWaiting = {};
    const newRunning = new Set();
    const newKinds = {};
    list.forEach(w => {
      newRunning.add(w.id);
      newKinds[w.id] = w.kind;   // 'question' | 'working' | 'idle'
      if (w.kind === 'question') newWaiting[w.id] = w;
    });

    // Auto-send queued input when Claude transitions from working → idle
    if (liveSessionId && liveQueuedText) {
      const wasWorking = (sessionKinds[liveSessionId] === 'working');
      const nowIdle    = (newKinds[liveSessionId] === 'idle');
      if (wasWorking && nowIdle) {
        const textToSend = liveQueuedText;
        liveQueuedText = '';
        showToast('Sending queued command\u2026');
        fetch('/api/respond/' + liveSessionId, {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({text: textToSend})
        }).then(r => r.json()).then(d => {
          if (d.method !== 'sent') showToast('Queue send failed', true);
        });
      }
    }

    // Update row state classes (4 states: si-question, si-working, si-idle, or none)
    document.querySelectorAll('.session-item[data-sid]').forEach(row => {
      const id = row.dataset.sid;
      row.classList.remove('si-question', 'si-working', 'si-idle');
      if (newRunning.has(id)) row.classList.add('si-' + (newKinds[id] || 'working'));
    });

    waitingData = newWaiting;
    runningIds  = newRunning;
    sessionKinds = newKinds;

    // If currently showing a popup for a session that is no longer waiting, close it
    if (respondTarget && !waitingData[respondTarget]) closeRespond();

    // Re-render workforce view if visible (to update status indicators)
    if (viewMode === 'workforce') filterSessions();

    // Update live panel input bar state
    if (liveSessionId) updateLiveInputBar();

    // Update Close Session button enabled state
    const btnClose = document.getElementById('btn-close');
    if (btnClose && activeId) btnClose.disabled = !newRunning.has(activeId) && !guiOpenSessions.has(activeId);

  } catch(e) {}
  finally {
    // Schedule next poll only after this one finishes — avoids overlap if WMI is slow
    setTimeout(pollWaiting, 2000);
  }
}

// ---- Live Terminal Panel ----
let liveSessionId = null;
let liveLineCount = 0;
let livePollTimer = null;
let liveAutoScroll = true;
let liveQueuedText = '';

function startLivePanel(id) {
  stopLivePanel();
  liveSessionId = id;
  liveLineCount = 0;
  liveAutoScroll = true;
  liveQueuedText = '';
  liveBarState = null;  // force fresh render

  document.getElementById('main-body').innerHTML =
    '<div class="live-panel" id="live-panel">' +
    '<div class="live-log" id="live-log"></div>' +
    '<div class="live-input-bar" id="live-input-bar">' +
    '<div class="live-working"><span class="spinner"></span>Loading session\u2026</div>' +
    '</div></div>';

  const logEl = document.getElementById('live-log');
  logEl.addEventListener('scroll', () => {
    const atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 60;
    liveAutoScroll = atBottom;
  });

  const btnClose = document.getElementById('btn-close');
  if (btnClose) btnClose.disabled = false;

  fetchLiveLog();
}

function stopLivePanel() {
  if (livePollTimer) { clearTimeout(livePollTimer); livePollTimer = null; }
  liveSessionId = null;
  liveBarState = null;
  const btnClose = document.getElementById('btn-close');
  if (btnClose) btnClose.disabled = true;
}

async function fetchLiveLog() {
  if (!liveSessionId) return;
  const id = liveSessionId;
  try {
    const r = await fetch('/api/session-log/' + id + '?since=' + liveLineCount);
    if (!r.ok) throw new Error('bad response');
    const d = await r.json();
    if (liveSessionId !== id) return;  // switched away

    const logEl = document.getElementById('live-log');
    if (!logEl) return;

    if (d.entries && d.entries.length) {
      d.entries.forEach(e => logEl.appendChild(renderLiveEntry(e)));
    }
    liveLineCount = d.total_lines || liveLineCount;

    if (liveAutoScroll) logEl.scrollTop = logEl.scrollHeight;

    updateLiveInputBar();
  } catch(e) {}
  finally {
    if (liveSessionId === id) {
      livePollTimer = setTimeout(fetchLiveLog, 2000);
    }
  }
}

function renderLiveEntry(e) {
  const div = document.createElement('div');
  div.className = 'live-entry';

  if (e.kind === 'user' || e.kind === 'asst') {
    div.classList.add(e.kind === 'user' ? 'live-entry-user' : 'live-entry-asst');
    const LIMIT = e.kind === 'asst' ? 600 : 800;
    const labelDiv = document.createElement('div');
    labelDiv.className = 'live-label';
    labelDiv.textContent = e.kind === 'user' ? 'You' : 'Claude';
    div.appendChild(labelDiv);

    const text = e.text || '';
    const textDiv = document.createElement('div');
    textDiv.className = 'live-text';
    const displayText = text.length > LIMIT ? text.slice(0, LIMIT) : text;
    if (e.kind === 'asst') {
      textDiv.innerHTML = mdParse(displayText);
    } else {
      textDiv.textContent = displayText;
    }
    div.appendChild(textDiv);

    if (text.length > LIMIT) {
      const btn = document.createElement('button');
      btn.className = 'live-expand-btn';
      btn.textContent = '\u2026 show more';
      btn.onclick = () => {
        if (e.kind === 'asst') { textDiv.innerHTML = mdParse(text); }
        else { textDiv.textContent = text; }
        btn.remove();
      };
      div.appendChild(btn);
    }

  } else if (e.kind === 'tool_use') {
    div.classList.add('live-entry-tool');
    const toolLine = document.createElement('div');
    toolLine.className = 'live-tool-line';

    const icon = document.createElement('span');
    icon.className = 'live-tool-icon';
    icon.textContent = '\u2699';

    const nameEl = document.createElement('span');
    nameEl.className = 'live-tool-name';
    nameEl.textContent = e.name || 'tool';

    const descEl = document.createElement('span');
    descEl.className = 'live-tool-desc';
    descEl.textContent = (e.desc || '').slice(0, 120);

    const toggle = document.createElement('button');
    toggle.className = 'live-expand-btn';
    toggle.textContent = '\u25be';

    toolLine.appendChild(icon);
    toolLine.appendChild(nameEl);
    toolLine.appendChild(descEl);
    toolLine.appendChild(toggle);

    const detail = document.createElement('div');
    detail.className = 'live-tool-detail';
    detail.textContent = e.desc || '';

    toolLine.onclick = () => detail.classList.toggle('open');
    div.appendChild(toolLine);
    div.appendChild(detail);

  } else if (e.kind === 'tool_result') {
    div.classList.add('live-entry-result');
    const ok = !e.is_error;
    const text = e.text || '';

    const line = document.createElement('div');
    line.className = 'live-result-line ' + (ok ? 'live-result-ok' : 'live-result-err');
    line.textContent = (ok ? '\u2713 ' : '\u2717 ') + text.slice(0, 80) + (text.length > 80 ? '\u2026' : '');

    const detail = document.createElement('div');
    detail.className = 'live-tool-detail';
    detail.textContent = text;

    line.onclick = () => detail.classList.toggle('open');
    div.appendChild(line);
    div.appendChild(detail);
  }

  return div;
}

let liveBarState = null;   // 'ended' | 'question:<questionText>' | 'idle' | 'working'
let _guiFocusPending = false;
let guiOpenSessions = new Set(JSON.parse(localStorage.getItem('guiOpenSessions') || '[]'));  // persists across reloads

function guiOpenAdd(id) {
  guiOpenSessions.add(id);
  localStorage.setItem('guiOpenSessions', JSON.stringify([...guiOpenSessions]));
}
function guiOpenDelete(id) {
  guiOpenSessions.delete(id);
  localStorage.setItem('guiOpenSessions', JSON.stringify([...guiOpenSessions]));
}

async function openInGUI(id) {
  _guiFocusPending = true;
  closeAllGrpDropdowns();
  activeId = id;
  guiOpenAdd(id);  // track as GUI-open so we show idle state (persisted)
  if (liveSessionId && liveSessionId !== id) stopLivePanel();
  filterSessions();

  setToolbarSession(id, 'Loading…', true, '');
  const resp = await fetch('/api/session/' + id);
  const s = await resp.json();
  setToolbarSession(id, s.custom_title || s.display_title, !s.custom_title, s.custom_title || '');

  startLivePanel(id);
}

function updateLiveInputBar() {
  if (!liveSessionId) return;
  const id = liveSessionId;
  const bar = document.getElementById('live-input-bar');
  if (!bar) return;

  const kind = sessionKinds[id];  // 'question' | 'working' | 'idle' | undefined
  const isRunning = runningIds.has(id);
  const wd = waitingData[id];     // {question, options, kind} or undefined

  // Compute a state key — for question state, include question text so we re-render if the question changed
  let stateKey;
  if (!isRunning) stateKey = 'ended';
  else if (kind === 'question') stateKey = 'question:' + (wd ? wd.question || '' : '');
  else if (kind === 'idle') stateKey = 'idle';
  else stateKey = 'working';

  // Don't re-render if the bar is already showing this exact state.
  // This is critical: prevents the 2s poll from wiping user's in-progress typed text.
  if (stateKey === liveBarState) return;
  liveBarState = stateKey;

  if (!isRunning) {
    bar.innerHTML =
      '<div class="live-ended" style="margin-bottom:6px;">' +
      '<span style="color:#555;font-size:11px;">Session ended \u2014 start a new message to continue</span>' +
      '</div>' +
      '<textarea id="live-input-ta" class="live-textarea" rows="2" placeholder="Type a message to start a new session\u2026"' +
      ' onkeydown="if(event.key===\'Enter\'&&(event.ctrlKey||event.metaKey))liveSubmitContinue(\'' + id + '\')"></textarea>' +
      '<div class="live-bar-row">' +
      '<span style="font-size:10px;color:#444;">Ctrl+Enter to send</span>' +
      '<button class="live-send-btn" onclick="liveSubmitContinue(\'' + id + '\')">Send \u21b5</button>' +
      '</div>';
    const btnClose = document.getElementById('btn-close');
    if (btnClose) btnClose.disabled = true;
    if (_guiFocusPending) {
      _guiFocusPending = false;
      setTimeout(() => {
        const logEl = document.getElementById('live-log');
        if (logEl) logEl.scrollTop = logEl.scrollHeight;
        const ta = document.getElementById('live-input-ta');
        if (ta) ta.focus();
      }, 50);
    }

  } else if (kind === 'question') {
    // Claude is asking something — show question text + option buttons + free-form textarea
    const prefill = liveQueuedText;
    liveQueuedText = '';
    const questionText = (wd && wd.question) ? wd.question : '';
    const options = (wd && wd.options) ? wd.options : null;

    // Render question bubble
    let questionHTML = '';
    if (questionText) {
      // Escape and show last ~400 chars of question (truncate top if very long)
      const display = questionText.length > 400 ? '\u2026' + questionText.slice(-400) : questionText;
      questionHTML = '<div class="live-question-text">' + escHtml(display) + '</div>';
    }

    // Render option buttons (y/n/a, yes/no, or numbered list)
    const isTool = (wd && wd.kind === 'tool');
    const optLabels = { y: '\u2713 Yes', n: '\u2717 No', a: '\u2605 Always', yes: '\u2713 Yes', no: '\u2717 No' };
    let optBtns = '';
    if (options && options.length) {
      optBtns = '<div class="live-option-btns">' +
        options.map((opt) => {
          // Numbered option: "1. Do X" → label = "1. Do X", send = "1"
          // Single token option: "y" / "n" / "yes" / "no" / "a" → expand label for tool prompts
          const isNumbered = /^\d+\./.test(opt);
          const sendVal = isNumbered ? opt.match(/^(\d+)\./)[1] : opt;
          const label = (!isNumbered && isTool && optLabels[opt.toLowerCase()])
            ? optLabels[opt.toLowerCase()] : escHtml(opt);
          const safeVal = sendVal.replace(/\\/g,'\\\\').replace(/'/g,"\\'");
          return '<button class="live-opt-btn" onclick="livePickOption(\'' + safeVal + '\')">' + label + '</button>';
        }).join('') +
      '</div>';
    }

    bar.innerHTML =
      '<div class="live-waiting-label">\uD83D\uDCAC Claude has a question</div>' +
      questionHTML +
      optBtns +
      '<textarea id="live-input-ta" class="live-textarea waiting-focus" rows="2" placeholder="Type your response\u2026 (or click an option above)"' +
      ' onkeydown="if(event.key===\'Enter\'&&(event.ctrlKey||event.metaKey))liveSubmitWaiting()"></textarea>' +
      '<div class="live-bar-row">' +
      '<span style="font-size:10px;color:#554400;">Ctrl+Enter to send</span>' +
      '<button class="live-send-btn waiting" onclick="liveSubmitWaiting()">Send \u21b5</button>' +
      '</div>';
    const ta = document.getElementById('live-input-ta');
    if (ta) {
      if (prefill) ta.value = prefill;
      const shouldFocus = _guiFocusPending || true;
      if (shouldFocus) {
        _guiFocusPending = false;
        setTimeout(() => {
          const logEl = document.getElementById('live-log');
          if (logEl) logEl.scrollTop = logEl.scrollHeight;
          ta.focus();
        }, 50);
      }
    }

  } else if (kind === 'idle') {
    bar.innerHTML =
      '<textarea id="live-input-ta" class="live-textarea" rows="2" placeholder="Type your next command\u2026"' +
      ' onkeydown="if(event.key===\'Enter\'&&(event.ctrlKey||event.metaKey))liveSubmitIdle()"></textarea>' +
      '<div class="live-bar-row">' +
      '<span style="font-size:10px;color:#444;">Ctrl+Enter to send</span>' +
      '<button class="live-send-btn" onclick="liveSubmitIdle()">Send \u21b5</button>' +
      '</div>';
    if (_guiFocusPending) {
      _guiFocusPending = false;
      setTimeout(() => {
        const logEl = document.getElementById('live-log');
        if (logEl) logEl.scrollTop = logEl.scrollHeight;
        const ta = document.getElementById('live-input-ta');
        if (ta) ta.focus();
      }, 50);
    }

  } else {
    bar.innerHTML =
      '<div class="live-working" style="margin-bottom:6px;"><span class="spinner"></span>Claude is working\u2026</div>' +
      '<textarea id="live-queue-ta" class="live-textarea" rows="2" ' +
      'style="opacity:0.6;" placeholder="Type your next command \u2014 will send when Claude finishes\u2026">' +
      (liveQueuedText ? escHtml(liveQueuedText) : '') +
      '</textarea>' +
      '<div class="live-bar-row">' +
      '<span id="live-queue-hint" style="font-size:10px;color:#555;">' +
      (liveQueuedText ? '\u23f3 Command queued' : 'Will send automatically when done') +
      '</span>' +
      '<button class="live-send-btn" style="background:#2a2a2a;color:#666;border-color:#333;" onclick="liveQueueSave()">Queue</button>' +
      '<button class="live-send-btn" style="background:#1a0000;color:#664444;border-color:#330000;margin-left:2px;" onclick="liveClearQueue()" title="Cancel queued command">\u2715</button>' +
      '</div>';
    const qta = document.getElementById('live-queue-ta');
    if (qta) {
      qta.addEventListener('input', () => {
        liveQueuedText = qta.value;
        const hint = document.getElementById('live-queue-hint');
        if (hint) hint.textContent = qta.value.trim() ? '\u23f3 Command queued' : 'Will send automatically when done';
      });
    }
  }
}

function livePickOption(val) {
  // Fill the textarea with the option value and submit
  const ta = document.getElementById('live-input-ta');
  if (ta) ta.value = val;
  liveSubmitWaiting();
}

function liveQueueSave() {
  const ta = document.getElementById('live-queue-ta');
  if (ta) {
    liveQueuedText = ta.value.trim();
    showToast(liveQueuedText ? 'Command queued \u2014 will send when Claude finishes' : 'Queue cleared');
  }
}

function liveClearQueue() {
  liveQueuedText = '';
  const ta = document.getElementById('live-queue-ta');
  if (ta) ta.value = '';
  const hint = document.getElementById('live-queue-hint');
  if (hint) hint.textContent = 'Will send automatically when done';
  showToast('Queue cleared');
}

async function liveSubmitIdle() {
  const ta = document.getElementById('live-input-ta');
  if (!ta || !liveSessionId) return;
  const text = ta.value.trim();
  if (!text) return;
  ta.disabled = true;
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 8000);
    const r = await fetch('/api/respond/' + liveSessionId, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text}), signal: ctrl.signal
    });
    clearTimeout(timer);
    const d = await r.json();
    if (d.method === 'sent') {
      ta.value = '';
      setTimeout(pollWaiting, 500);
    } else if (d.method === 'clipboard') {
      alert(d.message);
    } else {
      alert('Send failed: ' + (d.err || d.method));
    }
  } catch(e) {
    if (e.name === 'AbortError') alert('Timed out \u2014 copied to clipboard. Paste in your terminal.');
    else alert('Error: ' + e.message);
  } finally {
    if (ta) ta.disabled = false;
  }
}

async function liveSubmitContinue(fromId) {
  const ta = document.getElementById('live-input-ta');
  const text = ta ? ta.value.trim() : '';
  // Continue the session (creates new session), then send the typed text
  const resp = await fetch('/api/continue/' + fromId, { method: 'POST' });
  const data = await resp.json();
  if (!data.ok) { showToast('Could not continue session'); return; }
  await loadSessions();
  _guiFocusPending = true;
  await openInGUI(data.new_id);
  if (text) {
    // Wait for the new session to start, then send the text
    setTimeout(async () => {
      const r = await fetch('/api/respond/' + data.new_id, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text})
      });
    }, 1500);
  }
}

async function liveSubmitWaiting() {
  const ta = document.getElementById('live-input-ta');
  if (!ta || !liveSessionId) return;
  const text = ta.value.trim();
  if (!text) return;
  ta.disabled = true;
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 8000);
    const r = await fetch('/api/respond/' + liveSessionId, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text}), signal: ctrl.signal
    });
    clearTimeout(timer);
    const d = await r.json();
    if (d.method === 'sent') {
      ta.value = '';
      liveBarState = null;  // force bar to re-render next poll (question → working)
      setTimeout(pollWaiting, 500);
    } else if (d.method === 'clipboard') {
      alert(d.message);
    } else {
      alert('Send failed: ' + (d.err || d.method));
    }
  } catch(e) {
    if (e.name === 'AbortError') alert('Timed out \u2014 response copied to clipboard. Paste in your terminal.');
    else alert('Error: ' + e.message);
  } finally {
    if (ta) ta.disabled = false;
  }
}

async function closeSession(id) {
  if (!id) return;
  const s = allSessions.find(x => x.id === id);
  const name = (s && s.display_title) || id.slice(0, 8);
  if (!confirm('Close session "' + name + '"?\n\nThis will stop the running Claude process and close the terminal window.')) return;
  // Attempt to kill the process (may already be stopped — that's fine)
  if (runningIds.has(id)) {
    const r = await fetch('/api/close/' + id, { method: 'POST' });
    const d = await r.json();
    if (!d.ok) showToast('Process stop: ' + (d.error || 'unknown'));
  }
  // Always clear GUI state and show static preview
  stopLivePanel();
  guiOpenDelete(id);
  runningIds.delete(id);
  showToast('Session closed');
  const sr = await fetch('/api/session/' + id);
  const sess = await sr.json();
  document.getElementById('main-body').innerHTML =
    '<div class="conversation" id="convo">' + renderMessages(sess.messages) + '</div>';
  setTimeout(() => { const c = document.getElementById('convo'); if (c) c.scrollTop = c.scrollHeight; }, 50);
  filterSessions();
}

// ---- Workforce mode helpers ----
let sessionKinds = {};   // session_id → 'question' | 'working' | 'idle'

function getSessionStatus(id) {
  if (!runningIds.has(id)) {
    // Sessions opened in GUI panel are considered idle even if no OS process detected
    if (guiOpenSessions.has(id)) return 'idle';
    return 'sleeping';
  }
  return sessionKinds[id] || 'working';
}

function setViewMode(mode) {
  viewMode = mode;
  localStorage.setItem('viewMode', mode);
  const listEl = document.getElementById('session-list');
  const gridEl = document.getElementById('workforce-grid');
  const sortBar = document.getElementById('wf-sort-bar');
  const btnList = document.getElementById('btn-view-list');
  const btnWf   = document.getElementById('btn-view-workforce');
  if (mode === 'workforce') {
    listEl.style.display = 'none';
    gridEl.classList.add('visible');
    sortBar.style.display = 'flex';
    if (btnList) btnList.classList.remove('active');
    if (btnWf)   btnWf.classList.add('active');
  } else {
    listEl.style.display = '';
    gridEl.classList.remove('visible');
    sortBar.style.display = 'none';
    if (btnList) btnList.classList.add('active');
    if (btnWf)   btnWf.classList.remove('active');
  }
  filterSessions();
}

function setWfSort(sort) {
  wfSort = sort;
  localStorage.setItem('wfSort', sort);
  ['status','recent','name'].forEach(s => {
    const btn = document.getElementById('wf-btn-' + s);
    if (btn) btn.classList.toggle('active', s === sort);
  });
  filterSessions();
}

function wfSortedSessions(sessions) {
  const copy = [...sessions];
  const statusOrder = {question:0, working:1, idle:2, sleeping:3};
  if (wfSort === 'status') {
    copy.sort((a, b) => {
      const sa = statusOrder[getSessionStatus(a.id)] ?? 3;
      const sb = statusOrder[getSessionStatus(b.id)] ?? 3;
      if (sa !== sb) return sa - sb;
      return (b.last_activity_ts||b.sort_ts||0) - (a.last_activity_ts||a.sort_ts||0);
    });
  } else if (wfSort === 'name') {
    copy.sort((a, b) => (a.display_title||'').localeCompare(b.display_title||''));
  } else {
    // recent
    copy.sort((a, b) => (b.last_activity_ts||b.sort_ts||0) - (a.last_activity_ts||a.sort_ts||0));
  }
  return copy;
}

function renderWorkforce(sessions) {
  const grid = document.getElementById('workforce-grid');
  if (!sessions.length) {
    grid.innerHTML = '<div style="padding:20px;color:#444;font-size:12px;">No sessions found</div>';
    return;
  }
  const statusEmoji = {question:'&#x1F64B;', working:'&#x26CF;&#xFE0F;', idle:'&#x1F4BB;', sleeping:'&#x1F634;'};
  const statusLabel = {question:'Question', working:'Working', idle:'Idle', sleeping:'Sleeping'};
  grid.innerHTML = sessions.map(s => {
    const st = getSessionStatus(s.id);
    const emoji = statusEmoji[st] || '&#x1F634;';
    const label = statusLabel[st] || 'Sleeping';
    const selClass = s.id === activeId ? ' wf-selected' : '';
    const name = escHtml((s.display_title||s.id).slice(0,22) + ((s.display_title||'').length>22?'\u2026':''));
    const date = (s.last_activity||'').split('  ')[0] || '';
    return `<div class="wf-card wf-${st}${selClass}" onclick="singleOrDouble('${s.id}',event)" title="${escHtml(s.display_title)} — double-click to open in Claude Code GUI">
      <div class="wf-avatar">${emoji}</div>
      <div class="wf-status-label">${label}</div>
      <div class="wf-name">${name}</div>
      <div class="wf-meta">${escHtml(date)}</div>
    </div>`;
  }).join('');
}

let _clickTimer = null;
let _lastClickId = null;
let _lastClickTime = 0;

function singleOrDouble(id, e) {
  const now = Date.now();
  const isDouble = (_lastClickId === id && now - _lastClickTime < 400);
  _lastClickId = id;
  _lastClickTime = now;
  if (isDouble) {
    // Double click — cancel pending single-click and open in GUI
    if (_clickTimer) { clearTimeout(_clickTimer); _clickTimer = null; }
    openInGUI(id);
  } else {
    // Single click — delay so double-click can cancel it
    if (_clickTimer) clearTimeout(_clickTimer);
    _clickTimer = setTimeout(() => { _clickTimer = null; selectSession(id); }, 400);
  }
}

function wfCardClick(id) {
  selectSession(id);
}

function openRespond(id) {
  const w = waitingData[id];
  if (!w) return;
  respondTarget = id;

  // Question text
  document.getElementById('respond-question').textContent = w.question || '(no question text)';

  // Option buttons
  const optsEl = document.getElementById('respond-options');
  const orEl   = document.getElementById('respond-or');
  optsEl.innerHTML = '';
  if (w.options && w.options.length) {
    w.options.forEach(opt => {
      const btn = document.createElement('button');
      btn.className = 'respond-opt';
      btn.textContent = opt;
      btn.onclick = () => sendRespond(opt);
      optsEl.appendChild(btn);
    });
    orEl.style.display = 'block';
  } else {
    orEl.style.display = 'none';
  }

  document.getElementById('respond-input').value = '';
  document.getElementById('respond-overlay').classList.add('open');
  // Scroll question to bottom so the most recent part (the actual ask) is visible
  setTimeout(() => {
    const qEl = document.getElementById('respond-question');
    qEl.scrollTop = qEl.scrollHeight;
    document.getElementById('respond-input').focus();
  }, 60);
}

function closeRespond() {
  document.getElementById('respond-overlay').classList.remove('open');
  respondTarget = null;
}

async function sendRespond(text) {
  if (!text || !respondTarget) return;
  const sendBtn = document.getElementById('respond-send');
  sendBtn.disabled = true; sendBtn.textContent = 'Sending…';
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 8000);
    const r = await fetch('/api/respond/' + respondTarget, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({text}), signal: ctrl.signal
    });
    clearTimeout(timer);
    const d = await r.json();
    if (d.method === 'sent') {
      closeRespond();
      setTimeout(pollWaiting, 1000);
    } else if (d.method === 'clipboard') {
      closeRespond();
      alert(d.message);
    } else {
      // Debug: show what went wrong
      alert('Send failed (rc=' + d.rc + '): ' + (d.err || d.method));
    }
  } catch(e) {
    if (e.name === 'AbortError') alert('Timed out — response copied to clipboard. Switch to your terminal and paste.');
    else alert('Error: ' + e.message);
  }
  finally { sendBtn.disabled = false; sendBtn.textContent = 'Send ↵'; }
}

async function submitRespond() {
  const text = document.getElementById('respond-input').value.trim();
  if (text) await sendRespond(text);
}

document.getElementById('respond-overlay').addEventListener('click', function(e) {
  if (e.target === this) closeRespond();
});

pollWaiting(); // self-rescheduling every 2s via finally block

// ---- Extract Code ----
let extractBlocks = [];

async function openExtract() {
  if (!activeId) return;
  const body = document.getElementById('extract-body');
  body.innerHTML = '<p style="padding:20px;color:#555;font-size:12px;">Loading\u2026</p>';
  document.getElementById('extract-drawer').classList.add('open');
  try {
    const r = await fetch('/api/extract-code/' + activeId);
    const d = await r.json();
    extractBlocks = d.blocks || [];
    renderExtractBlocks(extractBlocks);
  } catch(e) {
    body.innerHTML = '<p style="padding:20px;color:#cc4444;font-size:12px;">Error loading code blocks.</p>';
  }
}

function closeExtract() {
  document.getElementById('extract-drawer').classList.remove('open');
}

function renderExtractBlocks(blocks) {
  const body = document.getElementById('extract-body');
  const copyAll = document.getElementById('extract-copy-all');
  if (!blocks.length) {
    body.innerHTML = '<p style="padding:20px;color:#555;font-size:13px;text-align:center;">No code blocks found in this session.</p>';
    copyAll.style.display = 'none';
    return;
  }
  copyAll.style.display = 'inline-block';
  body.innerHTML = blocks.map((b, i) => {
    const langBadge = b.is_shell
      ? `<span class="code-shell-badge">${escHtml(b.language||'shell')}</span>`
      : `<span class="code-lang-badge">${escHtml(b.language||'text')}</span>`;
    const dupBadge = b.duplicate_of !== null && b.duplicate_of !== undefined
      ? `<span class="code-dup-badge">duplicate of #${b.duplicate_of+1}</span>` : '';
    const fname = b.inferred_filename ? `<span class="code-filename">${escHtml(b.inferred_filename)}</span>` : '';
    const preview = escHtml((b.content||'').slice(0, 2000));
    return `<div class="code-block-card">
      <div class="code-block-header">
        <div style="display:flex;gap:6px;align-items:center;">${langBadge}${dupBadge}${fname}</div>
        <button class="code-copy-btn" onclick="copyBlock(${i})">Copy</button>
      </div>
      <pre class="code-block-pre">${preview}</pre>
    </div>`;
  }).join('');
}

function copyBlock(i) {
  const b = extractBlocks[i];
  if (!b) return;
  navigator.clipboard.writeText(b.content).then(() => {
    const btns = document.querySelectorAll('.code-copy-btn');
    if (btns[i]) { btns[i].textContent = 'Copied!'; setTimeout(() => btns[i].textContent = 'Copy', 1200); }
  });
}

document.getElementById('extract-copy-all').addEventListener('click', () => {
  const all = extractBlocks.map((b, i) => `// --- Block ${i+1}: ${b.inferred_filename||b.language||'code'} ---\n${b.content}`).join('\n\n');
  navigator.clipboard.writeText(all).then(() => {
    const btn = document.getElementById('extract-copy-all');
    btn.textContent = 'Copied!'; setTimeout(() => btn.textContent = 'Copy All', 1500);
  });
});

document.getElementById('extract-export-btn').addEventListener('click', () => {
  if (activeId) triggerExport();
});

// ---- Export Project ----
function triggerExport() {
  if (!activeId) return;
  const a = document.createElement('a');
  a.href = '/api/export-project/' + activeId;
  a.download = 'session_export.zip';
  a.click();
}

// ---- Find ----
let findMatches = [];
let findCurrent = -1;

function openFind() {
  document.getElementById('find-bar').classList.add('open');
  document.getElementById('find-input').focus();
}

function closeFind() {
  clearFindHighlights();
  document.getElementById('find-bar').classList.remove('open');
  document.getElementById('find-input').value = '';
  document.getElementById('find-count').textContent = '';
  findMatches = []; findCurrent = -1;
}

function runFind() {
  clearFindHighlights();
  findMatches = []; findCurrent = -1;
  const q = document.getElementById('find-input').value;
  if (!q || q.length < 2) { document.getElementById('find-count').textContent = ''; return; }

  const msgEls = document.querySelectorAll('.msg-content');

  // Highlight all matches using a regex replacement
  const re = new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
  msgEls.forEach(el => {
    if (el.textContent.toLowerCase().includes(q.toLowerCase())) {
      el.innerHTML = el.innerHTML.replace(re, m => `<mark class="find-match">${escHtml(m)}</mark>`);
    }
  });

  const allMarks = document.querySelectorAll('mark.find-match');
  findMatches = Array.from(allMarks);
  document.getElementById('find-count').textContent = findMatches.length ? `1 / ${findMatches.length}` : 'No matches';
  if (findMatches.length) { findCurrent = 0; highlightCurrent(); }
}

function clearFindHighlights() {
  document.querySelectorAll('mark.find-match').forEach(m => {
    m.outerHTML = m.textContent;
  });
}

function highlightCurrent() {
  document.querySelectorAll('mark.find-match').forEach((m, i) => {
    m.classList.toggle('current', i === findCurrent);
  });
  if (findMatches[findCurrent]) findMatches[findCurrent].scrollIntoView({block:'center', behavior:'smooth'});
  document.getElementById('find-count').textContent = `${findCurrent+1} / ${findMatches.length}`;
}

function findNav(dir) {
  if (!findMatches.length) return;
  findCurrent = (findCurrent + dir + findMatches.length) % findMatches.length;
  highlightCurrent();
}

function findKeyNav(e) {
  if (e.key === 'Enter') findNav(e.shiftKey ? -1 : 1);
  if (e.key === 'Escape') closeFind();
}

document.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'f') {
    e.preventDefault();
    openFind();
  }
});

// ---- Compare Sessions ----
function openCompare() {
  const sel = document.getElementById('compare-select');
  sel.innerHTML = allSessions
    .filter(s => s.id !== activeId)
    .map(s => `<option value="${s.id}">${escHtml(s.display_title)}</option>`)
    .join('');
  document.getElementById('compare-body').innerHTML = '<p style="color:#555;padding:20px;font-size:12px;">Select a session above and click Compare.</p>';
  document.getElementById('compare-overlay').classList.add('open');
}

function closeCompare() {
  document.getElementById('compare-overlay').classList.remove('open');
}

document.getElementById('compare-overlay').addEventListener('click', function(e) {
  if (e.target === this) closeCompare();
});

async function runCompare() {
  const id2 = document.getElementById('compare-select').value;
  if (!id2 || !activeId) return;
  const body = document.getElementById('compare-body');
  body.innerHTML = '<p style="padding:20px;color:#555;font-size:12px;">Comparing\u2026</p>';
  try {
    const r = await fetch(`/api/compare/${activeId}/${id2}`);
    const d = await r.json();
    renderCompare(d);
  } catch(e) {
    body.innerHTML = '<p style="padding:20px;color:#cc4444;font-size:12px;">Error comparing sessions.</p>';
  }
}

function renderCompare(d) {
  const body = document.getElementById('compare-body');
  const s1 = d.session1, s2 = d.session2;
  const meta = `
    <div class="compare-meta">
      <div class="compare-meta-card">
        <h4>${escHtml(s1.title)}</h4>
        <div>${s1.date} \u00b7 ${s1.size} \u00b7 ${s1.message_count} messages</div>
      </div>
      <div class="compare-meta-card">
        <h4>${escHtml(s2.title)}</h4>
        <div>${s2.date} \u00b7 ${s2.size} \u00b7 ${s2.message_count} messages</div>
      </div>
    </div>`;

  const stats = d.stats;
  const statsBar = `<div style="font-size:11px;color:#666;margin-bottom:12px;">
    ${stats.s1_blocks} blocks vs ${stats.s2_blocks} blocks \u00a0\u00b7\u00a0
    <span style="color:#44cc88">+${stats.added} added</span> \u00a0
    <span style="color:#cc4444">\u2212${stats.removed} removed</span> \u00a0
    <span style="color:#cccc44">${stats.changed} changed</span>
  </div>`;

  const diffRows = (d.code_diff || []).map(row => {
    const statusColors = {added:'diff-added',removed:'diff-removed',changed:'diff-changed',same:'diff-same'};
    const cls = statusColors[row.status] || '';
    const badge = `<span class="diff-status-badge">${row.status}</span>`;
    const fn = escHtml(row.filename||row.language||'code');
    const c1 = escHtml((row.content1||'(none)').slice(0,500));
    const c2 = escHtml((row.content2||'(none)').slice(0,500));
    return `<div class="diff-row ${cls}" style="margin-bottom:10px;">
      <div>
        <div class="diff-cell-label">${fn} ${badge}</div>
        <div class="diff-cell">${c1}</div>
      </div>
      <div>
        <div class="diff-cell-label">&nbsp;</div>
        <div class="diff-cell">${c2}</div>
      </div>
    </div>`;
  }).join('') || '<p style="color:#555;font-size:12px;padding:10px 0;">No code blocks to compare.</p>';

  body.innerHTML = meta + statsBar + `<div class="sum-label" style="margin-bottom:8px;">Code Comparison</div>` + diffRows;
}

loadProjects();
pollGitStatus();
setInterval(pollGitStatus, 60000);
</script>
<div id="session-tooltip"></div>
</body>
</html>
"""


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
    print("Starting Session Manager at http://localhost:5050")
    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host="0.0.0.0", port=5050, debug=False, threaded=True)
