"""A tab must never keep flashing for an approval that is over.

Reported 2026-10-12: "when my customernode sessions request approval for a
release, the tab name on the browser flashes. there's some edge cases where it
continues flashing after i approve, that should be made impossible".

``static/js/notify.js`` used to start the title flash on the approval ping and
stop it only when that one tab was looked at.  So it kept flashing after the
approval when the user stayed on the approval page, answered from another
device, or had a second tab open.  A ping that arrived while the tab was on
screen was remembered with no expiry and started the flash hours later.
"Release is live" pings, which have nothing to approve, flashed "Approval
needed" too.  And the favicon's red dot, built from an image that loads late,
could be painted after the flash had already stopped.

Now nothing that stays on is started by an event.  The flash runs only while a
reason holds, every tick re-checks the reason before it writes the title, and
the favicon dot and the OS banner are up only while the title is flashing:

* an approval holds while the server's ``approvals_open`` snapshots keep naming
  it (``app/approval_watch.py``, tested in ``tests/test_approval_watch.py``).
  Each snapshot is a lease, so "answered" and "no longer heard about" both end
  it;
* a stalled session holds until the tab is looked at.

Four layers here:

1. scenarios: the real ``notify.js`` runs in Node inside a small fake browser
   with a hand-driven clock, one scenario per way the flash used to outlive
   its reason;
2. an invariant: 240 random histories of snapshots, tab switches, throttled
   timers, freezes and pings, checked against an independent model.  No
   title is ever written, and no banner left up, for a reason that is over;
3. the chain: the payloads the real server routes emit are fed through the
   real ``socket.js`` handlers into the real ``notify.js``;
4. the real page in headless Chromium, real timers and the real
   ``document.title`` (skipped where Playwright is not installed).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
TEMPLATES = ROOT / "templates"
NOTIFY_JS = STATIC / "js" / "notify.js"
SOCKET_JS = STATIC / "js" / "socket.js"

WARN = "⚠ Approval needed"
STALLED = "⚠ Session stalled"
PAGE = "VibeNode"

# A browser small enough to run notify.js in Node: a document with a title and
# a visibility state, timers on a fake clock, a <link rel=icon>, Image and
# Notification stand-ins.  Nothing in it knows how notify.js works.
_FAKE_BROWSER = r"""
const vm = require('vm');

function makeBrowser(source, opts) {
  opts = opts || {};
  // Two clocks, as in a browser. `now` is the page's own time: it never
  // jumps, and timers run on it. The wall clock (Date.now) is `now` plus
  // whatever the user, NTP or a closed laptop lid has moved it by.
  let now = 1700000000000;
  let wallSkew = 0;
  let nextTimer = 1;
  const timers = new Map();
  const listeners = { document: {}, window: {} };
  const titleWrites = [];
  const link = { id: '', rel: 'icon', _href: '/static/images/logo.png' };
  Object.defineProperty(link, 'href', { get() { return link._href; }, set(v) { link._href = String(v); } });
  const images = [];
  const notifications = [];
  let title = 'VibeNode';
  let hidden = false;
  let hiddenThrows = false;

  const document = {
    readyState: 'complete',
    head: { appendChild() {} },
    addEventListener(ev, fn) { (listeners.document[ev] = listeners.document[ev] || []).push(fn); },
    getElementById(id) { return link.id === id ? link : null; },
    querySelector() { return link; },
    createElement(tag) {
      if (tag !== 'canvas') return {};
      return { width: 0, height: 0,
        getContext() { return { drawImage() {}, beginPath() {}, arc() {}, fill() {}, stroke() {} }; },
        toDataURL() { return 'data:image/png;base64,BADGE'; } };
    },
  };
  Object.defineProperty(document, 'title', {
    // As in a browser: whitespace in a title is trimmed and collapsed, so
    // what is read back is not always what was written.
    get() { return title; },
    set(v) {
      title = String(v).replace(/[\t\n\f\r ]+/g, ' ').replace(/^ | $/g, '');
      titleWrites.push({ at: now, title: title });
    },
  });
  Object.defineProperty(document, 'hidden', {
    get() { if (hiddenThrows) throw new Error('document is gone'); return hidden; },
  });

  class FakeDate extends Date { static now() { return now + wallSkew; } }
  class FakeImage { constructor() { images.push(this); } }
  class FakeNotification {
    constructor(t, o) { this.title = t; this.options = o || {}; this.closed = false; this.at = now; notifications.push(this); }
    close() { this.closed = true; }
  }
  FakeNotification.permission = 'granted';
  FakeNotification.requestPermission = () => Promise.resolve('granted');

  const store = Object.assign({}, opts.storage || {});
  const sandbox = {
    document, Date: FakeDate, Image: FakeImage, Notification: FakeNotification,
    navigator: { vibrate() { api.vibrations++; } },
    localStorage: { getItem(k) { return k in store ? store[k] : null; }, setItem(k, v) { store[k] = String(v); } },
    console: { log() {}, warn() {}, error() {} },
    performance: opts.noPerformance ? undefined : { now() { return now - 1699999990000; } },
    setInterval(fn, ms) { const id = nextTimer++; timers.set(id, { fn, ms, next: now + ms, repeat: true }); return id; },
    setTimeout(fn, ms) { const id = nextTimer++; timers.set(id, { fn, ms: ms || 0, next: now + (ms || 0), repeat: false }); return id; },
    clearInterval(id) { timers.delete(id); },
    clearTimeout(id) { timers.delete(id); },
    addEventListener(ev, fn) { (listeners.window[ev] = listeners.window[ev] || []).push(fn); },
  };
  sandbox.window = sandbox;
  const api = {
    window: sandbox, document, link, images, notifications, titleWrites, vibrations: 0,
    get now() { return now; },
    get title() { return title; },
    get timers() { return timers.size; },
    get badged() { return link._href.indexOf('data:image') === 0; },
    get hidden() { return hidden; },
    set hiddenThrows(v) { hiddenThrows = v; },
    fire(target, ev) { (listeners[target][ev] || []).slice().forEach((fn) => fn({ type: ev })); },
    hide() { hidden = true; api.fire('document', 'visibilitychange'); },
    show() { hidden = false; api.fire('document', 'visibilitychange'); },
    setClockBack(ms) { wallSkew -= ms; },
    // The machine sleeps: the wall clock moves on, the page's clock and its
    // timers stand still.
    sleep(ms) { wallSkew += ms; },
    // Let `ms` pass. `throttle` is the least gap the browser leaves between
    // two runs of one timer: Chrome holds a tab that has been hidden for five
    // minutes to one run a minute.
    advance(ms, throttle) {
      const end = now + ms;
      for (;;) {
        let id = null, due = Infinity;
        timers.forEach((t, k) => { if (t.next < due) { due = t.next; id = k; } });
        if (id === null || due > end) break;
        const t = timers.get(id);
        now = Math.max(now, due);
        if (t.repeat) t.next = now + Math.max(t.ms, throttle || 0); else timers.delete(id);
        t.fn();
      }
      now = end;
    },
    loadImages() { images.splice(0).forEach((img) => { if (img.onload) img.onload(); }); },
    failImages() { images.splice(0).forEach((img) => { if (img.onerror) img.onerror(); }); },
  };
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: 'notify.js' });
  return api;
}
"""

_SCENARIOS = r"""
const fs = require('fs');
const NOTIFY = fs.readFileSync(process.argv[2], 'utf8');
const WARN = '⚠ Approval needed';
const STALLED = '⚠ Session stalled';
const CARD = { id: 'c1', title: 'Release 4.2', message: 'Click Approve or Cancel in your browser to continue.',
               source: 'pipeline', since: 1 };
const CARD2 = { id: 'c2', title: 'Release 4.3', message: '', source: 'pipeline', since: 2 };
const STALL = { sessionId: 's1', sessionName: 'Template Fix', title: 'Session stalled',
                body: 'Template Fix is stuck waiting on background work that is not running.' };

