"""A phoned session, as a real session on the friend's computer.

VibeNode shows a session from its transcript file. So the friend's VibeNode
writes the shared thread into a transcript of its own, in one project per
caller ("Priya phoned you", a folder under ``~/.vibenode-phoned``), and the
app's own session list, chat, summary cards and composer show it with no code
of their own. The session's title leads with the caller's project, so it reads
"CustomerNode · Login test".

Three rules keep it safe:

* **Nothing runs it.** ``is_mirror`` is checked by the socket handlers before
  any send, queue or start reaches the Session Engine; a reply is routed to the
  caller through the call (``reply``), a wake is answered with an idle state.
* **It follows the call.** Ended calls keep their thread for ``KEEP_DAYS``;
  after that, or once the caller has no calls to this member, the file and the
  project go, so an empty "phoned you" project never shows.
* **Lines are the caller's transcript lines,** already redacted and trimmed on
  the caller's side (``phone.clean_entry``); an older caller's timeline entries
  are turned back into lines (``_entry_to_line``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
from pathlib import Path

from .. import config as _cfg
from . import phone

log = logging.getLogger("web.orgs.mirror")

# Not a dot folder: the project list reads "--" in a folder name as a Windows
# drive, so a ".vibenode-phoned" project never showed.
ROOT: Path = Path(os.environ.get("VIBENODE_PHONED_DIR") or (Path.home() / "vibenode-phoned"))
_LEGACY_ROOT = Path.home() / ".vibenode-phoned"
KEEP_DAYS = 7
_lock = threading.Lock()
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(owner: str) -> str:
    return (_SAFE.sub("-", owner or "friend").strip("-") or "friend")[:40]


def project_cwd(owner: str) -> Path:
    return ROOT / _slug(owner)


def project_encoded(owner: str) -> str:
    return _cfg._encode_cwd(str(project_cwd(owner)))


def project_prefix() -> str:
    return _cfg._encode_cwd(str(ROOT)) + "-"


def _project_dir(owner: str) -> Path:
    return _cfg._CLAUDE_PROJECTS / project_encoded(owner)


def is_mirror(session_id: str) -> bool:
    """A phoned session's file lives in a "phoned you" project."""
    if not session_id or not phone._SID_RE.match(session_id):
        return False
    try:
        pre = project_prefix()
        for d in _cfg._CLAUDE_PROJECTS.iterdir():
            if d.name.startswith(pre) and (d / f"{session_id}.jsonl").exists():
                return True
    except OSError:
        pass
    return False


def _state() -> dict:
    st = phone._load_state()
    return st.setdefault("mirrors", {})


def grant_for(session_id: str) -> dict | None:
    with phone._lock:
        return phone._load_state().get("mirrors", {}).get(session_id)


# ── turning posts into transcript lines ─────────────────────────────────────

def _entry_to_line(e: dict) -> dict | None:
    kind = e.get("kind")
    if kind == "user":
        return {"type": "user", "message": {"role": "user", "content": e.get("text", "")}}
    if kind == "asst":
        return {"type": "assistant", "message": {"role": "assistant",
                                                 "content": [{"type": "text", "text": e.get("text", "")}]}}
    if kind == "tool_use":
        return {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": e.get("id", ""), "name": e.get("name", ""),
             "input": {"description": e.get("desc", "")}}]}}
    return None


def _post_lines(post: dict, grant: dict, key_for=None) -> list:
    from . import keys
    body = post.get("body") or {}
    if isinstance(body, dict) and "enc" in body:
        body = keys.open_body(body, key_for or (lambda v: None))
        if body is None:
            return []                                   # not readable with this member's keys
    elif post.get("kind") == "entry":
        return []                                       # plain lines are never accepted
    if post.get("kind") == "entry":
        line = body.get("line") if isinstance(body.get("line"), dict) else _entry_to_line(body)
        return [line] if line else []
    if post.get("kind") == "guest_msg" and body.get("level") == "suggest":
        # The friend's own suggestion, locked like everything else.
        try:
            inner = json.loads(body.get("text") or "")
        except ValueError:
            return []
        opened = keys.open_body(inner, key_for or (lambda v: None)) if isinstance(inner, dict) else None
        if not isinstance(opened, dict):
            return []
        body = dict(body, text=opened.get("text", ""))
        # A suggestion is not in the caller's transcript; show it where it was written.
        return [{"type": "user", "message": {"role": "user",
                 "content": f"[Suggested to {grant.get('owner', 'them')}] {body.get('text', '')}"}}]
    return []


def _title(g: dict) -> str:
    t = g.get("title") or "Untitled session"
    return f"{g['project']} · {t}" if g.get("project") else t


def _write_lines(path: Path, lines: list) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")


def _ensure_project(owner: str) -> Path:
    d = _project_dir(owner)
    d.mkdir(parents=True, exist_ok=True)
    project_cwd(owner).mkdir(parents=True, exist_ok=True)
    names = _cfg._load_project_names()
    want = f"{owner} phoned you"
    if names.get(d.name) != want:
        names[d.name] = want
        _cfg._save_project_names(names)
    return d


def _drop_project(encoded: str) -> None:
    """Remove a phoned project folder and everything in it. VibeNode writes
    its own bookkeeping files into every project folder it lists, so removing
    only the transcripts left an empty, nameless project behind."""
    d = _cfg._CLAUDE_PROJECTS / encoded
    if not (d.name.startswith(project_prefix()) or d.name.startswith(_cfg._encode_cwd(str(_LEGACY_ROOT)) + "-")):
        return
    shutil.rmtree(d, ignore_errors=True)
    names = _cfg._load_project_names()
    if names.pop(encoded, None) is not None:
        _cfg._save_project_names(names)


