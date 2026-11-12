"""Keep the person's own words out of every tracked file.

This repository is public.  What the person types into a session (requests,
complaints, thinking out loud) is private, cursing or not, so no run of
``RUN`` words from any of their messages may be written into a tracked file:
not as a code comment, a test input, a regex example or a doc line.  Rules are
written in neutral, made-up words instead.

- **The index** (``data/user_words.json``, gitignored) holds every ``RUN``-word
  run from the person's messages in every session on this machine, read from
  the CLI's transcripts (``~/.claude/projects/*/*.jsonl``).  Transcripts only
  grow, so each is read from the byte where the last refresh stopped.
- **Only typed words count.**  Messages VibeNode or the CLI composed (job
  notices, watchdog nudges, card buttons, scan reports, interruptions) are left
  out, and so are pasted code, file paths and web addresses; a run holding a
  number is ignored.
- **Text the app showed is not theirs.**  Pasting a line the app displayed (an
  error page, a card) puts the app's own words in a message.  A run already
  present in the repository's last commit when the message is first indexed
  is recorded as app text and never reported.
- Used by the Publish scan (``src/web/git_scanner.py`` ``scan_text``), which the
  Session Engine's write guard also runs, so a write is refused at once and
  Publish refuses whatever got past (shell writes included).

Kill switch: ``VIBENODE_USER_WORDS_GUARD=0`` (the test suite sets it).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

RUN = 6
REPO = Path(__file__).resolve().parents[2]
INDEX_FILE = Path(os.environ.get("VIBENODE_USER_WORDS_FILE") or REPO / "data" / "user_words.json")
_VERSION = 2
_REFRESH_EVERY_S = 20.0

_WORD = re.compile(r"[a-z0-9]+")
# "\n" written in source text would glue "n" onto the next word.
_ESCAPE = re.compile(r"\\[nrtbfv0]")

# Messages that open like one of these were composed by VibeNode or the CLI.
_COMPOSED = (
    "Flagged summary:", "Wrong kind of card:", "UPDATE STOPPED", "SECURITY SCAN VIOLATION",
    "The owner flagged", "⟦VN-EVENT", "Waiting for ", "My answers to the", "My review of the",
    "Yes, do that", "Yes, restart the", "Approve ", "Deal with what you found", "[Request interrupted",
    "<task-notification", "Stop hook feedback", "Caveat:", "<command-", "<local-command",
    "[VibeNode", "[SYSTEM NOTIFICATION", "<system-reminder>",
)
_INJECTED = re.compile(r"⟦VN-(?:MOBILE|NOTE)⟧.*?⟦/VN-(?:MOBILE|NOTE)⟧", re.S)
_SENT_FROM = re.compile(r"(?m)^\s*Sent from .*$")
_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
_INLINE = re.compile(r"`[^`\n]*`")
_URL = re.compile(r"\b(?:https?|file)://\S+|\b\w+(?:\.\w+)+/\S*")
_PATH = re.compile(r"(?:[A-Za-z]:)?[\\/]?(?:[\w.-]+[\\/])+[\w.-]*"
                   r"|\b[\w-]+\.(?:py|js|css|html|md|json|ts|tsx|sh|txt|jsonl|yaml|yml)\b")
_CODE_MARK = re.compile(r"[{}();=<>\[\]|$]|\w+\(|^\s*(?:def|class|import|from|return|const|let|var|"
                        r"function|if|for)\b|^\s*(?:File|Traceback|at)\b|:\d+")
_TEXT_EXT = re.compile(r"\.(?:py|js|mjs|css|html|md|txt|json|yaml|yml|toml|ini|cfg|sh|bat|ps1|svg|"
                       r"jsx|ts|tsx)$|^[^.]+$", re.I)


def enabled() -> bool:
    return os.environ.get("VIBENODE_USER_WORDS_GUARD", "1") != "0"


def words(text: str) -> list:
    return _WORD.findall(_ESCAPE.sub("  ", (text or "").lower()))


def typed_text(text: str) -> str:
    """The part of a user-role message the person typed, or '' if none."""
    if not isinstance(text, str) or not text.strip():
        return ""
    if text.lstrip().startswith(_COMPOSED):
        return ""
    try:
        from shared.transcripts import automated_source, is_timeline_system_content
        if automated_source(text) or is_timeline_system_content(text):
            return ""
    except Exception:
        pass
    text = _INJECTED.sub(" ", text)
    text = _SENT_FROM.sub(" ", text)
    text = _FENCE.sub("\n", text)
    text = _INLINE.sub(" ", text)
    kept = []
    for line in text.split("\n"):
        if len(_CODE_MARK.findall(line)) >= 3:
            continue          # a pasted line of code, a log or a traceback
        kept.append(_PATH.sub(" ", _URL.sub(" ", line)))
    return "\n".join(kept)


def runs(text: str) -> set:
    w = words(text)
    out = set()
    for i in range(len(w) - RUN + 1):
        g = w[i:i + RUN]
        if not any(x.isdigit() for x in g):
            out.add(" ".join(g))
    return out


def _message_text(line: str) -> str:
    if '"user"' not in line or '"tool_result"' in line:
        return ""
    try:
        o = json.loads(line)
    except ValueError:
        return ""
    if o.get("type") != "user" or o.get("isMeta") or o.get("isSidechain") or o.get("auto"):
        return ""
    c = (o.get("message") or {}).get("content") if isinstance(o.get("message"), dict) else None
    if isinstance(c, list):
        c = "\n".join(p.get("text") or "" for p in c if isinstance(p, dict) and p.get("type") == "text")
    return c if isinstance(c, str) else ""


def _norm(text: str) -> str:
    return " ".join(words(text))


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)


def _button_replies(line: str) -> set:
    """The reply texts of the buttons on any summary in this assistant entry."""
    try:
        texts = list(_strings(json.loads(line)))
    except ValueError:
        return set()
    out = set()
    for t in texts:
        for block in re.findall(r"```handoff\s*(.*?)```", t, re.S):
            try:
                card = json.loads(block)
            except ValueError:
                continue
            needs = card.get("needs") if isinstance(card, dict) else None
            for r in (needs or {}).get("replies") or []:
                if isinstance(r, dict) and isinstance(r.get("reply"), str):
                    out.add(_norm(r["reply"]))
    return out


def _transcripts(base: Path):
    try:
        return sorted(base.glob("*/*.jsonl"))
    except OSError:
        return []


def _repo_words(repo: Path) -> str:
    """The words of every text file in the last commit, as one string.

    The commit, not the working tree: a file being written or committed right
    now must not count as text the app already showed."""
    nw = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    r = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "-z", "HEAD"],
                       capture_output=True, timeout=20, creationflags=nw)
    shas = []
    for entry in r.stdout.decode("utf-8", "replace").split("\0"):
        meta, _, rel = entry.partition("\t")
        bits = meta.split()
        if len(bits) == 3 and bits[1] == "blob" and _TEXT_EXT.search(rel.rsplit("/", 1)[-1]):
            shas.append(bits[2])
    if not shas:
        return " \n "
    out = subprocess.run(["git", "-C", str(repo), "cat-file", "--batch"],
                         input=("\n".join(shas) + "\n").encode(),
                         capture_output=True, timeout=60, creationflags=nw).stdout
    parts, i = [], 0
    while i < len(out):
        nl = out.find(b"\n", i)
        if nl < 0:
            break
        head = out[i:nl].split()
        size = int(head[2]) if len(head) == 3 and head[2].isdigit() else 0
        if size <= 2_000_000:
            parts.append(" ".join(words(out[nl + 1:nl + 1 + size].decode("utf-8", "ignore"))))
        i = nl + 1 + size + 1
    return " \n " + " \n ".join(parts) + " \n "


def _app_text(candidates: set, repo: Path) -> set:
    """The candidate runs already present in the repository: words the app showed."""
    if not candidates:
        return set()
    corpus = _repo_words(repo)
    if len(candidates) <= 400:
        return {g for g in candidates if f" {g} " in corpus}
    found = set()
    firsts = {g.split(" ", 1)[0] for g in candidates}
    for chunk in corpus.split("\n"):
        w = chunk.split()
        for i in range(len(w) - RUN + 1):
            if w[i] in firsts:
                g = " ".join(w[i:i + RUN])
                if g in candidates:
                    found.add(g)
    return found


def _current_repo() -> Path:
    """The repository being written to (a git hook runs from its top), else this one."""
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                           timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return REPO


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") == _VERSION:
            return {"version": _VERSION, "files": dict(data.get("files") or {}),
                    "runs": set(data.get("runs") or ()), "app": set(data.get("app") or ())}
    except (OSError, ValueError, AttributeError):
        pass
    return {"version": _VERSION, "files": {}, "runs": set(), "app": set()}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"version": _VERSION, "files": data["files"],
                               "runs": sorted(data["runs"]), "app": sorted(data["app"])}),
                   encoding="utf-8")
    os.replace(tmp, path)


def refresh(base: Path = None, index: Path = None, repo: Path = None) -> dict:
    """Read what was typed since the last refresh and save the index."""
    base = base or Path(os.environ.get("VIBENODE_TRANSCRIPTS_DIR") or Path.home() / ".claude" / "projects")
    index = index or INDEX_FILE
    repo = repo or _current_repo()
    data = _load(index)
    fresh = set()
    changed = False
    for f in _transcripts(base):
        key = str(f)
        try:
            size = f.stat().st_size
        except OSError:
            continue
        done = data["files"].get(key, 0)
        if done > size:
            done = 0        # rewritten: read it again
        if done == size:
            continue
        with open(f, "rb") as fh:
            fh.seek(done)
            chunk = fh.read(size - done)
        end = chunk.rfind(b"\n")
        if end < 0:
            continue
        buttons = set()     # reply texts the latest summary offered as buttons
        for raw in chunk[:end].split(b"\n"):
            line = raw.decode("utf-8", "replace")
            if '"assistant"' in line and "reply" in line:
                buttons = _button_replies(line)
                continue
            msg = _message_text(line)
            if msg and _norm(msg) in buttons:
                continue    # a tapped button sends text the session wrote
            text = typed_text(msg)
            if text:
                fresh |= runs(text)
        data["files"][key] = done + end + 1
        changed = True
    new = fresh - data["runs"] - data["app"]
    if new:
        app = _app_text(new, repo)
        data["app"] |= app
        data["runs"] |= new - app
    if changed:
        _save(index, data)
    return data


_cache = {"at": 0.0, "mtime": None, "runs": frozenset(), "firsts": frozenset()}


def known_runs(index: Path = None, refresh_now: bool = True) -> frozenset:
    """The person's runs, refreshed at most every ``_REFRESH_EVERY_S`` seconds."""
    index = index or INDEX_FILE
    now = time.monotonic()
    if refresh_now and now - _cache["at"] >= _REFRESH_EVERY_S:
        _cache["at"] = now
        try:
            refresh(index=index)
        except Exception:
            pass
    try:
        mtime = index.stat().st_mtime_ns
    except OSError:
        return frozenset()
    if mtime != _cache["mtime"]:
        r = frozenset(_load(index)["runs"])
        _cache.update(mtime=mtime, runs=r, firsts=frozenset(g.split(" ", 1)[0] for g in r))
    return _cache["runs"]


def findings(content: str, known: frozenset = None) -> list:
    """One finding per line holding a run of the person's own words."""
    if known is None:
        if not enabled():
            return []
        known = known_runs()
        firsts = _cache["firsts"]
    else:
        firsts = frozenset(g.split(" ", 1)[0] for g in known)
    if not known or not content:
        return []
    low = _ESCAPE.sub("  ", content.lower())
    toks = [(m.group(0), m.start()) for m in _WORD.finditer(low)]
    out, lines = [], set()
    for i in range(len(toks) - RUN + 1):
        if toks[i][0] not in firsts:
            continue
        g = " ".join(t for t, _ in toks[i:i + RUN])
        if g in known:
            line = low.count("\n", 0, toks[i][1]) + 1
            if line not in lines:
                lines.add(line)
                out.append({"line": line, "type": "Your Own Words",
                            "desc": "Words you typed into a session: say the rule in neutral, made-up words",
                            "match": toks[i][0][:4] + "***"})
    return out


if __name__ == "__main__":
    t = time.time()
    d = refresh()
    print(f"{len(d['runs'])} runs of your words, {len(d['app'])} app-text runs, "
          f"{len(d['files'])} transcripts, {time.time() - t:.1f}s")
