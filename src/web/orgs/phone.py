"""Phone a Friend: open one session to one org member.

A call is a grant: one session, one person, a level (view / suggest / drive)
and an expiry. Any number of grants may be live; each is revoked on its own.

One background thread (``start``, from ``create_app``, never in a test app)
does three jobs every ``TICK_S`` seconds:

1. **Post.** For each session this VibeNode shares with a live grant, read the
   transcript lines added since the last post, turn them into the same
   timeline entries the chat draws (``ws_events._parse_raw_line``), redact
   secret-shaped text, trim long fields, and post them in one batch.
2. **Inbox.** Pick up friends' messages on those sessions. Each arrives with
   its grant's CURRENT state, so a message written before a revoke is dropped.
   Drive: the message is queued into the session, labelled with who sent it.
   Suggest: it is shown to the owner (``pf_suggestion``) to send or ignore.
   View grants cannot write at all (the database refuses).
3. **Pings.** A new grant to this member is announced once
   (``pf_incoming`` push + notification); a grant that ends is announced too,
   so open tabs drop it at once; a changed call note (``pf_note``), on either
   side, so every view shows the latest.

Local state (``data/phone_a_friend.json``): posting cursors per share, the
grants already announced, the inbox cursor, pending suggestions.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import threading
import time
from pathlib import Path

from ..config import _CLAUDE_PROJECTS, _VIBENODE_DIR
from . import store
from .store import OrgsError

log = logging.getLogger("web.orgs.phone")

STATE_FILE: Path = Path(os.environ.get("VIBENODE_PHONE_FILE")
                        or (_VIBENODE_DIR / "data" / "phone_a_friend.json"))
TICK_S = 3.0
MAX_FIELD = 4000
MAX_BATCH = 150
LEVELS = ("view", "suggest", "drive")
DURATIONS = {"1h": 60, "4h": 240, "today": None, "1d": 1440, "7d": 10080, "revoked": None}
_SID_RE = re.compile(r"^[A-Za-z0-9_-]{1,120}$")
_lock = threading.Lock()

# Secret-shaped text never leaves this machine in a shared thread.
_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|sb_secret_[A-Za-z0-9_-]{8,}|sb_publishable_[A-Za-z0-9_-]{8,}"
               r"|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}"
               r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{20,}"),
    re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)(\s*[=:]\s*)(['\"]?)[^\s'\"]{6,}\3"),
]


def redact(text: str) -> str:
    out = text
    for i, pat in enumerate(_SECRET_PATTERNS):
        if i == 2:
            out = pat.sub(lambda m: m.group(1) + " [hidden]", out)
        elif i == 3:
            out = pat.sub(lambda m: m.group(1) + m.group(2) + "[hidden]", out)
        else:
            out = pat.sub("[hidden]", out)
    return out


def clean_entry(entry, _depth: int = 0):
    """Redact and trim every string in an entry (dicts and lists walked)."""
    if isinstance(entry, str):
        s = redact(entry)
        if len(s) > MAX_FIELD:
            s = s[:MAX_FIELD] + f"\n…[{len(s) - MAX_FIELD} more characters not shared]"
        return s
    if _depth > 6:
        return None
    if isinstance(entry, dict):
        return {k: clean_entry(v, _depth + 1) for k, v in entry.items()}
    if isinstance(entry, list):
        return [clean_entry(v, _depth + 1) for v in entry[:200]]
    return entry


# ── local state ──────────────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.parent / (STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def source_name() -> str:
    """Which VibeNode a shared session lives on (shown to the friend)."""
    return (os.environ.get("VIBENODE_ORGS_SOURCE") or socket.gethostname() or "vibenode")[:120]


def find_transcript(session_id: str) -> Path | None:
    if not _SID_RE.match(session_id or ""):
        return None
    try:
        for d in _CLAUDE_PROJECTS.iterdir():
            cand = d / f"{session_id}.jsonl"
            if d.is_dir() and cand.exists():
                return cand
    except OSError:
        pass
    return None


# ── calls the routes make ────────────────────────────────────────────────────

def project_name(path: Path) -> str:
    """The project a transcript belongs to, as its owner knows it: the last
    part of the first working folder the transcript records."""
    try:
        with path.open(encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i > 50:
                    break
                if '"cwd"' not in line:
                    continue
                cwd = (json.loads(line).get("cwd") or "").rstrip("/\\")
                if cwd:
                    return re.split(r"[/\\]", cwd)[-1][:120]
    except (OSError, ValueError):
        pass
    # No recorded folder: the end of the project folder's name (the CLI names it
    # after the working folder, with separators turned into dashes).
    return path.parent.name.rsplit("-", 1)[-1][:120]


def call_friend(session_id: str, guest_id: str, level: str, duration: str,
                title: str = "", note: str = "") -> dict:
    """Open ``session_id`` to one member. Posts the thread's last part at once
    so the friend sees something the moment the ping lands."""
    if level not in LEVELS:
        raise OrgsError("bad_level")
    if duration not in DURATIONS:
        raise OrgsError("bad_duration")
    path = find_transcript(session_id)
    if path is None:
        raise OrgsError("no_session", "That session has no transcript on this computer yet.")
    minutes = DURATIONS[duration]
    if duration == "today":
        lt = time.localtime()
        minutes = max(1, 24 * 60 - (lt.tm_hour * 60 + lt.tm_min))
    g = store.call("pf_share", p_guest=guest_id, p_source=source_name(), p_session=session_id,
                   p_title=(title or "")[:200], p_level=level, p_minutes=minutes,
                   p_note=(note or "")[:500], p_project=project_name(path))
    try:
        publish_once(only_share=g.get("share_id"))
    except Exception:
        log.exception("first post for a new call failed; the loop retries")
    return g


def revoke(grant_id: str) -> bool:
    return bool(store.call("pf_revoke", p_grant=grant_id))


def set_note(grant_id: str, note: str) -> dict:
    """Change a call's note; either side of the call may, live or ended."""
    return store.call("pf_set_note", p_grant=grant_id, p_note=(note or "")[:500])