function fresh(opts) { const b = makeBrowser(NOTIFY, opts); return [b, b.window.VNNotify]; }
function writes(b, ms, throttle) { const from = b.titleWrites.length; b.advance(ms, throttle); return b.titleWrites.slice(from).map((w) => w.title); }
function look(b) { return { title: b.title, timers: b.timers, badged: b.badged }; }
function banners(b) { return b.notifications.map((n) => ({ title: n.title, body: n.options.body, tag: n.options.tag, closed: n.closed })); }
// The server's side of an open approval: the same snapshot every 3 seconds.
function keepOpen(b, N, cards, ms, throttle) {
  let out = [];
  for (let t = 0; t < ms; t += 3000) { N.syncApprovals(cards, 10000); out = out.concat(writes(b, Math.min(3000, ms - t), throttle)); }
  return out;
}
function untilWarningShows(b) { for (let i = 0; i < 20 && b.title.charAt(0) !== '⚠'; i++) b.advance(250); return b.title; }

const S = {
  answered_while_hidden() {
    const [b, N] = fresh();
    b.hide();
    const during = keepOpen(b, N, [CARD], 9000);
    b.loadImages();
    const flashing = look(b);
    N.syncApprovals([], 10000);                    // the server saw the page answered
    const atOnce = look(b);
    return { during, flashing, atOnce, later: writes(b, 30 * 60 * 1000), end: look(b) };
  },
  answered_while_the_warning_is_in_the_title() {
    const [b, N] = fresh();
    b.hide();
    N.syncApprovals([CARD], 10000);
    const shown = untilWarningShows(b);
    N.syncApprovals([], 10000);
    return { shown, atOnce: look(b), later: writes(b, 60000) };
  },
  lease_runs_out_when_the_server_goes_quiet() {
    const [b, N] = fresh();
    b.hide();
    N.syncApprovals([CARD], 10000);
    const t0 = b.now;
    b.loadImages();
    b.advance(5 * 60 * 1000);
    const warnings = b.titleWrites.filter((w) => w.title === WARN).map((w) => w.at - t0);
    return { warnings, last: warnings[warnings.length - 1], end: look(b), banners: banners(b) };
  },
  kept_alive_by_snapshots() {
    const [b, N] = fresh();
    b.hide();
    const during = keepOpen(b, N, [CARD], 10 * 60 * 1000);
    return { warnings: during.filter((t) => t === WARN).length, state: N.state(), look: look(b) };
  },
  answered_on_screen_then_hidden_hours_later() {
    const [b, N] = fresh();
    N.notice({ key: 'external:pipeline', title: CARD.title, body: CARD.message, banner: false });
    keepOpen(b, N, [CARD], 6000);
    N.syncApprovals([], 10000);
    b.advance(2 * 3600 * 1000);
    b.hide();
    return { writes: writes(b, 60000), look: look(b), banners: banners(b) };
  },
  never_answered_on_screen_but_the_server_went_quiet() {
    const [b, N] = fresh();
    N.syncApprovals([CARD], 10000);
    b.advance(2 * 3600 * 1000);                    // no snapshot since
    b.hide();
    return { writes: writes(b, 60000), look: look(b), banners: banners(b) };
  },
  still_open_when_the_tab_goes_to_the_background() {
    const [b, N] = fresh();
    const onScreen = keepOpen(b, N, [CARD], 9000);
    const bannersOnScreen = banners(b).length;
    b.hide();
    const hidden = keepOpen(b, N, [CARD], 6000);
    const bannersHidden = banners(b);
    N.syncApprovals([], 10000);
    return { onScreen, hidden, bannersOnScreen, bannersHidden, atOnce: look(b), bannersAfter: banners(b) };
  },
  looking_at_the_tab_answers_it_here() {
    const [b, N] = fresh();
    b.hide();
    N.syncApprovals([CARD], 10000);
    const shown = untilWarningShows(b);
    const bannerUp = banners(b);
    b.show();
    const atOnce = look(b);
    const bannerAtOnce = banners(b);
    keepOpen(b, N, [CARD], 6000);
    b.hide();                                      // still open, already seen
    const again = keepOpen(b, N, [CARD], 9000);
    const other = keepOpen(b, N, [CARD, CARD2], 6000);     // a new approval is new
    return { shown, atOnce, again, bannerUp, bannerAtOnce, other: other.filter((t) => t === WARN).length };
  },
  a_ping_never_flashes() {
    const [b, N] = fresh();
    b.hide();
    N.notice({ key: 'external:pipeline', title: '4.2: live', body: 'Released in 9m 12s' });
    const first = { writes: writes(b, 10 * 60 * 1000), look: look(b), banners: banners(b), vibrations: b.vibrations };
    b.advance(5000);
    N.notice({ key: 'external:pipeline', title: CARD.title, body: CARD.message, banner: false });
    return { first, bannersAfterApprovalPing: banners(b).length, vibrations: b.vibrations, look: look(b), state: N.state() };
  },
  a_ping_is_deduplicated() {
    const [b, N] = fresh();
    b.hide();
    N.notice({ key: 'external:pipeline', title: 'a', body: '' });
    N.notice({ key: 'external:pipeline', title: 'a', body: '' });
    b.advance(3100);
    N.notice({ key: 'external:pipeline', title: 'a', body: '' });
    return { vibrations: b.vibrations, banners: banners(b).length };
  },
  a_stalled_session_flashes_until_looked_at() {
    const [b, N] = fresh();
    b.hide();
    N.attention(STALL);
    const first = writes(b, 5000);
    N.syncApprovals([], 10000);                    // approvals have nothing to do with it
    const afterSnapshot = look(b);
    const long = writes(b, 60 * 60 * 1000).filter((t) => t === STALLED).length;
    const bannerUp = banners(b);
    b.show();
    return { first, afterSnapshot: afterSnapshot.timers, long, atOnce: look(b), later: writes(b, 60000), bannerUp, banners: banners(b) };
  },
  a_stall_seen_on_screen_flashes_when_the_tab_is_hidden() {
    const [b, N] = fresh();
    N.attention(STALL);
    const onScreen = writes(b, 5000);
    const bannersOnScreen = banners(b).length;
    b.hide();
    const hidden = writes(b, 4000);
    b.show();
    b.hide();
    return { onScreen, bannersOnScreen, hidden, banners: banners(b), again: writes(b, 10000) };
  },
  approval_and_stall_together() {
    const [b, N] = fresh();
    b.hide();
    N.attention(STALL);
    const both = keepOpen(b, N, [CARD], 6000);
    N.syncApprovals([], 10000);
    const timersAfterAnswer = b.timers > 0;
    const stallOnly = writes(b, 6000);
    b.show();
    return { both, timersAfterAnswer, stallOnly, atOnce: look(b) };
  },
  throttled_tab_stops_on_the_answer_without_waiting_for_a_tick() {
    const [b, N] = fresh();
    b.hide();
    keepOpen(b, N, [CARD], 7 * 60 * 1000, 60000);
    N.syncApprovals([CARD], 10000);
    for (let i = 0; i < 40 && b.title !== WARN; i++) { N.syncApprovals([CARD], 10000); b.advance(3000, 60000); }
    const shown = b.title;
    N.syncApprovals([], 10000);
    return { shown, atOnce: look(b), later: writes(b, 10 * 60 * 1000, 60000) };
  },
  throttled_tab_writes_no_warning_after_the_lease() {
    // The socket died in a tab whose timer runs once a minute. Nothing tells
    // it the approval is over; its first tick after the lease must be a stop.
    const out = [];
    for (let phase = 0; phase < 60; phase += 7) {
      const [b, N] = fresh();
      b.hide();
      b.advance(phase * 1000);
      keepOpen(b, N, [CARD], 6 * 60 * 1000 + phase * 500, 60000);
      N.syncApprovals([CARD], 10000);
      const leaseEnd = b.now + 10000;
      b.advance(5 * 60 * 1000, 60000);
      const late = b.titleWrites.filter((w) => w.title === WARN && w.at >= leaseEnd).length;
      const quietAt = b.titleWrites.length ? b.titleWrites[b.titleWrites.length - 1].at - leaseEnd : 0;
      out.push({ late, quietAt, look: look(b) });
    }
    return out;
  },
  frozen_page_is_left_without_a_warning() {
    const [b, N] = fresh();
    b.hide();
    N.syncApprovals([CARD], 10000);
    b.loadImages();
    const shown = untilWarningShows(b);
    b.fire('document', 'freeze');
    const frozen = look(b);
    b.advance(30 * 60 * 1000);
    b.fire('document', 'resume');
    const resumed = look(b);
    return { shown, frozen, resumed, later: writes(b, 60000) };
  },
  a_short_freeze_resumes_an_approval_that_is_still_vouched_for() {
    const [b, N] = fresh();
    b.hide();
    N.syncApprovals([CARD], 10000);
    untilWarningShows(b);
    b.fire('document', 'freeze');
    b.advance(2000);
    b.fire('document', 'resume');
    return { timers: b.timers, writes: writes(b, 3000) };
  },
  page_in_the_back_forward_cache_is_left_without_a_warning() {
    const [b, N] = fresh();
    b.hide();
    N.syncApprovals([CARD], 10000);
    const shown = untilWarningShows(b);
    b.fire('window', 'pagehide');
    return { shown, parked: look(b) };
  },
  favicon_dot_follows_the_flash() {
    const [b, N] = fresh();
    b.hide();
    N.syncApprovals([CARD], 10000);
    const beforeLoad = b.badged;
    b.loadImages();
    const loaded = b.badged;
    N.syncApprovals([], 10000);
    const answered = b.badged;
    N.syncApprovals([CARD2], 10000);               // the badge is cached now
    const secondAtOnce = b.badged;
    b.show();
    return { beforeLoad, loaded, answered, secondAtOnce, shown: b.badged, href: b.link.href };
  },
  favicon_image_that_loads_after_the_flash_stopped() {
    const out = {};
    for (const how of ['loadImages', 'failImages']) {
      const [b, N] = fresh();
      b.hide();
      N.syncApprovals([CARD], 10000);
      N.syncApprovals([], 10000);                  // answered before the icon image arrived
      b[how]();
      const late = b.badged;
      N.syncApprovals([CARD2], 10000);             // and the dot still works afterwards
      const next = b.badged;
      N.syncApprovals([], 10000);
      out[how] = { late, next, end: b.badged, href: b.link.href };
    }
    return out;
  },
  another_module_retitles_the_page() {
    const [b, N] = fresh();
    b.hide();
    N.syncApprovals([CARD], 10000);
    untilWarningShows(b);
    b.document.title = 'VibeNode – Project X';      // while the warning is up
    N.syncApprovals([], 10000);
    const whileWarning = b.title;

    const [c, M] = fresh();
    c.hide();
    M.syncApprovals([CARD], 10000);
    untilWarningShows(c);
    for (let i = 0; i < 20 && c.title.charAt(0) === '⚠'; i++) c.advance(250);
    c.document.title = 'VibeNode – Project X';      // between two warnings
    M.syncApprovals([CARD], 10000);
    const next = untilWarningShows(c);
    M.syncApprovals([], 10000);
    return { whileWarning, next, betweenWarnings: c.title };
  },
  two_approvals() {
    const [b, N] = fresh();
    b.hide();
    keepOpen(b, N, [CARD, CARD2], 6000);
    const one = keepOpen(b, N, [CARD2], 6000).filter((t) => t === WARN).length;
    const bannersOne = banners(b);
    N.syncApprovals([], 10000);
    return { one, bannersOne, atOnce: look(b), banners: banners(b) };
  },
  switched_off() {
    const [b, N] = fresh({ storage: { vn_notify_approvals: '0' } });
    b.hide();
    N.notice({ key: 'k', title: 'x', body: 'y' });
    N.attention(STALL);
    const off = { writes: keepOpen(b, N, [CARD], 9000), look: look(b), banners: banners(b).length, vibrations: b.vibrations };

    const [c, M] = fresh();
    c.hide();
    M.syncApprovals([CARD], 10000);
    untilWarningShows(c);
    M.setEnabled(false);
    return { off, switchedOffMidFlash: look(c), later: keepOpen(c, M, [CARD], 9000) };
  },
  banner_goes_with_the_approval() {
    const [b, N] = fresh();
    b.hide();
    keepOpen(b, N, [CARD], 9000);
    const open = banners(b);
    N.syncApprovals([], 10000);
    const answered = banners(b);

    const [c, M] = fresh();
    c.hide();
    M.syncApprovals([CARD], 10000);
    c.advance(60000);                              // the server went quiet instead
    return { open, answered, quiet: banners(c) };
  },
  an_approval_that_comes_back_keeps_what_the_user_did() {
    // The server lost sight of the page for a moment (a stalled machine), then
    // saw it again: same id. The user had already looked at this tab.
    const [b, N] = fresh();
    b.hide();
    keepOpen(b, N, [CARD], 3000);
    b.show();
    N.syncApprovals([], 10000);
    N.syncApprovals([CARD], 10000);
    b.hide();
    return { writes: keepOpen(b, N, [CARD], 9000), banners: banners(b).length, state: N.state() };
  },
  malformed_snapshots() {
    const [b, N] = fresh();
    b.hide();
    const bad = [undefined, null, 'x', 7, {}, [null, {}, { id: '' }, 7, 'c1'], [{ title: 'no id' }]];
    bad.forEach((v) => { N.syncApprovals(v, 10000); N.syncApprovals(v); N.syncApprovals(v, 'abc'); });
    const none = { writes: writes(b, 5000), look: look(b), state: N.state() };
    const lease = {};
    [['missing', undefined], ['text', 'soon'], ['negative', -5], ['zero', 0], ['huge', 1e15]].forEach(([name, ttl]) => {
      const [c, M] = fresh();
      c.hide();
      M.syncApprovals([{ id: 9, title: null }], ttl);
      const t0 = c.now;
      c.advance(10 * 60 * 1000);
      const w = c.titleWrites.filter((x) => x.title === WARN);
      lease[name] = { flashed: w.length > 0, lastMs: w.length ? w[w.length - 1].at - t0 : 0, look: look(c) };
    });
    return { none, lease };
  },
  the_wall_clock_is_set_back() {
    // The socket is dead and the clock is moved an hour back. A lease held
    // against the wall clock alone would now last an hour and ten seconds.
    const [b, N] = fresh();
    b.hide();
    N.syncApprovals([CARD], 10000);
    const t0 = b.now;
    b.advance(2000);
    b.setClockBack(3600 * 1000);
    b.advance(10 * 60 * 1000);
    const w = b.titleWrites.filter((x) => x.title === WARN).map((x) => x.at - t0);
    return { last: w[w.length - 1], look: look(b), state: N.state() };
  },
  the_machine_sleeps() {
    // Lid closed for an hour. On waking nothing has told the tab anything
    // yet, and its own clock says no time has passed.
    const [b, N] = fresh();
    b.hide();
    N.syncApprovals([CARD], 10000);
    const shown = untilWarningShows(b);
    const from = b.titleWrites.length;
    b.sleep(3600 * 1000);
    b.advance(8000);
    return { shown, writes: b.titleWrites.slice(from).map((w) => w.title), look: look(b) };
  },
  a_browser_without_a_monotonic_clock() {
    const [b, N] = fresh({ noPerformance: true });
    b.hide();
    N.syncApprovals([CARD], 10000);
    const t0 = b.now;
    b.advance(60000);
    const w = b.titleWrites.filter((x) => x.title === WARN).map((x) => x.at - t0);
    return { warnings: w.length, last: w[w.length - 1], look: look(b) };
  },
  a_banner_never_outlives_the_flash() {
    // The banner is up. The user glances at the tab and leaves again. Then the
    // approval is answered while this tab hears nothing (the web server was
    // restarted, the socket dropped). A tab that is not flashing has no tick
    // to notice with, so the banner must already be gone.
    const [b, N] = fresh();
    b.hide();
    keepOpen(b, N, [CARD], 3000);
    const up = banners(b);
    b.show();
    const looked = banners(b);
    b.hide();
    b.advance(6 * 3600 * 1000);
    return { up, looked, later: banners(b), look: look(b) };
  },
  a_stopped_page_leaves_no_banner() {
    const out = {};
    [['document', 'freeze'], ['window', 'pagehide']].forEach(([target, ev]) => {
      const [b, N] = fresh();
      b.hide();
      N.attention(STALL);
      keepOpen(b, N, [CARD], 3000);
      const up = banners(b).filter((x) => !x.closed).length;
      b.fire(target, ev);
      out[ev] = { up, left: banners(b).filter((x) => !x.closed).length, look: look(b) };
    });
    return out;
  },
  a_warning_the_browser_rewrote() {
    // A warning that is compared against what was written, not against what
    // the browser kept, is never recognised again and stays for good.
    const [b, N] = fresh();
    b.hide();
    N.attention({ sessionId: 's1', sessionName: 'x', title: '  Session   stalled \n', body: 'y' });
    const shown = untilWarningShows(b);
    b.show();
    return { shown, look: look(b), state: N.state() };
  },
  an_error_while_reconciling_turns_everything_off() {
    const [b, N] = fresh();
    b.hide();
    N.attention(STALL);
    keepOpen(b, N, [CARD], 3000);
    b.loadImages();
    const shown = untilWarningShows(b);
    const up = banners(b).filter((x) => !x.closed).length;
    b.hiddenThrows = true;
    N.syncApprovals([CARD], 10000);
    const after = { look: look(b), banners: banners(b).filter((x) => !x.closed).length };
    b.hiddenThrows = false;
    return { shown, up, after };
  },
  old_approvals_are_forgotten() {
    const [b, N] = fresh();
    b.hide();
    for (let i = 0; i < 120; i++) { N.syncApprovals([{ id: 'x' + i, title: 't' }], 10000); b.advance(100); }
    N.syncApprovals([], 10000);
    const remembered = N.state().remembered;
    const quiet = look(b);
    const again = keepOpen(b, N, [CARD], 6000).filter((t) => t === WARN).length;
    // A snapshot far longer than any the server sends.
    const flood = [];
    for (let i = 0; i < 500; i++) flood.push({ id: 'f' + i, title: 't' });
    N.syncApprovals(flood, 10000);
    const flooded = { remembered: N.state().remembered, up: banners(b).filter((x) => !x.closed).length };
    N.syncApprovals([], 10000);
    return { remembered, look: quiet, again, flooded, left: banners(b).filter((x) => !x.closed).length, end: look(b) };
  },
  an_error_in_a_tick_stops_the_flash() {
    const [b, N] = fresh();
    b.hide();
    N.syncApprovals([CARD], 10000);
    const shown = untilWarningShows(b);
    b.hiddenThrows = true;
    b.advance(1500);
    const afterError = { timers: b.timers, title: b.title, badged: b.badged };
    b.hiddenThrows = false;
    return { shown, afterError };
  },
};

