"""Three visible fixes from the 2026-10-23 review, each against the real page.

1. ``.kanban-column`` has ``min-width: 240px`` (was 160px): five columns at
   1280px squeezed titles to one word per line.  The wrapper still scrolls
   horizontally, so nothing is crushed.
2. The jump/auto pill never covers the last line of the final reply.  The
   pill is lifted out of a zero-height sticky wrapper with translateY(-100%),
   so at the true bottom it sat on the final message.  A flow gap before the
   wrapper (``.live-log > .vn-jump-wrap:not(.vn-classic) { margin-top }``)
   reserves the room; the classic pill keeps its old geometry.
3. ``/#kanban`` or ``/#compose`` opened in a fresh tab whose stored view was
   the homepage rendered without the sidebar: the boot script hid it for the
   homepage, and ``setViewMode(viewMode)`` ran with ``prevMode === viewMode``
   so the "leaving homepage: restore sidebar" branch never fired.  app.js now
   undoes the boot hide when the hash overrides the stored mode.

The page is the REAL ``templates/index.html`` with every real script and the
real CSS in headless Chromium; only the network is faked (``tests/_fake_app.py``).
"""

from __future__ import annotations

import pytest

from tests._browser import shared_browser_scope
from tests._fake_app import FakeApp, fake_sessions, init_script

XDIST_SPLITTABLE = True

PHONE = {"width": 390, "height": 844}
LAPTOP = {"width": 1280, "height": 800}

# A standalone thread: the real .live-log CSS applies by class, ThreadScroll is
# the real one, and the pill is built by its attach().
BUILD_THREAD = """(classic) => {
  const log = document.createElement('div');
  log.className = 'live-log';
  log.id = 'live-log';
  log.style.cssText = 'position:fixed;top:0;left:0;width:360px;height:400px;background:#111;';
  for (let i = 0; i < 30; i++) {
    const m = document.createElement('div');
    m.className = 'msg ' + (i % 2 ? 'assistant' : 'user');
    m.textContent = 'message ' + i + ' ' + 'word '.repeat(12);
    log.appendChild(m);
  }
  document.body.appendChild(log);
  ThreadScroll.attach(log, 'thread-under-test');
  log.scrollTop = log.scrollHeight;
  return log.scrollHeight > log.clientHeight;
}"""

# The visible pill/chip (whichever thread-scroll shows) vs the last message.
MEASURE = """() => {
  const log = document.getElementById('live-log');
  const msgs = log.querySelectorAll('.msg');
  const last = msgs[msgs.length - 1].getBoundingClientRect();
  const wrap = log.querySelector('.vn-jump-wrap');
  const vis = [...wrap.querySelectorAll('.vn-jump-pill, .vn-jump-auto')]
    .filter(e => getComputedStyle(e).display !== 'none' && e.getBoundingClientRect().height > 0);
  const el = vis[0];
  const r = el ? el.getBoundingClientRect() : null;
  return {
    visible: wrap.classList.contains('visible'),
    classic: wrap.classList.contains('vn-classic'),
    marginTop: getComputedStyle(wrap).marginTop,
    atBottom: Math.abs(log.scrollHeight - log.clientHeight - log.scrollTop) < 2,
    pill: r ? {top: r.top, bottom: r.bottom, height: r.height} : null,
    last: {top: last.top, bottom: last.bottom},
    overlap: r ? Math.max(0, Math.min(r.bottom, last.bottom) - Math.max(r.top, last.top)) : 0,
  };
}"""


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


def _wait_frames(app, n=3):
    for _ in range(n):
        app.page.evaluate("() => new Promise(r => requestAnimationFrame(() => r()))")


@pytest.mark.slow
@pytest.mark.parametrize("viewport", [LAPTOP, PHONE], ids=["laptop", "phone"])
def test_pill_never_covers_the_last_line(browser, viewport):
    app = FakeApp(browser, fake_sessions(3), viewport=viewport)
    try:
        assert app.eval(BUILD_THREAD, False), "the thread must overflow for the test to mean anything"
        _wait_frames(app)
        m = app.eval(MEASURE)
        assert m["atBottom"], m
        assert m["visible"] and m["pill"], f"the Auto chip must be on screen at the bottom: {m}"
        assert not m["classic"]
        assert m["overlap"] == 0, f"pill covers the last message by {m['overlap']}px: {m}"
        # The reserve is at least the pill's own height.
        assert float(m["marginTop"].rstrip("px")) >= m["pill"]["height"], m
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
def test_classic_pill_geometry_is_unchanged(browser):
    """Classic scroll keeps the pre-2026-10-14 thread exactly: no reserve."""
    app = FakeApp(browser, fake_sessions(3),
                  init=init_script(extra="localStorage.setItem('vnClassicScroll', 'on');"))
    try:
        assert app.eval(BUILD_THREAD, True)
        _wait_frames(app)
        m = app.eval(MEASURE)
        assert m["classic"], m
        assert m["marginTop"] == "0px", m
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
def test_kanban_columns_are_at_least_240_wide(browser):
    app = FakeApp(browser, fake_sessions(3), viewport=LAPTOP)
    try:
        widths = app.eval("""() => {
          const board = document.getElementById('kanban-board');
          board.style.display = '';
          board.style.width = '1100px';
          board.innerHTML = '<div class="kanban-columns-wrapper">' +
            '<div class="kanban-column"></div>'.repeat(5) + '</div>';
          const cols = [...board.querySelectorAll('.kanban-column')];
          return {
            widths: cols.map(c => c.getBoundingClientRect().width),
            minWidth: getComputedStyle(cols[0]).minWidth,
            overflowX: getComputedStyle(board.firstElementChild).overflowX,
          };
        }""")
        assert widths["minWidth"] == "240px", widths
        assert all(w >= 240 for w in widths["widths"]), widths
        assert widths["overflowX"] == "auto", widths   # horizontal scroll kept
    finally:
        app.close()


SIDEBAR_STATE = """() => {
  const sb = document.querySelector('.sidebar');
  const r = sb.getBoundingClientRect();
  const proj = document.getElementById('btn-project');
  const pr = proj.getBoundingClientRect();
  return {
    display: getComputedStyle(sb).display, inline: sb.style.display, width: r.width,
    projectVisible: pr.width > 0 && pr.height > 0,
    viewMode: viewMode,
    kanbanShown: getComputedStyle(document.getElementById('kanban-board')).display !== 'none',
  };
}"""


@pytest.mark.slow
@pytest.mark.parametrize("hash_, mode", [("#kanban", "kanban"), ("#compose", "compose")])
def test_fresh_tab_on_a_hash_route_shows_the_sidebar(browser, hash_, mode):
    """Stored view = homepage (sidebar hidden at boot), URL = /#kanban."""
    app = FakeApp(browser, fake_sessions(3), init=init_script("homepage", "grid"),
                  path="/" + hash_, wait_for="viewMode === '%s' && typeof allSessions !== 'undefined' && allSessions.length > 0" % mode)
    try:
        app.page.wait_for_timeout(300)   # setViewMode's fade + initKanban/initCompose
        st = app.eval(SIDEBAR_STATE)
        assert st["viewMode"] == mode, st
        assert st["display"] != "none", f"sidebar hidden on a fresh {hash_} tab: {st}"
        assert st["width"] > 0 and st["projectVisible"], st
        if mode == "kanban":
            assert st["kanbanShown"], st
    finally:
        app.close()
