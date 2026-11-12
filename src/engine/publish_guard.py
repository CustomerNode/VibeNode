"""Refuse a file write that would fail the Publish scan, at the moment it is written.

This repository is public.  Without this guard, the Publish scan
(``src/web/git_scanner.py``) is the first thing to notice a session wrote a real
name, an internal address or a key into a tracked file, and the fix needs a new
session.  This guard runs the SAME check on the text a session is about to write
into this repository and refuses the write with what it found, so the session
fixes it in the same turn.

- Only Write / Edit / MultiEdit / NotebookEdit into THIS repository, outside
  anything gitignored (``git check-ignore``), are judged.  Other projects, other
  tools and ignored folders (``data/``, ``docs/plans/``, ``src/web/static/scratch/``)
  are never touched.
- Only the NEW text is scanned (an Edit's ``new_string``), so an edit near an
  old finding is not blamed for it.
- The rules are the scanner's own (``git_scanner.scan_text``), run in a separate
  interpreter because the engine never imports ``app`` (``tests/test_layering.py``).
  One rule set, so the guard and Publish never disagree.
- It fails OPEN: no interpreter, a timeout, a scanner without ``scan_text`` (an
  older checkout), anything unexpected, and the write goes through.  Publish
  still runs the full scan, so a gap here costs a later fix, never a lost write.
- Shell writes (``cat > file``, ``sed -i``) are not seen here; the suite's
  ``tests/test_publish_scan_clean.py`` catches those when the tests run.

Kill switch: ``VIBENODE_PUBLISH_GUARD=0``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

NAME = "publish scan"
ROOT = Path(__file__).resolve().parents[2]
_TIMEOUT_S = 8

_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

_SCAN = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from web import git_scanner as g
fn = getattr(g, "scan_text", None)
if fn is None:
    print("[]"); sys.exit(0)
print(json.dumps(fn(sys.argv[2], sys.stdin.read())))
"""


def _new_text(tool_name: str, tool_input: dict) -> str:
    if tool_name == "Write":
        return str(tool_input.get("content") or "")
    if tool_name == "Edit":
        return str(tool_input.get("new_string") or "")
    if tool_name == "MultiEdit":
        return "\n".join(str(e.get("new_string") or "") for e in tool_input.get("edits") or []
                         if isinstance(e, dict))
    if tool_name == "NotebookEdit":
        return str(tool_input.get("new_source") or "")
    return ""


def _repo_path(tool_input: dict, cwd: str):
    raw = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = Path(cwd or os.getcwd()) / p
    try:
        rel = Path(os.path.realpath(p)).relative_to(os.path.realpath(ROOT))
    except ValueError:
        return None
    return rel.as_posix()


def _ignored(rel: str) -> bool:
    r = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", "--", rel],
                       capture_output=True, timeout=_TIMEOUT_S)
    return r.returncode == 0


def findings(rel: str, text: str) -> list:
    """What the Publish scan would report for ``text`` saved at ``rel``, or [] when unsure."""
    r = subprocess.run([sys.executable, "-c", _SCAN, str(ROOT / "src"), rel], input=text,
                       capture_output=True, text=True, timeout=_TIMEOUT_S, cwd=str(ROOT))
    if r.returncode != 0:
        return []
    out = json.loads(r.stdout or "[]")
    return out if isinstance(out, list) else []


import re

# The user's own words, cursing or not, must never be copied into a tracked
# file. A swear-word list can't do that (rants and tone are not single words),
# so this compares the write against what the user actually typed this session:
# a run of this many identical words in a row is a quote, not a coincidence.
_USER_WORDS_NGRAM = 8
_WORD_RE = re.compile(r"[a-z0-9]+")
_MOBILE_BLOCK = re.compile(r"⟦VN-(?:MOBILE|NOTE)⟧.*?⟦/VN-(?:MOBILE|NOTE)⟧", re.S)


def _norm_words(s: str) -> list:
    return _WORD_RE.findall(s.lower())


def _user_messages(session_id: str) -> list:
    """The user's own message texts this session (not tool results, not the
    automated/system or stop-hook messages, not the mobile preamble)."""
    out = []
    if not session_id:
        return out
    try:
        base = Path.home() / ".claude" / "projects"
        for f in base.glob(f"*/{session_id}.jsonl"):
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    m = o.get("message") if isinstance(o.get("message"), dict) else o
                    if (m.get("role") or o.get("type")) != "user":
                        continue
                    if o.get("isMeta") or o.get("auto"):
                        continue
                    c = m.get("content")
                    text = ""
                    if isinstance(c, str):
                        text = c
                    elif isinstance(c, list):
                        for part in c:
                            if isinstance(part, dict):
                                if part.get("type") == "text":
                                    text += (part.get("text") or "") + "\n"
                                elif part.get("type") == "tool_result":
                                    text = ""  # a tool result, not the user's words
                                    break
                            elif isinstance(part, str):
                                text += part + "\n"
                    if not text.strip():
                        continue
                    stripped = text.lstrip()
                    if stripped.startswith(("Stop hook feedback:", "[SYSTEM NOTIFICATION",
                                            "Caveat:", "<system-reminder>")):
                        continue
                    text = _MOBILE_BLOCK.sub(" ", text)
                    text = re.sub(r"(?m)^\s*Sent from .*$", " ", text)
                    out.append(text)
    except Exception:
        return []
    return out