if (process.argv[3] === 'scenarios') {
  const out = {};
  Object.keys(S).forEach((name) => {
    try { out[name] = S[name](); } catch (e) { out[name] = { error: String((e && e.stack) || e) }; }
  });
  process.stdout.write(JSON.stringify(out));
}
"""

# Random interleavings against an independent model of when flashing is
# allowed.  The model is deliberately not notify.js: a lease table, a "seen"
# flag per approval, a set of stalled sessions, and the tab's visibility.
_FUZZ = r"""
function fuzz(seed, steps, throttle) {
  let s = seed >>> 0;
  const rnd = () => { s = (s + 0x6D2B79F5) >>> 0; let t = s; t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
  const pick = (a) => a[Math.floor(rnd() * a.length)];
  const b = makeBrowser(NOTIFY);
  const N = b.window.VNNotify;
  const IDS = ['a', 'b', 'c', 'd'];

  // the model
  const lease = {};                 // id -> { until, seen }
  let stalls = {};                  // key -> true
  const lastStall = {};
  let hidden = false, enabled = true, pageTitle = 'VibeNode';
  const liveAt = (t) => IDS.some((id) => lease[id] && !lease[id].seen && t < lease[id].until);
  const allowedAt = (t) => hidden && enabled && (Object.keys(stalls).length > 0 || liveAt(t));

  const problems = [];
  let checkedWrites = 0, warnings = 0, i = 0;
  const fail = (what) => { if (problems.length < 5) problems.push({ seed, step: i, throttle, what, title: b.title, timers: b.timers, badged: b.badged }); };

  // Every title that was written: a warning only while flashing was allowed,
  // anything else only the page's own title.
  const audit = () => {
    for (; checkedWrites < b.titleWrites.length; checkedWrites++) {
      const w = b.titleWrites[checkedWrites];
      if (w.foreign) continue;
      if (w.title.charAt(0) === '⚠') { warnings++; if (!allowedAt(w.at)) fail('wrote "' + w.title + '" for a reason that was over'); }
      else if (w.title !== pageTitle) fail('put back "' + w.title + '", the page is titled "' + pageTitle + '"');
    }
  };
  // An OS banner is up only for a reason the title is flashing for.
  const bannersMatch = (after) => {
    b.notifications.forEach((n) => {
      const tag = String(n.options.tag || '');
      if (n.closed) return;
      if (tag.indexOf('vn-approval-') === 0) {
        const l = lease[tag.slice(12)];
        if (!(hidden && enabled && l && !l.seen && b.now < l.until)) fail('banner still up for approval ' + tag.slice(12) + ' after ' + after);
      } else if (tag.indexOf('vn-attention:') === 0) {
        if (!(hidden && enabled && stalls[tag.slice(3)])) fail('banner still up for ' + tag.slice(3) + ' after ' + after);
      }
    });
  };
  const noBanner = (what) => {
    if (b.notifications.some((n) => !n.closed && n.options.tag !== 'vn-notice')) fail(what + ' left a banner up');
  };
  // Nothing may be on when nothing is allowed.
  const quietIfOver = (after) => {
    bannersMatch(after);
    if (allowedAt(b.now)) return;
    if (b.title.charAt(0) === '⚠') fail('title still warns after ' + after);
    else if (b.title !== pageTitle) fail('wrong title after ' + after);
    if (b.timers) fail('timer still running after ' + after);
    if (b.badged) fail('favicon dot still on after ' + after);
  };
  const gap = Math.max(1000, throttle);

  for (i = 0; i < steps; i++) {
    const r = rnd();
    if (r < 0.30) {                                        // a snapshot from the server
      const ids = IDS.filter(() => rnd() < 0.35);
      const ttl = pick([10000, 10000, 10000, 4000, 30000, 90000, undefined]);
      N.syncApprovals(ids.map((id) => ({ id, title: 'Release ' + id, message: 'm' })), ttl);
      const eff = (typeof ttl === 'number' && ttl > 0) ? Math.min(ttl, 30000) : 10000;
      IDS.forEach((id) => {
        if (ids.indexOf(id) >= 0) { lease[id] = lease[id] || { seen: false }; lease[id].until = b.now + eff; }
        else if (lease[id]) lease[id].until = 0;
      });
      audit(); quietIfOver('a snapshot');
    } else if (r < 0.62) {                                 // time passes
      b.advance(pick([200, 900, 1000, 2500, 3000, 3000, 7000, 12000, 45000, 400000]), throttle);
      audit();
      if (!allowedAt(b.now)) { b.advance(gap, throttle); audit(); quietIfOver('one more tick'); }
    } else if (r < 0.72) {                                 // the tab is put in the background
      hidden = true; b.hide(); audit(); quietIfOver('hiding');
    } else if (r < 0.80) {                                 // the tab is looked at
      hidden = false; IDS.forEach((id) => { if (lease[id]) lease[id].seen = true; }); stalls = {};
      b.show(); audit(); quietIfOver('showing');
      if (b.title.charAt(0) === '⚠' || b.timers || b.badged) fail('a tab on screen is flashing');
    } else if (r < 0.86) {                                 // a stalled session
      const key = pick(['s1', 's2']);
      if (enabled && !(b.now - (lastStall[key] || 0) < 3000)) { lastStall[key] = b.now; stalls['attention:' + key] = true; }
      N.attention({ sessionId: key, sessionName: 'x', title: 'Session stalled', body: 'y' });
      audit();
    } else if (r < 0.90) {                                 // a one-off ping
      const before = b.titleWrites.length, timers = b.timers;
      N.notice({ key: pick(['external:pipeline', 'k2']), title: '4.2: live', body: 'done', banner: rnd() < 0.5 });
      if (b.titleWrites.length !== before || b.timers !== timers) fail('a ping touched the title or a timer');
    } else if (r < 0.93) {                                 // the browser freezes the page
      b.fire('document', 'freeze');
      if (b.title.charAt(0) === '⚠' || b.timers || b.badged) fail('a frozen page is flashing');
      noBanner('a frozen page');
      b.advance(pick([500, 5000, 600000]), throttle);
      b.fire('document', 'resume'); audit(); quietIfOver('resume');
    } else if (r < 0.95) {                                 // back/forward cache
      b.fire('window', 'pagehide');
      if (b.title.charAt(0) === '⚠' || b.timers || b.badged) fail('a parked page is flashing');
      noBanner('a parked page');
      b.fire('window', 'pageshow'); audit(); quietIfOver('pageshow');
    } else if (r < 0.97) {                                 // the icon image arrives, or fails
      if (rnd() < 0.8) b.loadImages(); else b.failImages();
      if (b.badged && !b.timers) fail('favicon dot on with no flash running');
    } else if (r < 0.985) {                                // another module retitles the page
      pageTitle = pick(['VibeNode', 'VibeNode – Project X', 'VibeNode – VibeNode']);
      b.document.title = pageTitle;
      b.titleWrites[b.titleWrites.length - 1].foreign = true;
    } else {                                               // the user switches pings off or on
      enabled = rnd() < 0.5; N.setEnabled(enabled); audit(); quietIfOver('the switch');
    }
  }
  // Whatever state it ended in: once every reason is over, it goes quiet.
  hidden = false; IDS.forEach((id) => { if (lease[id]) lease[id].seen = true; }); stalls = {};
  b.show(); audit(); quietIfOver('the end');
  if (b.title !== pageTitle || b.timers || b.badged) fail('not quiet at the end');
  noBanner('the end');
  return { problems, warnings };
}

if (process.argv[3] === 'fuzz') {
  let problems = [], warnings = 0, runs = 0;
  for (let seed = 1; seed <= 240; seed++) {
    const r = fuzz(seed, 500, seed % 3 === 0 ? 60000 : 0);
    runs++; warnings += r.warnings; problems = problems.concat(r.problems);
  }
  process.stdout.write(JSON.stringify({ runs, warnings, problems: problems.slice(0, 8), failed: problems.length }));
}
"""

# The server's payloads, through the real socket.js handlers, into notify.js.
_CHAIN = r"""
if (process.argv[3] === 'chain') {
  const input = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
  const b = makeBrowser(NOTIFY);
  const handlers = {};
  b.window.socket = { on(ev, fn) { handlers[ev] = fn; } };
  vm.runInContext(input.handlers, b.window, { filename: 'socket.js (the two handlers)' });
  const out = [];
  input.steps.forEach((step) => {
    if (step.do === 'hide') b.hide();
    else if (step.do === 'show') b.show();
    else if (step.do === 'push') handlers[step.event](step.data);
    const from = b.titleWrites.length;
    b.advance(step.then || 0);
    out.push({ title: b.title, timers: b.timers, badged: b.badged, vibrations: b.vibrations,
               writes: b.titleWrites.slice(from).map((w) => w.title),
               banners: b.notifications.map((n) => [n.title, n.options.body, n.closed]) });
  });
  process.stdout.write(JSON.stringify({ handlers: Object.keys(handlers).sort(), out }));
}
"""


def _node(tmp_path_factory, mode: str, *args: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    script = tmp_path_factory.mktemp("notify") / "run.js"
    script.write_text(_FAKE_BROWSER + _SCENARIOS + _FUZZ + _CHAIN, encoding="utf-8")
    proc = subprocess.run([node, str(script), str(NOTIFY_JS), mode, *args],
                          capture_output=True, text=True, timeout=55, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def seen(tmp_path_factory):
    """What each scenario observed."""
    results = _node(tmp_path_factory, "scenarios")
    broken = {name: r["error"] for name, r in results.items() if isinstance(r, dict) and "error" in r}
    assert not broken, broken
    return results


QUIET = {"title": PAGE, "timers": 0, "badged": False}


# ── the reported bug ────────────────────────────────────────────────────────

def test_it_stops_the_moment_the_approval_is_answered(seen):
    r = seen["answered_while_hidden"]
    assert r["during"].count(WARN) >= 4 and set(r["during"]) == {WARN, PAGE}, "it flashed while it was open"
    assert r["flashing"] == {"title": r["flashing"]["title"], "timers": 1, "badged": True}
    assert r["atOnce"] == QUIET, "title, timer and favicon dot all go with the answer, not at the next tick"
    assert r["later"] == [] and r["end"] == QUIET, "and nothing touches the title in the half hour after"

    r = seen["answered_while_the_warning_is_in_the_title"]
    assert r["shown"] == WARN and r["atOnce"] == QUIET and r["later"] == []


def test_it_stops_by_itself_when_nothing_says_the_approval_is_still_open(seen):
    """Dead socket, restarted web server, crashed watcher: the tab hears
    nothing. Silence is not "still open"."""
    r = seen["lease_runs_out_when_the_server_goes_quiet"]
    assert r["warnings"], "it flashed while the snapshot was fresh"
    assert r["last"] < 10000, f"a warning was written {r['last']}ms after the last snapshot (lease: 10000ms)"
    assert r["end"] == QUIET
    assert [b["closed"] for b in r["banners"]] == [True], "its OS banner goes too"


def test_it_keeps_flashing_while_the_server_keeps_vouching_for_it(seen):
    """The other direction: an approval nobody has answered must not go quiet."""
    r = seen["kept_alive_by_snapshots"]
    assert 290 <= r["warnings"] <= 300, "one warning every two seconds for ten minutes"
    assert r["state"]["flashing"] is True and r["look"]["timers"] == 1
    assert [a["id"] for a in r["state"]["approvals"]] == ["c1"]


def test_an_approval_answered_while_the_tab_was_on_screen_never_flashes_later(seen):
    """The ping used to be remembered with no expiry: hours after the approval,
    the first time the tab went to the background, the title started flashing."""
    for name in ("answered_on_screen_then_hidden_hours_later",
                 "never_answered_on_screen_but_the_server_went_quiet"):
        r = seen[name]
        assert r["writes"] == [] and r["look"] == QUIET, name
        assert [b for b in r["banners"] if not b["closed"]] == [], name


def test_an_approval_still_open_when_the_tab_is_hidden_does_flash(seen):
    r = seen["still_open_when_the_tab_goes_to_the_background"]
    assert r["onScreen"] == [] and r["bannersOnScreen"] == 0, "a tab on screen never flashes"
    assert WARN in r["hidden"]
    assert [(b["title"], b["closed"]) for b in r["bannersHidden"]] == [("Approval needed", False)]
    assert r["atOnce"] == QUIET and [b["closed"] for b in r["bannersAfter"]] == [True]


def test_looking_at_the_tab_stops_it_for_that_approval(seen):
    r = seen["looking_at_the_tab_answers_it_here"]
    assert r["shown"] == WARN and r["atOnce"] == QUIET
    assert [b["closed"] for b in r["bannerUp"]] == [False]
    assert [b["closed"] for b in r["bannerAtOnce"]] == [True], "its banner goes when the tab is looked at"
    assert r["again"] == [], "seen is seen: hiding the tab again does not restart it"
    assert r["other"] >= 2, "a different approval is new"


# ── pings that are not approvals ────────────────────────────────────────────

def test_a_ping_with_nothing_to_answer_never_flashes(seen):
    """"4.2: live" used to flash "Approval needed" until the tab was
    looked at, and its banner read "4.2: live needs approval for ..."."""
    r = seen["a_ping_never_flashes"]
    assert r["first"]["writes"] == [] and r["first"]["look"] == QUIET
    assert r["first"]["vibrations"] == 1, "it still pings"
    assert r["first"]["banners"] == [
        {"title": "4.2: live", "body": "Released in 9m 12s", "tag": "vn-notice", "closed": False}]
    assert r["vibrations"] == 2 and r["bannersAfterApprovalPing"] == 1, \
        "an approval's ping makes the sound; its banner belongs to the approval's state"
    assert r["look"] == QUIET and r["state"]["flashing"] is False

    assert seen["a_ping_is_deduplicated"] == {"vibrations": 2, "banners": 2}


def test_a_stalled_session_still_flashes_until_the_tab_is_looked_at(seen):
    """The watchdog's escalation is a different kind of reason: nothing
    outside the tab can answer it. An approval ending must not end it."""
    r = seen["a_stalled_session_flashes_until_looked_at"]
    assert r["first"].count(STALLED) >= 2 and WARN not in r["first"]
    assert r["afterSnapshot"] == 1 and r["long"] >= 1700
    assert r["atOnce"] == QUIET and r["later"] == []
    assert [(b["title"], b["tag"], b["closed"]) for b in r["bannerUp"]] == [("Session stalled", "vn-attention:s1", False)]
    assert [b["closed"] for b in r["banners"]] == [True]

    r = seen["a_stall_seen_on_screen_flashes_when_the_tab_is_hidden"]
    assert r["onScreen"] == [] and r["bannersOnScreen"] == 0
    assert STALLED in r["hidden"] and [b["closed"] for b in r["banners"]] == [True]
    assert r["again"] == [], "looked at once, done"

    r = seen["approval_and_stall_together"]
    assert WARN in r["both"] and STALLED not in r["both"], "the approval is the more urgent label"
    assert r["timersAfterAnswer"] is True and STALLED in r["stallOnly"] and WARN not in r["stallOnly"]
    assert r["atOnce"] == QUIET


# ── timers the browser does not run on time ─────────────────────────────────

def test_a_throttled_tab_stops_on_the_answer_without_a_tick(seen):
    """Chrome runs a hidden tab's timers once a minute. The answer arrives as
    a socket message, which is not a timer."""
    r = seen["throttled_tab_stops_on_the_answer_without_waiting_for_a_tick"]
    assert r["shown"] == WARN and r["atOnce"] == QUIET and r["later"] == []


def test_a_throttled_tab_never_writes_a_warning_after_its_lease(seen):
    runs = seen["throttled_tab_writes_no_warning_after_the_lease"]
    assert len(runs) >= 8
    for r in runs:
        assert r["late"] == 0, "the tick that finds the lease over must stop, not flash once more"
        assert r["quietAt"] <= 60000, "and that tick comes within one throttled interval"
        assert r["look"] == QUIET


def test_a_frozen_or_parked_page_is_never_left_showing_a_warning(seen):
    """A frozen page cannot hear that the approval was answered."""
    r = seen["frozen_page_is_left_without_a_warning"]
    assert r["shown"] == WARN and r["frozen"] == QUIET
    assert r["resumed"] == QUIET and r["later"] == [], "its lease ran out while it was frozen"

    r = seen["a_short_freeze_resumes_an_approval_that_is_still_vouched_for"]
    assert r["timers"] == 1 and WARN in r["writes"]

    r = seen["page_in_the_back_forward_cache_is_left_without_a_warning"]
    assert r["shown"] == WARN and r["parked"] == QUIET


def test_a_lease_runs_out_on_time_whatever_the_clocks_do(seen):
    """Two clocks can each stretch a lease: the wall clock when it is set
    back, the page's own clock when the machine sleeps. Either one ends it."""
    r = seen["the_wall_clock_is_set_back"]
    assert r["last"] < 10000, f"still flashing {r['last']}ms after the last snapshot"
    assert r["look"] == QUIET and r["state"]["approvals"] == []

    r = seen["the_machine_sleeps"]
    assert r["shown"] == WARN
    assert WARN not in r["writes"] and r["look"] == QUIET, "awake again: its first tick stops it"

    r = seen["a_browser_without_a_monotonic_clock"]
    assert r["warnings"] >= 4 and r["last"] < 10000 and r["look"] == QUIET


def test_an_error_turns_everything_off(seen):
    r = seen["an_error_in_a_tick_stops_the_flash"]
    assert r["shown"] == WARN
    assert r["afterError"] == {"timers": 0, "title": PAGE, "badged": False}

    r = seen["an_error_while_reconciling_turns_everything_off"]
    assert r["shown"] == WARN and r["up"] == 2
    assert r["after"] == {"look": QUIET, "banners": 0}


def test_a_warning_the_browser_rewrote_is_still_taken_down(seen):
    """A browser trims and collapses whitespace in a title. Found in real
    Chromium: a label with a double space left its warning in the title for
    good, while the module believed it was not flashing."""
    r = seen["a_warning_the_browser_rewrote"]
    assert r["shown"] == "⚠ Session stalled"
    assert r["look"] == QUIET and r["state"]["warning"] is None and r["state"]["flashing"] is False


def test_ended_approvals_are_not_remembered_for_ever(seen):
    r = seen["old_approvals_are_forgotten"]
    assert r["remembered"] <= 50 and r["look"] == QUIET
    assert r["again"] >= 2
    assert r["flooded"]["remembered"] <= 50 and r["flooded"]["up"] <= 50, "whatever a snapshot holds"
    assert r["left"] == 0 and r["end"] == QUIET, "and a forgotten approval leaves no banner behind"


# ── everything else that stays on ───────────────────────────────────────────

def test_the_favicon_dot_goes_on_and_off_with_the_flash(seen):
    r = seen["favicon_dot_follows_the_flash"]
    assert r == {"beforeLoad": False, "loaded": True, "answered": False, "secondAtOnce": True,
                 "shown": False, "href": "/static/images/logo.png"}


def test_a_favicon_image_that_loads_late_cannot_paint_the_dot_on_a_stopped_flash(seen):
    """The badge is drawn from an image that loads later. It used to be painted
    whenever the image arrived, and nothing took it off again."""
    for how, r in seen["favicon_image_that_loads_after_the_flash_stopped"].items():
        assert r["late"] is False, f"{how}: the dot appeared after the approval was answered"
        assert r["next"] is True and r["end"] is False and r["href"] == "/static/images/logo.png", how


def test_a_title_another_module_set_is_never_overwritten(seen):
    r = seen["another_module_retitles_the_page"]
    assert r["whileWarning"] == "VibeNode – Project X"
    assert r["next"] == WARN and r["betweenWarnings"] == "VibeNode – Project X"


def test_two_approvals_flash_until_both_are_over(seen):
    r = seen["two_approvals"]
    assert r["one"] >= 2
    assert sorted((b["tag"], b["closed"]) for b in r["bannersOne"]) == [
        ("vn-approval-c1", True), ("vn-approval-c2", False)]
    assert r["atOnce"] == QUIET and all(b["closed"] for b in r["banners"])


def test_the_os_banner_goes_with_the_approval(seen):
    r = seen["banner_goes_with_the_approval"]
    assert r["open"] == [{
        "title": "Approval needed", "tag": "vn-approval-c1", "closed": False,
        "body": "Release 4.2 — Click Approve or Cancel in your browser to continue."}], \
        "one banner, however many snapshots repeat the approval"
    assert [b["closed"] for b in r["answered"]] == [True]
    assert [b["closed"] for b in r["quiet"]] == [True]


def test_a_banner_never_outlives_the_flash(seen):
    """A banner is up only while the title is flashing for its reason. Left up
    on a tab that was not flashing, it had nothing to take it down: it stayed
    after the approval was answered, for as long as the tab stayed hidden."""
    r = seen["a_banner_never_outlives_the_flash"]
    assert [b["closed"] for b in r["up"]] == [False]
    assert [b["closed"] for b in r["looked"]] == [True]
    assert [b["closed"] for b in r["later"]] == [True] and r["look"] == QUIET

    for event, state in seen["a_stopped_page_leaves_no_banner"].items():
        assert state["up"] == 2, event
        assert state["left"] == 0 and state["look"] == QUIET, \
            f"{event}: a page that stops running cannot take a banner down later"


def test_an_approval_that_comes_back_does_not_start_over(seen):
    r = seen["an_approval_that_comes_back_keeps_what_the_user_did"]
    assert r["writes"] == [] and r["banners"] == 1
    assert r["state"]["approvals"][0]["acked"] is True


def test_switched_off_means_nothing_at_all(seen):
    r = seen["switched_off"]
    assert r["off"] == {"writes": [], "look": QUIET, "banners": 0, "vibrations": 0}
    assert r["switchedOffMidFlash"] == QUIET and r["later"] == []


def test_a_snapshot_it_cannot_read_never_starts_or_prolongs_a_flash(seen):
    r = seen["malformed_snapshots"]
    assert r["none"]["writes"] == [] and r["none"]["look"] == QUIET and r["none"]["state"]["approvals"] == []
    for name, lease in r["lease"].items():
        assert lease["flashed"], name
        assert lease["look"] == QUIET, name
        limit = 30000 if name == "huge" else 10000
        assert lease["lastMs"] < limit, f"{name}: a snapshot is never believed for longer than {limit}ms"


# ── the invariant ───────────────────────────────────────────────────────────

def test_no_interleaving_flashes_for_a_reason_that_is_over(tmp_path_factory):
    """240 random histories of 500 steps: snapshots with every subset of
    approvals, tab switches, stalls, pings, freezes, late icon loads, another
    module retitling the page, and a clock that runs the timer on time or once
    a minute. Against a model that only knows when flashing is allowed:

    * no warning is ever written to the title at a moment it was not allowed;
    * after anything that ends the last reason, the title is the page's own,
      no timer is running and the favicon dot is off, at once;
    * a reason that only runs out with time is gone by the next tick.
    """
    r = _node(tmp_path_factory, "fuzz")
    assert r["failed"] == 0, json.dumps(r["problems"], indent=2, ensure_ascii=False)
    assert r["runs"] == 240 and r["warnings"] > 5000, "the histories did flash, so the check was not vacuous"


# ── the chain: server payloads -> socket.js handlers -> notify.js ───────────

WIRING = {"external_notice": "_onExternalNotice", "approval_needed": "_onExternalNotice",
          "approvals_open": "_onApprovalsOpen"}


def _socket_handlers():
    """(source, wiring): the two handler functions in socket.js, with the
    ``socket.on`` lines that register them, and which event goes to which."""
    src = SOCKET_JS.read_text(encoding="utf-8")
    wiring = dict(re.findall(r"^socket\.on\('(\w+)', (_on(?:ExternalNotice|ApprovalsOpen))\);$", src, flags=re.M))
    source = "\n".join(
        [_function_source(src, "_onExternalNotice"), _function_source(src, "_onApprovalsOpen")]
        + [f"socket.on('{event}', {fn});" for event, fn in wiring.items()])
    return source, wiring


def test_the_tab_follows_the_server_from_the_ping_to_the_answer(tmp_path, tmp_path_factory):
    """The whole path with nothing faked but the socket and the browser: a real
    approval page on a local port, the real routes, the real watch, the real
    socket.js handlers, the real notify.js."""
    from tests.test_approval_watch import Card, Clock, card_page, PROGRESS_PAGE, TITLE as title
    from app import approval_watch as aw, create_app
    from app.routes import main as main_routes

    emitted = []
    clock = Clock()
    watch = aw.ApprovalWatch(emit=lambda event, data: emitted.append((event, data)), clock=clock, threaded=False)
    card = Card(card_page(title))
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(aw, "watch", watch)
        mp.setattr(main_routes.socketio, "emit", lambda event, data=None, **kw: emitted.append((event, data)))
        client = create_app(testing=True).test_client()

        steps = [{"do": "hide"}]
        client.post("/api/notify/approval", json={
            "title": title, "message": "Click Approve or Cancel in your browser to continue.",
            "source": "pipeline", "url": card.url})
        watch.step()
        for _ in range(3):                                   # heartbeats while it waits
            clock.tick(aw.HEARTBEAT_S)
            watch.step()
        card.page = PROGRESS_PAGE                            # "Release to Production" tapped
        clock.tick(aw.PROBE_FAST_S)
        watch.step()
        answered_at = len(emitted)
        client.post("/api/notify/approval", json={           # 9 minutes later
            "title": "4.2: live", "message": "Released in 9m 12s", "source": "pipeline", "url": ""})
        watch.step()
    finally:
        mp.undo()
        card.close()
        watch.stop()

    assert [e for e, _ in emitted] == ["external_notice"] + ["approvals_open"] * 5 + ["external_notice"]
    for n, (event, data) in enumerate(emitted):
        steps.append({"do": "push", "event": event, "data": data,
                      "then": 600000 if n >= answered_at - 1 else 3000})
    # And the ping as a web server that has not been restarted yet sends it:
    # the old event name, no id, and no approvals_open to follow.
    steps.append({"do": "push", "event": "approval_needed", "then": 600000, "data": {
        "title": "Release 4.3", "message": "Click Approve or Cancel in your browser to continue.",
        "source": "pipeline", "url": "http://127.0.0.1:40125/"}})

    source, wiring = _socket_handlers()
    assert wiring == WIRING
    args = tmp_path / "chain.json"
    args.write_text(json.dumps({"handlers": source, "steps": steps}), encoding="utf-8")
    r = _node(tmp_path_factory, "chain", str(args))
    assert r["handlers"] == sorted(WIRING)
    out = r["out"][1:]                                       # one entry per pushed event

    assert out[0]["vibrations"] == 1 and out[0]["writes"] == [] and out[0]["banners"] == [], \
        "the ping chimes; it starts nothing"
    waiting = out[1:answered_at - 1]
    assert len(waiting) == 4
    for step in waiting:
        assert WARN in step["writes"] and step["timers"] == 1, "open: it flashes"
    assert waiting[0]["banners"] == [[
        "Approval needed", title + " — Click Approve or Cancel in your browser to continue.", False]]

    answered = out[answered_at - 1]
    assert answered["writes"] in ([], [PAGE]), "the snapshot without it puts the title back, once"
    assert answered["title"] == PAGE and answered["timers"] == 0 and answered["badged"] is False
    assert answered["banners"][0][2] is True, "and takes the banner down"

    live = out[answered_at]
    assert live["writes"] == [] and live["title"] == PAGE and live["timers"] == 0, \
        '"4.2: live" ten minutes after the approval: a chime, never a flash'
    assert live["vibrations"] == 2
    assert live["banners"][-1] == ["4.2: live", "Released in 9m 12s", False]

    old_server = out[-1]
    assert old_server["vibrations"] == 3, "a ping from a server not yet restarted still chimes"
    assert old_server["banners"][-1] == [
        "Release 4.3", "Click Approve or Cancel in your browser to continue.", False]
    assert old_server["writes"] == [] and old_server["title"] == PAGE and old_server["timers"] == 0, \
        "and with nothing to say when it is answered, it must not flash at all"


# ── source guards: no Node needed ───────────────────────────────────────────

def _code(src: str) -> str:
    """The source without // comments and /* */ blocks."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(line.split("//")[0] for line in src.splitlines())


def _function_source(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    i = src.index("{", start)
    depth = 0
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces in {name}")


def test_the_title_is_written_in_two_places_and_only_after_the_reason_is_checked():
    src = NOTIFY_JS.read_text(encoding="utf-8")
    code = _code(src)
    assert len(re.findall(r"document\.title\s*=[^=]", code)) == 2, \
        "document.title is written by _showWarning and _restoreTitle and nowhere else"
    assert "document.title = text" in _function_source(src, "_showWarning")
    assert "document.title = _pageTitle" in _function_source(src, "_restoreTitle")

    assert len(re.findall(r"\b_showWarning\(", code)) == 2, "one definition, one caller"
    tick = _code(_function_source(src, "_flashTick"))
    assert tick.index("_flashLabel()") < tick.index("_stopFlash()") < tick.index("_showWarning("), \
        "the tick asks whether the reason still holds before it writes the warning"
    assert len(re.findall(r"\bsetInterval\(", code)) == 1, "one timer, started by _reconcile"
    reconcile = _code(_function_source(src, "_reconcile"))
    assert reconcile.index("_flashLabel()") < reconcile.index("setInterval(")


def test_no_event_starts_a_flash_and_the_old_latch_is_gone():
    notify = _code(NOTIFY_JS.read_text(encoding="utf-8"))
    sock = SOCKET_JS.read_text(encoding="utf-8")
    for gone in ("approvalNeeded", "clearPending", "_pendingWhileForeground", "_startTitleFlash"):
        assert gone not in notify and gone not in _code(sock), \
            f"{gone}: a flash started by an event and stopped only by looking at the tab"

    notice = _code(_function_source(NOTIFY_JS.read_text(encoding="utf-8"), "notice"))
    assert "_reconcile" not in notice and "_attention" not in notice and "_approvals" not in notice, \
        "a ping makes a sound; it must not become a reason to flash"

    _source, wiring = _socket_handlers()
    assert wiring == WIRING, "the old event name is heard only as a ping, never as a reason to flash"
    assert len(re.findall(r"socket\.on\('(?:approval_needed|external_notice|approvals_open)'", sock)) == 3
    assert "VNNotify.syncApprovals(d.open, d.ttl_ms)" in _function_source(sock, "_onApprovalsOpen")
    ping = _code(_function_source(sock, "_onExternalNotice"))
    assert "VNNotify.notice(" in ping and "banner: !d.id" in ping
    assert "syncApprovals" not in ping and "attention" not in ping


def test_the_client_never_believes_a_snapshot_longer_than_the_server_repeats_it():
    """The lease the server grants must outlive two heartbeats, and the client
    caps whatever it is told."""
    from app import approval_watch as aw
    src = NOTIFY_JS.read_text(encoding="utf-8")
    cap = int(re.search(r"var MAX_LEASE_MS = (\d+);", src).group(1))
    default = int(re.search(r"var DEFAULT_LEASE_MS = (\d+);", src).group(1))
    assert aw.CLIENT_TTL_S * 1000 <= cap <= 60000
    assert default == aw.CLIENT_TTL_S * 1000
    assert aw.HEARTBEAT_S * 2 < aw.CLIENT_TTL_S
    assert aw.VERIFY_TTL_S > aw.PROBE_SLOW_S + aw.PROBE_TIMEOUT_S, \
        "one slow check plus one timeout must not drop a page that answers"


# ── the real page ───────────────────────────────────────────────────────────

ORIGIN = "http://localhost:59174"
_CTYPES = {".js": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml",
           ".png": "image/png", ".json": "application/json", ".woff2": "font/woff2"}

# Stand-in for the Socket.IO client: never connects, and keeps the listeners so
# the test can deliver "server pushes" to the real handlers.
_FAKE_IO = r"""
window.io = function () {
  const handlers = {};
  const s = {
    connected: false, active: false,
    io: {opts: {}, _readyState: 'closed', on() {}, off() {}},
    on(ev, fn) { (handlers[ev] = handlers[ev] || []).push(fn); return s; },
    once(ev, fn) { return s.on(ev, fn); },
    off(ev, fn) { if (handlers[ev]) handlers[ev] = fn ? handlers[ev].filter(f => f !== fn) : []; return s; },
    listeners(ev) { return (handlers[ev] || []).slice(); },
    emit() { return s; },
    connect() { return s; },
    disconnect() { return s; },
  };
  return s;
};
"""

# Headless Chromium has no tab strip to switch away from, so the tab's
# visibility is set by hand, the way the browser reports it.
_INIT = """
localStorage.setItem('activeProject', 'C--test-proj');
window.__hidden = false;
Object.defineProperty(document, 'hidden', {configurable: true, get: () => window.__hidden});
Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => window.__hidden ? 'hidden' : 'visible'});
window.__setHidden = (h) => { window.__hidden = h; document.dispatchEvent(new Event('visibilitychange')); };
window.__push = (ev, d) => { for (const fn of socket.listeners(ev)) fn(d); };
window.__titles = [];
window.__watchTitle = () => new MutationObserver(() => window.__titles.push(document.title))
    .observe(document.querySelector('title'), {childList: true, characterData: true, subtree: true});
