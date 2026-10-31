"""A Standby session's text box says Standby, not Sleeping (owner's pick A,
2026-10-28, static/scratch/standby-bar.html).

The strip above the text box of a sleeping session used to read "Sleeping ·
Wakes when you send a message" even when the person had put it on Standby.
Now it reads "Standby · Set aside 12 min ago" in the sidebar's slate blue,
with "Type to pick it back up" in the box.

Server: ``hold_since`` in the session list and ``since`` in the
``session_hold`` broadcast (``app/session_holds.py``, ``sessions_api.py``).
Browser: the real ``index.html`` in headless Chromium (``live-panel.js``).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

# At module level: conftest keeps the shared browser open only for files that
# import this name (``uses_shared_browser``); imported inside the fixture, the
# browser was closed after the first test and the second one failed.
from tests._browser import shared_browser_scope


def _line(kind, text, ts, sid):
    return json.dumps({"type": kind, "message": {"role": kind, "content": text}, "timestamp": ts, "sessionId": sid})


def _iso(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


@pytest.fixture()
def proj(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    import app.config as cfg
    import app.routes.sessions_api as sa
    import app.sessions as sess
    monkeypatch.setattr(cfg, "_CLAUDE_PROJECTS", root)
    monkeypatch.setattr(sa, "_CLAUDE_PROJECTS", root)
    cfg._summary_cache.clear()
    cfg._summary_cache_index.clear()
    sess._orphan_cache.clear()
    name = "C--Users-test-standby"
    (root / name).mkdir()
    (root / name / "deck.jsonl").write_text(
        _line("user", "Make the deck", _iso(30), "deck") + "\n" + _line("assistant", "Pick a style?", _iso(29), "deck") + "\n",
        encoding="utf-8")
    return root / name


def test_the_list_says_when_a_session_was_set_aside(proj):
    from app import create_app
    client = create_app(testing=True).test_client()
    before = time.time()
    client.post("/api/session-hold/deck", json={"state": "standby", "project": proj.name})
    row = {s["id"]: s for s in client.get(f"/api/sessions?project={proj.name}").get_json()}["deck"]
    assert row["hold"] == "standby"
    assert before - 1 <= row["hold_since"] <= time.time() + 1
    client.post("/api/session-hold/deck", json={"state": None, "project": proj.name})
    row = {s["id"]: s for s in client.get(f"/api/sessions?project={proj.name}").get_json()}["deck"]
    assert "hold" not in row and "hold_since" not in row


# ── The real page ──

@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


STRIP = """() => { const c = document.querySelector('#live-input-bar .vn-card');
  const t = document.getElementById('live-input-ta');
  return c ? [c.dataset.st || '', c.querySelector('.vn-st-lbl').textContent,
              (c.querySelector('.vn-st-hint') || {}).textContent || '', t ? t.placeholder : ''] : null; }"""


@pytest.mark.slow
def test_the_text_box_of_a_standby_session_says_standby(browser):
    from tests._fake_app import CWD, FakeApp, fake_sessions, init_script
    s = fake_sessions(2)
    ids = [x["id"] for x in s]
    s[0]["hold"] = "standby"
    s[0]["hold_since"] = time.time() - 12 * 60
    app = FakeApp(browser, s, init=init_script("sessions", "grid"),
                  extra_routes={"/api/session/": lambda r: (200, {"id": ids[0], "display_title": "T", "messages": []})})
    try:
        app.push_many("session_state", [{"session_id": i, "state": "stopped", "cwd": CWD} for i in ids])
        app.eval(f"() => openInGUI('{ids[0]}')")
        app.page.wait_for_function(f"typeof liveSessionId !== 'undefined' && liveSessionId === '{ids[0]}'", timeout=10000)
        app.page.wait_for_function("!!document.querySelector('#live-input-bar .vn-card .vn-st-lbl')", timeout=5000)
        assert app.eval(STRIP) == ["standby", "Standby", "Set aside 12 min ago", "Type to pick it back up"]
        # Its colour is the sidebar's Standby teal, not the grey of Sleeping.
        assert app.eval("() => getComputedStyle(document.querySelector('#live-input-bar .vn-st-lbl')).color") in (
            "rgb(40, 190, 175)", "rgb(10, 135, 125)")

        # Taken off Standby on another device: the box goes back to Needs you
        # at once (the status picker, owner's pick A, 2026-10-31).
        app.push("session_hold", {"session_id": ids[0], "state": None})
        app.page.wait_for_timeout(200)
        assert app.eval(STRIP) == ["needs", "Needs you", "Its reply is waiting for you", "Type a message to wake it"]

        # Put back on Standby from elsewhere: "just now".
        app.push("session_hold", {"session_id": ids[0], "state": "standby", "since": time.time()})
        app.page.wait_for_timeout(200)
        assert app.eval(STRIP) == ["standby", "Standby", "Set aside just now", "Type to pick it back up"]
    finally:
        app.close()


@pytest.mark.slow
def test_with_person_statuses_off_a_sleeping_session_still_says_sleeping(browser):
    # With Smart termination off there are no person statuses, so no picker:
    # the box keeps the process status.  With it on, see test_status_picker.py.
    from tests._fake_app import CWD, FakeApp, fake_sessions, init_script
    s = fake_sessions(1)
    sid = s[0]["id"]
    app = FakeApp(browser, s, init=init_script("sessions", "grid", "localStorage.setItem('vnSmartTermination', 'off');"),
                  extra_routes={"/api/session/": lambda r: (200, {"id": sid, "display_title": "T", "messages": []})})
    try:
        app.push("session_state", {"session_id": sid, "state": "stopped", "cwd": CWD})
        app.eval(f"() => openInGUI('{sid}')")
        app.page.wait_for_function("!!document.querySelector('#live-input-bar .vn-card .vn-st-lbl')", timeout=10000)
        assert app.eval(STRIP) == ["", "Sleeping", "Wakes when you send a message", "Type a message to wake it"]
    finally:
        app.close()