def outgoing(days: int = 7) -> list:
    return store.call("pf_grants_list", p_side="out", p_days=days) or []


def incoming(days: int = 7) -> list:
    return store.call("pf_grants_list", p_side="in", p_days=days) or []


def read(share_id: str, after: int = 0, limit: int = 100, before: int | None = None) -> dict:
    return store.call("pf_read", p_share=share_id, p_after=after, p_limit=limit, p_before=before)


def send_to_friend_session(grant_id: str, text: str) -> int:
    return store.call("pf_guest_message", p_grant=grant_id, p_text=text)


def suggestions() -> list:
    with _lock:
        return list(_load_state().get("suggestions", []))


def dismiss_suggestion(key: str) -> dict | None:
    with _lock:
        state = _load_state()
        items = state.get("suggestions", [])
        hit = next((s for s in items if s.get("key") == key), None)
        state["suggestions"] = [s for s in items if s.get("key") != key]
        _save_state(state)
        return hit


# ── the loop ─────────────────────────────────────────────────────────────────

# Transcript line types a friend's copy needs; the rest (file snapshots,
# progress ticks, the caller's own title) are left out.
_SHARED_TYPES = {"user", "assistant", "system"}
MAX_LINE_BYTES = 60000


def _lines_since(path: Path, line_no: int) -> tuple[list, int]:
    """The transcript lines added since ``line_no``, redacted and trimmed, as
    the friend's VibeNode writes them into its own copy of the session."""
    try:
        lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    except OSError:
        return [], line_no
    if line_no > len(lines):          # rewritten (rewind): start over from the end
        line_no = max(0, len(lines) - 40)
    out = []
    for raw in lines[line_no:]:
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("type") not in _SHARED_TYPES:
            continue
        obj = clean_entry(obj)
        if len(json.dumps(obj)) > MAX_LINE_BYTES:
            # Still too big (many long fields): keep only its words.
            from ..routes.ws_events import _parse_raw_line
            text = " ".join((e.get("text") or e.get("desc") or "") for e in _parse_raw_line(raw))
            obj = {"type": obj.get("type"), "message": {"role": (obj.get("message") or {}).get("role", "assistant"),
                   "content": redact(text)[:MAX_FIELD]}}
        out.append(obj)
    return out, len(lines)


def publish_once(only_share: str | None = None, status_of=None) -> int:
    """Post new thread entries for every share with a live grant. Returns how many."""
    grants = [g for g in outgoing(days=0) if g.get("live") and g.get("source") == source_name()]
    shares = {}
    for g in grants:
        if only_share is None or g["share_id"] == only_share:
            shares[g["share_id"]] = g
    posted = 0
    with _lock:
        state = _load_state()
        cursors = state.setdefault("cursors", {})
        for share_id, g in shares.items():
            path = find_transcript(g["session_id"])
            if path is None:
                continue
            cur = cursors.get(share_id)
            if cur is None:                       # a new share: start with the recent part
                total = sum(1 for l in path.read_text(encoding="utf-8").splitlines() if l.strip())
                cur = max(0, total - 150)
            lines, new_cur = _lines_since(path, cur)
            status = status_of(g["session_id"]) if status_of else None
            if not lines and new_cur == cur and status is None:
                continue
            batch = [{"kind": "entry", "body": {"line": ln}} for ln in lines[-MAX_BATCH:]]
            store.call("pf_post", p_share=share_id, p_posts=batch, p_status=status)
            cursors[share_id] = new_cur
            posted += len(batch)
        _save_state(state)
    return posted


