/**
 * Unit tests for static/js/thread-scroll.js — the session-thread scroll +
 * collapse state machine.
 *
 * Run:  node tests/test_thread_scroll.js
 *
 * There is no browser here, so this stubs the four DOM facilities the module
 * depends on (Element, IntersectionObserver, MutationObserver, rAF) with
 * *manually pumped* versions. That is deliberate: it makes observer delivery
 * explicit and deterministic instead of racing real async callbacks, which is
 * exactly what you want when the thing under test is a state machine.
 *
 * The behaviours pinned here are the ones the old boolean got wrong:
 *   - a PROGRAMMATIC scroll off the bottom must not look like user intent
 *   - content the user can see must never be collapsed, in any state
 *   - returning to the bottom restores scrolling but NOT collapsing
 */
'use strict';

const fs = require('fs');
const path = require('path');
const assert = require('assert');

// ── Minimal DOM stubs ─────────────────────────────────────────────────────

let _pendingRecords = [];
let _moCallback = null;
let _ioCallback = null;
let _rafQueue = [];

class FakeClassList {
  constructor() { this._s = new Set(); }
  add(...c) { c.forEach((x) => this._s.add(x)); }
  remove(...c) { c.forEach((x) => this._s.delete(x)); }
  contains(c) { return this._s.has(c); }
}

class FakeEl {
  constructor(tag) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.classList = new FakeClassList();
    this.children = [];
    this.parentNode = null;
    this.isConnected = true;
    this.rect = { top: 0, bottom: 0, left: 0, right: 0 };
    this.scrollTop = 0;
    this.scrollHeight = 1000;
    this.clientHeight = 500;
    this.style = {};
    this._attrs = {};
    this._listeners = {};
    this._html = '';
  }
  set className(v) {
    this.classList = new FakeClassList();
    String(v).split(/\s+/).filter(Boolean).forEach((c) => this.classList.add(c));
  }
  get className() { return Array.from(this.classList._s).join(' '); }
  get lastElementChild() {
    return this.children.length ? this.children[this.children.length - 1] : null;
  }
  appendChild(c) {
    if (c.parentNode) c.parentNode.removeChild(c);
    this.children.push(c);
    c.parentNode = this;
    _pendingRecords.push({ addedNodes: [c] });
    return c;
  }
  removeChild(c) {
    const i = this.children.indexOf(c);
    if (i >= 0) this.children.splice(i, 1);
    c.parentNode = null;
    return c;
  }
  setAttribute(k, v) { this._attrs[k] = v; }
  getAttribute(k) { return this._attrs[k]; }
  getBoundingClientRect() { return this.rect; }
  addEventListener(t, f) { (this._listeners[t] = this._listeners[t] || []).push(f); }
  removeEventListener(t, f) {
    const a = this._listeners[t];
    if (!a) return;
    const i = a.indexOf(f);
    if (i >= 0) a.splice(i, 1);
  }
  dispatch(t, ev) { (this._listeners[t] || []).slice().forEach((f) => f(ev || {})); }
  set innerHTML(v) { this.children.forEach((c) => { c.parentNode = null; }); this.children = []; this._html = v; }
  get innerHTML() { return this._html; }
}

/** Deliver queued mutation records the way a real MutationObserver batches. */
function flushMutations(maxRounds = 6) {
  let rounds = 0;
  while (_pendingRecords.length && rounds++ < maxRounds) {
    const batch = _pendingRecords;
    _pendingRecords = [];
    if (_moCallback) _moCallback(batch);
  }
  assert.ok(rounds < maxRounds, 'mutation handling did not converge (tail re-pin loop?)');
  _pendingRecords = [];
}

function fireIO(isIntersecting) {
  if (_ioCallback) _ioCallback([{ isIntersecting: !!isIntersecting }]);
}