def _user_words_problem(text: str, session_id=None):
    """Refuse a write that copies a run of the user's own words into the repo."""
    try:
        if os.environ.get("VIBENODE_USER_WORDS_GUARD", "1") == "0":
            return None
        sid = session_id if session_id is not None else os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        msgs = _user_messages(sid)
        if not msgs:
            return None
        grams = set()
        for m in msgs:
            w = _norm_words(m)
            for i in range(len(w) - _USER_WORDS_NGRAM + 1):
                grams.add(tuple(w[i:i + _USER_WORDS_NGRAM]))
        if not grams:
            return None
        tw = _norm_words(text)
        for i in range(len(tw) - _USER_WORDS_NGRAM + 1):
            if tuple(tw[i:i + _USER_WORDS_NGRAM]) in grams:
                span = " ".join(tw[i:i + _USER_WORDS_NGRAM])
                return (f"this write copies the user's own words into the public repo (a run of "
                        f"{_USER_WORDS_NGRAM}+ words from one of the user's messages: \"{span}…\"). "
                        f"Never put the user's words, messages, rants or tone into a tracked file. "
                        f"Write the rule and its technical reason in your own neutral words instead, "
                        f"then write it again. (Kill switch: VIBENODE_USER_WORDS_GUARD=0.)")
    except Exception:
        return None
    return None


CLAUDE_MD_MAX_LINES = 60   # src/web/git_scanner.py's limit; a test keeps the two equal


def _public_file_problem(tool_name: str, tool_input: dict):
    """The public project file stays short: project notes go in the private notes file."""
    path = ROOT / "CLAUDE.md"
    try:
        now = path.read_text(encoding="utf-8")
    except Exception:
        now = ""
    if tool_name == "Write":
        after = str(tool_input.get("content") or "")
    elif tool_name == "Edit":
        old, new = str(tool_input.get("old_string") or ""), str(tool_input.get("new_string") or "")
        after = now.replace(old, new) if tool_input.get("replace_all") else now.replace(old, new, 1)
    elif tool_name == "MultiEdit":
        after = now
        for e in tool_input.get("edits") or []:
            if isinstance(e, dict):
                after = after.replace(str(e.get("old_string") or ""), str(e.get("new_string") or ""), 1)
    else:
        return None
    if after.count("\n") > CLAUDE_MD_MAX_LINES:
        return ("CLAUDE.md is the PUBLIC project file and holds only short public rules (at most "
                f"{CLAUDE_MD_MAX_LINES} lines). Project notes are private: write them in "
                "docs/plans/PROJECT-NOTES.md instead.")
    return None


def decide(tool_name: str, tool_input, cwd: str = ""):
    """A reason to refuse this write, or None.  Never raises."""
    try:
        if os.environ.get("VIBENODE_PUBLISH_GUARD", "1") == "0" or tool_name not in _WRITE_TOOLS:
            return None
        tool_input = tool_input if isinstance(tool_input, dict) else {}
        text = _new_text(tool_name, tool_input)
        rel = _repo_path(tool_input, cwd)
        if not text or rel is None or _ignored(rel):
            return None
        if rel == "CLAUDE.md":
            why = _public_file_problem(tool_name, tool_input)
            if why:
                return why
        quoted = _user_words_problem(text)
        if quoted:
            return quoted
        found = findings(rel, text)
        if not found:
            return None
        kinds = sorted({f.get("type", "?") for f in found})
        lines = "; ".join(f"line {f.get('line')} of the new text: {f.get('type')}" for f in found[:5])
        return (f"this write would fail VibeNode's Publish scan (a public repository): {lines}. "
                f"Replace real names, addresses, ids and keys with made-up ones (e.g. 'Jordan Rivers', "
                f"'my-pc.tail0000.ts.net'), reword any swearing, and write a rule and its technical reason "
                f"instead of quoting a person, noting who decided or dating a change (git records when). "
                f"Then write it again. A false positive in a source pattern goes in the scanner's skip list "
                f"in src/web/git_scanner.py. Found: {', '.join(kinds)}."
                + (" 'Your Own Words' is a run of words the person typed into a session: their "
                   "messages are private, cursing or not, so never quote or echo them in a tracked "
                   "file; describe the case in neutral, made-up words." if "Your Own Words" in kinds else ""))
    except Exception:
        return None