def _deliver(msg: dict, sm, notify) -> None:
    sid, text, who = msg["session_id"], msg.get("text") or "", msg.get("guest") or "A friend"
    if msg.get("level") == "drive":
        labelled = f"[{who} via Phone a Friend]\n{text}"
        try:
            if sm is not None and sm.has_session(sid):
                sm.queue_message(sid, labelled)
            elif sm is not None:
                sm.start_session(sid, prompt=labelled, resume=True)
        except Exception:
            log.exception("could not hand a friend's message to session %s", sid)
            return
        notify("pf_message", {"session_id": sid, "from": who, "level": "drive", "text": text[:300]})
    else:
        key = f"{msg['share_id']}:{msg['seq']}"
        state = _load_state()
        items = state.setdefault("suggestions", [])
        if not any(s.get("key") == key for s in items):
            items.append({"key": key, "session_id": sid, "from": who, "text": text,
                          "at": msg.get("created_at")})
            state["suggestions"] = items[-100:]
            _save_state(state)
        notify("pf_suggestion", {"key": key, "session_id": sid, "from": who, "text": text[:300]})


def inbox_once(sm=None, notify=lambda e, d: None) -> int:
    with _lock:
        state = _load_state()
        since = state.get("inbox_since")
        seen = set(state.get("inbox_seen", []))
    msgs = store.call("pf_inbox", p_since=since) or []
    handled = 0
    for msg in msgs:
        key = f"{msg['share_id']}:{msg['seq']}"
        if key in seen:
            continue
        seen.add(key)
        if msg.get("live") and msg.get("source") == source_name():
            with _lock:
                _deliver(msg, sm, notify)
            handled += 1
        since = msg.get("created_at") or since
    with _lock:
        state = _load_state()
        state["inbox_since"] = since
        state["inbox_seen"] = sorted(seen)[-500:]
        _save_state(state)
    return handled


def pings_once(notify=lambda e, d: None) -> list:
    """Announce each new incoming grant once, and each that ended."""
    grants = incoming(days=1)
    with _lock:
        state = _load_state()
        announced = state.setdefault("announced", {})
        new = []
        for g in grants:
            gid = g["grant_id"]
            was = announced.get(gid)
            if g.get("live") and was is None:
                announced[gid] = "live"
                new.append(g)
                notify("pf_incoming", g)
            elif not g.get("live") and was == "live":
                announced[gid] = "ended"
                notify("pf_ended", {"grant_id": gid, "share_id": g["share_id"]})
        state["announced"] = dict(list(announced.items())[-500:])
        _save_state(state)
    return new


def notes_once(notify=lambda e, d: None) -> list:
    """Tell open tabs when a call's note changed, on either side."""
    grants = outgoing(days=7) + incoming(days=7)
    changed = []
    with _lock:
        state = _load_state()
        seen = state.setdefault("notes", {})
        for g in grants:
            mark = f"{g.get('note_at')}|{g.get('note', '')}"
            if g["grant_id"] in seen and seen[g["grant_id"]] != mark:
                changed.append(g)
                notify("pf_note", g)
            seen[g["grant_id"]] = mark
        state["notes"] = dict(list(seen.items())[-500:])
        _save_state(state)
    return changed


_thread: dict = {"t": None, "stop": None}


def start(app, socketio) -> None:
    """The background loop. Quiet when this VibeNode is in no org."""
    if os.environ.get("VIBENODE_PHONE_A_FRIEND") == "0" or _thread["t"] is not None:
        return
    stop = threading.Event()

    def notify(event, data):
        try:
            socketio.emit(event, data)
        except Exception:
            log.exception("push %s failed", event)

    def status_of(sid):
        try:
            st = app.session_manager.get_session_state(sid) or {}
            return str(st.get("state") or "")[:40] or None
        except Exception:
            return None

    def run():
        backoff = TICK_S
        while not stop.wait(backoff):
            if not store.load():
                backoff = 15.0
                continue
            try:
                publish_once(status_of=status_of)
                inbox_once(sm=app.session_manager, notify=notify)
                pings_once(notify=notify)
                notes_once(notify=notify)
                from . import mirror
                mirror.sync_once(notify=notify)
                backoff = TICK_S
            except OrgsError as e:
                if e.code in ("not_a_member",):
                    log.warning("this VibeNode's org membership was removed")
                backoff = min(60.0, backoff * 2)
            except Exception:
                log.exception("Phone a Friend tick failed")
                backoff = min(60.0, backoff * 2)

    t = threading.Thread(target=run, name="phone-a-friend", daemon=True)
    _thread.update(t=t, stop=stop)
    t.start()