function flushRaf() {
  const q = _rafQueue;
  _rafQueue = [];
  q.forEach((f) => f());
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ── Install globals, then load the module under test ─────────────────────

global.window = global;
global.document = { createElement: (t) => new FakeEl(t) };
global.matchMedia = () => ({ matches: false });
global.window.matchMedia = global.matchMedia;
global.IntersectionObserver = class {
  constructor(cb) { _ioCallback = cb; }
  observe() {} unobserve() {} disconnect() { _ioCallback = null; }
};
global.MutationObserver = class {
  constructor(cb) { _moCallback = cb; }
  observe() {} disconnect() { _moCallback = null; }
};
global.requestAnimationFrame = (f) => { _rafQueue.push(f); return _rafQueue.length; };
global.cancelAnimationFrame = () => {};
// live-panel.js normally declares this; thread-scroll.js owns its value.
global.liveAutoScroll = true;

const SRC = path.join(__dirname, '..', 'static', 'js', 'thread-scroll.js');
// eslint-disable-next-line no-eval
(0, eval)(fs.readFileSync(SRC, 'utf8'));
const TS = global.window.ThreadScroll;
assert.ok(TS, 'ThreadScroll did not install on window');

// ── Harness ───────────────────────────────────────────────────────────────

let log;
function setup() {
  _pendingRecords = [];
  _rafQueue = [];
  log = new FakeEl('div');
  log.rect = { top: 0, bottom: 500, left: 0, right: 800 };
  log.scrollTop = 0;
  log.scrollHeight = 1000;
  log.clientHeight = 500;
  TS.attach(log, 'sess-1');
  flushMutations();
  fireIO(true);            // start caught up
  return log;
}

function msg(topPx, bottomPx) {
  const el = new FakeEl('div');
  el.className = 'msg assistant';
  el.rect = { top: topPx, bottom: bottomPx, left: 0, right: 760 };
  log.appendChild(el);
  flushMutations();
  return el;
}

// ThreadScroll is a singleton bound to one log at a time, so tests MUST run
// sequentially — an async test that yields would otherwise have its state
// stomped by the next test's setup().
const cases = [];
function test(name, fn) { cases.push([name, fn]); }

// ── Tests ─────────────────────────────────────────────────────────────────

test('starts in LIVE with auto-scroll enabled', () => {
  setup();
  assert.strictEqual(TS.state(), 'LIVE');
  assert.strictEqual(TS.mayAutoScroll(), true);
  assert.strictEqual(TS.mayAutoCollapse(), true);
  assert.strictEqual(global.liveAutoScroll, true);
});

test('PROGRAMMATIC scroll off the bottom does NOT drop out of LIVE', () => {
  // This is the defect the old 150ms `_autoScrollTopAlignTs` blind spot
  // existed to paper over: a top-align moved the view off the bottom and the
  // position-based listener read it as a manual scroll-up.
  setup();
  fireIO(false);                       // sentinel left the viewport, no input event
  assert.strictEqual(TS.state(), 'LIVE', 'programmatic scroll must not imply intent');
  assert.strictEqual(global.liveAutoScroll, true);
});

test('user scroll off the bottom drops to READING and disables auto-scroll', () => {
  setup();
  log.dispatch('wheel', {});           // real input event => user is driving
  fireIO(false);
  assert.strictEqual(TS.state(), 'READING');
  assert.strictEqual(TS.mayAutoScroll(), false);
  assert.strictEqual(global.liveAutoScroll, false, 'legacy global must mirror READING');
});

test('touchmove counts as driving (mobile path)', () => {
  setup();
  log.dispatch('touchmove', {});
  fireIO(false);
  assert.strictEqual(TS.state(), 'READING');
});

test('typing in the composer is not a scroll intent', () => {
  setup();
  log.dispatch('keydown', { key: 'a', target: { tagName: 'TEXTAREA' } });
  fireIO(false);
  assert.strictEqual(TS.state(), 'LIVE');
});

test('expanding a message drops to READING', () => {
  setup();
  TS.noteUserExpand();
  assert.strictEqual(TS.state(), 'READING');
});

test('returning to the bottom restores FOLLOW, not LIVE (after settle)', async () => {
  setup();
  log.dispatch('wheel', {});
  fireIO(false);
  assert.strictEqual(TS.state(), 'READING');

  fireIO(true);                        // back at the bottom
  assert.strictEqual(TS.state(), 'READING', 'must not promote before settling');
  await sleep(450);                    // > DRIVE_DECAY_MS + SETTLE_MS
  assert.strictEqual(TS.state(), 'FOLLOW', 'weak signal restores scrolling only');
  assert.strictEqual(TS.mayAutoScroll(), true);
  assert.strictEqual(TS.mayAutoCollapse(), false, 'collapse needs a STRONG signal');
});

test('a flick that overshoots the bottom does not promote mid-flick', async () => {
  setup();
  log.dispatch('wheel', {});
  fireIO(false);
  fireIO(true);                        // momentum carries through the bottom
  await sleep(60);
  log.dispatch('touchmove', {});       // still moving
  fireIO(false);
  await sleep(450);
  assert.strictEqual(TS.state(), 'READING');
});

test('send restores full LIVE from READING and jumps to bottom', () => {
  setup();
  log.dispatch('wheel', {});
  fireIO(false);
  assert.strictEqual(TS.state(), 'READING');
  log.scrollTop = 0;
  log.scrollHeight = 4000;
  TS.onSend();
  assert.strictEqual(TS.state(), 'LIVE');
  assert.strictEqual(log.scrollTop, 4000, 'send must jump to the newest content');
});

test('catchUp (jump pill) restores full LIVE', () => {
  setup();
  log.dispatch('wheel', {});
  fireIO(false);
  TS.catchUp();
  assert.strictEqual(TS.state(), 'LIVE');
  assert.strictEqual(TS.mayAutoCollapse(), true);
});

test('collapse is DEFERRED while the element is on screen (even in LIVE)', () => {
  setup();
  const el = msg(100, 400);            // squarely inside the 0..500 viewport
  let ran = false;
  TS.requestCollapse(el, () => { ran = true; });
  assert.strictEqual(ran, false, 'must never collapse what the user can see');
  assert.strictEqual(TS.state(), 'LIVE');
});

test('deferred collapse fires once the element clears the keep-alive zone', () => {
  setup();
  const el = msg(100, 400);
  let ran = false;
  TS.requestCollapse(el, () => { ran = true; });
  assert.strictEqual(ran, false);

  el.rect = { top: -900, bottom: -600, left: 0, right: 760 };   // > 1 viewport up
  log.dispatch('scroll', {});
  flushRaf();
  assert.strictEqual(ran, true);
});

test('collapse runs immediately when a full viewport clear, in LIVE', () => {
  setup();                             // viewport is 0..500, clientHeight 500
  const el = msg(-900, -600);          // > 500px above the top edge
  let ran = false;
  TS.requestCollapse(el, () => { ran = true; });
  assert.strictEqual(ran, true);
});

test('a message JUST off screen is kept alive (scroll-back-up is lossless)', () => {
  // The deciding case for the keep-alive zone: read a message, scroll down to
  // the newer one, then scroll back up a little to re-check something. With a
  // tight margin this message would already be a truncated stub.
  setup();
  const el = msg(-200, -50);           // off screen, but < 1 viewport clear
  let ran = false;
  TS.requestCollapse(el, () => { ran = true; });
  assert.strictEqual(ran, false, 'one viewport of hysteresis must protect it');

  el.rect = { top: -1400, bottom: -1250, left: 0, right: 760 };   // now well clear
  log.dispatch('scroll', {});
  flushRaf();
  assert.strictEqual(ran, true, 'and collapse once genuinely far away');
});

test('FOLLOW never collapses, even off screen', async () => {
  setup();
  log.dispatch('wheel', {});
  fireIO(false);
  fireIO(true);
  await sleep(450);
  assert.strictEqual(TS.state(), 'FOLLOW');

  const el = msg(-900, -600);
  let ran = false;
  TS.requestCollapse(el, () => { ran = true; });
  assert.strictEqual(ran, false, 'FOLLOW must preserve expanded content');
});

test('queued collapses flush on re-entering LIVE', async () => {
  setup();
  log.dispatch('wheel', {});
  fireIO(false);
  const a = msg(-2000, -1700);
  const b = msg(-1600, -1300);
  let n = 0;
  TS.requestCollapse(a, () => { n++; });
  TS.requestCollapse(b, () => { n++; });
  assert.strictEqual(n, 0);
  TS.catchUp();
  assert.strictEqual(n, 2, 'both backlogged collapses should run');
});

test('the same element is never queued twice', () => {
  setup();
  const el = msg(100, 400);
  let n = 0;
  TS.requestCollapse(el, () => { n++; });
  TS.requestCollapse(el, () => { n++; });
  el.rect = { top: -900, bottom: -600, left: 0, right: 760 };
  log.dispatch('scroll', {});
  flushRaf();
  assert.strictEqual(n, 1);
});

test('collapsing ABOVE the viewport anchors scrollTop against the height delta', () => {
  setup();
  const el = msg(-900, -600);
  log.scrollTop = 400;
  log.scrollHeight = 1000;
  TS.requestCollapse(el, () => { log.scrollHeight = 700; });   // shrank by 300
  assert.strictEqual(log.scrollTop, 100, 'reader position must hold: 400 - 300');
});

test('collapsing BELOW the viewport does not move the reader', () => {
  setup();
  const el = msg(1200, 1500);          // a full viewport below the 0..500 box
  log.scrollTop = 400;
  log.scrollHeight = 1000;
  let ran = false;
  TS.requestCollapse(el, () => { ran = true; log.scrollHeight = 700; });
  assert.strictEqual(ran, true, 'guard the test itself: the collapse must run');
  assert.strictEqual(log.scrollTop, 400, 'shrinking below the fold moves nothing');
});

test('detach tears down observers and listeners', () => {
  setup();
  TS.detach();
  assert.strictEqual(_ioCallback, null);
  assert.strictEqual(_moCallback, null);
  assert.strictEqual((log._listeners.wheel || []).length, 0);
});

test('attach resets state — READING must not leak across sessions', () => {
  setup();
  log.dispatch('wheel', {});
  fireIO(false);
  assert.strictEqual(TS.state(), 'READING');
  setup();                             // simulate switching sessions
  assert.strictEqual(TS.state(), 'LIVE');
  assert.strictEqual(global.liveAutoScroll, true);
});

test('sentinel stays pinned as the last child after appends', () => {
  setup();
  msg(100, 400);
  msg(410, 480);
  const last = log.lastElementChild;
  assert.ok(last.classList.contains('vn-thread-sentinel'),
    'sentinel must remain last or "caught up" detection breaks');
});

// ── Report ────────────────────────────────────────────────────────────────

(async () => {
  let failed = 0;
  for (const [name, fn] of cases) {
    try {
      await fn();
      console.log('  PASS  ' + name);
    } catch (e) {
      failed++;
      console.log('  FAIL  ' + name + '\n        ' + (e && e.message));
    }
  }
  console.log('\n' + (cases.length - failed) + '/' + cases.length + ' passed');
  process.exit(failed ? 1 : 0);
})();