window.__icon = () => (document.querySelector('link[rel~="icon"]') || {}).href || '';
"""


@pytest.fixture(scope="module")
def page():
    pw = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
    from jinja2 import Environment, FileSystemLoader

    index = Environment(loader=FileSystemLoader(str(TEMPLATES))).get_template("index.html").render(
        versioned_static=lambda f: f"/static/{f}", a2hs_title="VibeNode")

    def route(route, request):
        url = request.url
        if url.startswith("https://cdn.socket.io/"):
            return route.fulfill(body=_FAKE_IO, content_type="application/javascript")
        if not url.startswith(ORIGIN):
            return route.abort()
        path = url[len(ORIGIN):].split("?")[0]
        if path == "/":
            return route.fulfill(body=index, content_type="text/html")
        if path.startswith("/static/"):
            f = STATIC / path[len("/static/"):]
            if f.is_file():
                return route.fulfill(body=f.read_bytes(),
                                     content_type=_CTYPES.get(f.suffix, "application/octet-stream"))
            return route.fulfill(status=404, body="")
        if path.startswith("/api/projects") or path.startswith("/api/sessions"):
            return route.fulfill(content_type="application/json", body="[]")
        if path.startswith("/api/"):
            return route.fulfill(content_type="application/json", body="{}")
        return route.fulfill(status=404, body="")

    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # browser binary not downloaded on this machine
            pytest.skip(f"chromium unavailable: {str(exc).splitlines()[0][:80]}")
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        ctx.add_init_script(_INIT)
        pg = ctx.new_page()
        pg.route("**/*", route)
        pg.goto(ORIGIN + "/", wait_until="load")
        pg.wait_for_function("window.VNNotify && typeof socket !== 'undefined' && socket.listeners('approvals_open').length === 1")
        pg.evaluate("window.__watchTitle()")
        yield pg
        browser.close()


CARD = {"id": "c1", "title": "Release 4.2", "source": "pipeline", "since": 1,
        "message": "Click Approve or Cancel in your browser to continue."}


def _reset(page):
    page.evaluate("""() => { window.__push('approvals_open', {open: [], ttl_ms: 10000});
                             window.__setHidden(false); window.__titles.length = 0; }""")
    assert page.title() == PAGE


@pytest.mark.slow
def test_real_page_the_title_flashes_while_open_and_stops_on_the_answer(page):
    _reset(page)
    icon = page.evaluate("window.__icon()")
    page.evaluate("window.__setHidden(true)")
    page.evaluate("d => window.__push('external_notice', d)", dict(CARD, id="c1"))
    page.evaluate("d => window.__push('approvals_open', d)", {"open": [CARD], "ttl_ms": 10000})
    page.wait_for_function(f"document.title === {json.dumps(WARN)}", timeout=5000)
    page.wait_for_function(f"document.title === {json.dumps(PAGE)}", timeout=5000)
    page.wait_for_function(f"document.title === {json.dumps(WARN)}", timeout=5000)
    page.wait_for_function("window.__icon().startsWith('data:image/png')", timeout=5000)
    assert page.evaluate("VNNotify.state().flashing") is True

    page.evaluate("window.__push('approvals_open', {open: [], ttl_ms: 10000})")   # answered
    assert page.title() == PAGE, "the title goes back in the same turn as the snapshot"
    assert page.evaluate("window.__icon()") == icon
    page.evaluate("window.__titles.length = 0")
    page.wait_for_timeout(3500)
    assert page.evaluate("window.__titles") == [], "and is not touched again"
    state = page.evaluate("VNNotify.state()")
    assert (state["flashing"], state["warning"], state["dot"], state["approvals"], state["attention"]) == (
        False, None, False, [], [])


@pytest.mark.slow
def test_real_page_a_release_finished_ping_and_a_stale_approval_never_flash(page):
    _reset(page)
    page.evaluate("d => window.__push('approvals_open', d)", {"open": [dict(CARD, id="c2")], "ttl_ms": 10000})
    page.evaluate("window.__push('approvals_open', {open: [], ttl_ms: 10000})")   # answered on screen
    page.evaluate("window.__setHidden(true)")                                      # ...then hidden
    page.evaluate("""window.__push('external_notice', {title: '4.2: live',
        message: 'Released in 9m 12s', source: 'pipeline', id: ''})""")
    page.wait_for_timeout(3500)
    assert page.evaluate("window.__titles") == [] and page.title() == PAGE
    assert page.evaluate("VNNotify.state().flashing") is False


@pytest.mark.slow
def test_real_page_the_flash_dies_with_its_lease(page):
    """No answer and no more snapshots (a dead socket): it stops by itself."""
    _reset(page)
    page.evaluate("window.__setHidden(true)")
    page.evaluate("d => window.__push('approvals_open', d)", {"open": [dict(CARD, id="c9")], "ttl_ms": 2500})
    page.wait_for_function(f"document.title === {json.dumps(WARN)}", timeout=5000)
    page.wait_for_function("VNNotify.state().flashing === false", timeout=6000)
    assert page.title() == PAGE
    page.evaluate("window.__titles.length = 0")
    page.wait_for_timeout(2500)
    assert page.evaluate("window.__titles") == []
