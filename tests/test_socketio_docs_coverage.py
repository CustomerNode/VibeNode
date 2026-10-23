"""
Every SocketIO event the server handles or the browser listens for must be
documented in docs/api/socketio-events.md.

openapi.yaml has had a coverage test for a long time; the events reference had
none, and by 2026-10-23 it was missing 4 of 22 handlers and 19 of the events
the browser listens for. An event counts as documented when the reference has
a ``### `event_name` `` heading for it, or a table row whose first cell is
``` `event_name` ``` (the kanban and compose tables).
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "api" / "socketio-events.md"
WS_EVENTS = ROOT / "app" / "routes" / "ws_events.py"
STATIC_JS = ROOT / "static" / "js"

# Socket.IO's own lifecycle events; the browser subscribes to them but they are
# not VibeNode events. ``approval_needed`` is the pre-2026-10-12 name of
# ``external_notice`` that socket.js still listens for so an older web server
# keeps working; the server no longer emits it.
EXEMPT = {"connect", "disconnect", "connect_error", "reconnect"}


def _documented() -> set:
    text = DOC.read_text(encoding="utf-8")
    names = set(re.findall(r"^###\s+`([a-z_]+)`", text, re.M))
    # Rows of a table whose first column is "Event" (the kanban and compose
    # tables). Other tables list payload fields, which are not events.
    in_event_table = False
    for line in text.splitlines():
        if re.match(r"^\|\s*Event\s*\|", line):
            in_event_table = True
            continue
        if not line.startswith("|"):
            in_event_table = False
            continue
        if in_event_table:
            m = re.match(r"^\|\s*`([a-z_]+)`\s*\|", line)
            if m:
                names.add(m.group(1))
    return names


def _server_handlers() -> set:
    text = WS_EVENTS.read_text(encoding="utf-8")
    return set(re.findall(r"@socketio\.on\(\s*['\"]([a-z_]+)['\"]", text))


def _client_listeners() -> set:
    names = set()
    for f in STATIC_JS.glob("*.js"):
        names |= set(re.findall(r"socket\.on\(\s*['\"]([a-z_]+)['\"]", f.read_text(encoding="utf-8")))
    return names


def test_every_server_handler_is_documented():
    missing = sorted(_server_handlers() - _documented() - EXEMPT)
    assert not missing, "client -> server events without a section in socketio-events.md: %s" % missing


def test_every_event_the_browser_listens_for_is_documented():
    missing = sorted(_client_listeners() - _documented() - EXEMPT)
    assert not missing, "server -> client events without a section in socketio-events.md: %s" % missing


def _server_emits() -> set:
    """Event names the server or the Session Engine pushes (emit/_emit/push)."""
    pat = re.compile(r"(?:emit|_emit|_push_callback|push)\(\s*['\"]([a-z_]+)['\"]")
    const = re.compile(r"^[A-Z_]*EVENT\s*=\s*['\"]([a-z_]+)['\"]", re.M)
    names = set()
    for d in ("app", "daemon"):
        for f in (ROOT / d).rglob("*.py"):
            text = f.read_text(encoding="utf-8", errors="replace")
            names |= set(pat.findall(text)) | set(const.findall(text))
    return names


def test_doc_does_not_describe_events_nobody_uses():
    """A section for an event nothing emits or handles is stale documentation."""
    known = _server_handlers() | _client_listeners() | _server_emits() | EXEMPT
    stale = sorted(_documented() - known)
    assert not stale, "socketio-events.md documents events nothing emits or handles: %s" % stale