# The caller's session status, as the chat shows it: working while their
# session works, idle otherwise.
_WORKING = {"working", "starting", "running", "thinking"}


def sync_once(notify=lambda e, d: None, grants: list | None = None, posts: dict | None = None) -> int:
    """Bring every phoned session's file up to date; returns lines written.

    ``grants`` and ``posts`` come from the loop's single round trip; without
    them (tests, older cloud storage) this fetches them itself."""
    from ..routes.ws_events import _parse_raw_line

    if grants is None:
        grants = phone.incoming(days=KEEP_DAYS)
    # One file per shared session: the newest grant for it decides what may be done.
    by_share: dict = {}
    for g in sorted(grants, key=lambda g: g.get("created_at") or ""):
        by_share[g["share_id"]] = g
    written = 0
    with _lock:
        with phone._lock:
            state = phone._load_state()
        mirrors = state.setdefault("mirrors", {})
        keep_projects = set()
        for sid, g in by_share.items():
            d = _ensure_project(g.get("owner") or "friend")
            keep_projects.add(d.name)
            path = d / f"{sid}.jsonl"
            m = mirrors.get(sid) or {"cursor": 0, "title": ""}
            if not path.exists():
                m["cursor"], m["title"] = 0, ""
            title = _title(g)
            if m.get("title") != title:
                _write_lines(path, [{"type": "custom-title", "customTitle": title, "sessionId": sid}])
                m["title"] = title
            if g.get("live"):
                after = m.get("cursor", 0)
                handed = posts.get(sid) if posts is not None else None
                if posts is not None and handed is None:
                    pages = 0                            # nothing new: the sync said so
                else:
                    pages = 20                           # at most 20 pages a tick
                for _ in range(pages):
                    if handed is not None:
                        page, handed = handed, None
                    else:
                        try:
                            page = (phone.read(sid, after=after, limit=200) or {}).get("posts") or []
                        except phone.OrgsError:
                            break
                    from . import keys as _keys
                    key_for = lambda v, _g=g: _keys.thread_key_for_reading(state, _g, v)
                    for p in page:
                        lines = _post_lines(p, g, key_for)
                        if lines:
                            for line in lines:
                                line["sessionId"] = sid
                            _write_lines(path, lines)
                            written += len(lines)
                            for line in lines:
                                for entry in _parse_raw_line(json.dumps(line)):
                                    notify("session_entry", {"session_id": sid, "entry": entry})
                        after = max(after, p.get("seq", after))
                    if len(page) < 200:
                        break
                m["cursor"] = after
            # The caller's session status, drawn by the chat's own state handling.
            shown = "working" if (g.get("live") and str(g.get("status") or "").lower() in _WORKING) else "idle"
            if m.get("shown_state") != shown:
                notify("session_state", {"session_id": sid, "state": shown})
                m["shown_state"] = shown
            m.update(grant_id=g["grant_id"], level=g["level"], live=bool(g.get("live")),
                     owner=g.get("owner"), owner_id=g.get("owner_id"), note=g.get("note", ""),
                     project=g.get("project", ""), title_text=g.get("title", ""))
            mirrors[sid] = m
        # The first version's folders (see ROOT) are not shown: remove them.
        legacy = _cfg._encode_cwd(str(_LEGACY_ROOT)) + "-"
        try:
            for d in list(_cfg._CLAUDE_PROJECTS.iterdir()):
                if d.name.startswith(legacy):
                    _drop_project(d.name)
        except OSError:
            pass
        shutil.rmtree(_LEGACY_ROOT, ignore_errors=True)
        # Calls that are gone: their files go; a caller with none left loses the project.
        pre = project_prefix()
        for gone in [s for s in mirrors if s not in by_share]:
            mirrors.pop(gone, None)
        try:
            for d in list(_cfg._CLAUDE_PROJECTS.iterdir()):
                if not d.name.startswith(pre):
                    continue
                if d.name not in keep_projects:
                    _drop_project(d.name)
                    shutil.rmtree(ROOT / d.name[len(pre):], ignore_errors=True)
                    continue
                for f in d.glob("*.jsonl"):
                    if f.stem not in by_share:
                        f.unlink()
        except OSError:
            pass
        with phone._lock:
            fresh = phone._load_state()
            fresh["mirrors"] = mirrors
            phone._save_state(fresh)
    return written


def remove_all() -> None:
    """Leaving the org: every phoned project goes."""
    pre = project_prefix()
    try:
        for d in list(_cfg._CLAUDE_PROJECTS.iterdir()):
            if d.name.startswith(pre):
                _drop_project(d.name)
    except OSError:
        pass
    shutil.rmtree(ROOT, ignore_errors=True)


def reply(session_id: str, text: str) -> tuple[bool, str]:
    """A message typed in a phoned session goes to the caller, never to a CLI.
    Returns (sent, what to tell the person)."""
    m = grant_for(session_id)
    if not m:
        return False, "This call is not open on this computer any more."
    who = m.get("owner") or "your friend"
    if not m.get("live"):
        return False, f"{who} ended this call, so nothing was sent."
    if m.get("level") == "view":
        return False, f"{who} let you watch only, so nothing was sent."
    try:
        phone.send_to_friend_session(m["grant_id"], text)
    except phone.OrgsError as e:
        if e.code == "no_access":
            return False, f"{who} ended this call, so nothing was sent."
        return False, "Not sent: " + (str(e) or e.code)
    if m.get("level") == "drive":
        return True, f"Sent to {who}'s session."
    return True, f"Suggested to {who}; they decide whether to send it."
