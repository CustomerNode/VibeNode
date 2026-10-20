/**
 * Unit tests for static/js/thread-scroll-classic.js: "Classic scroll", the
 * pre-2026-10-14 thread restored on a switch beside the current design.
 *
 * Run:  node tests/test_thread_scroll_classic.js
 *
 * Loads BOTH real files, in page order (thread-scroll.js, then the classic
 * file that wraps it), over the same manually pumped DOM stubs as
 * tests/test_thread_scroll.js. Pins:
 *   - off by default: the current design runs, untouched, with a faint flip
 *   - the flip swaps the open thread to the old behaviour at once, and back
 *   - in classic mode the thread behaves as before 2026-10-14 (follows, the
 *     old "N new" pill, one tap to the bottom)
 *   - the restored code is the pre-2026-10-14 file verbatim, except [classic]
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
  toggle(c, force) {
    const on = force === undefined ? !this._s.has(c) : !!force;
    if (on) this._s.add(c); else this._s.delete(c);
    return on;
  }
}

class FakeEl {
  constructor(tag) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.nodeType = 1;   // ELEMENT_NODE: the module skips anything else
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
  // Enough for '.msg': direct children carrying the class.
  querySelectorAll(sel) {
    const cls = sel.replace(/^\./, '');
    return this.children.filter((c) => c.classList.contains(cls));
  }
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
const _ls = {};
global.localStorage = { getItem: (k) => (k in _ls ? _ls[k] : null), setItem: (k, v) => { _ls[k] = String(v); }, removeItem: (k) => { delete _ls[k]; } };
const _ss = {};
global.sessionStorage = { getItem: (k) => (k in _ss ? _ss[k] : null), setItem: (k, v) => { _ss[k] = String(v); }, removeItem: (k) => { delete _ss[k]; } };

const JS = path.join(__dirname, '..', 'static', 'js');
// eslint-disable-next-line no-eval
(0, eval)(fs.readFileSync(path.join(JS, 'thread-scroll.js'), 'utf8'));
const MODERN = global.window.ThreadScroll;
// eslint-disable-next-line no-eval
(0, eval)(fs.readFileSync(path.join(JS, 'thread-scroll-classic.js'), 'utf8'));
const TS = global.window.ThreadScroll;
assert.ok(TS && TS !== MODERN, 'the classic file must wrap ThreadScroll');
assert.strictEqual(typeof global.window.vnClassicScroll, 'function');

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
const NOEV = { preventDefault() {}, stopPropagation() {} };
const wraps = () => log.children.filter((c) => c.classList.contains('vn-jump-wrap'));
const sentinels = () => log.children.filter((c) => c.classList.contains('vn-thread-sentinel'));
const wrap = () => { const w = wraps(); assert.strictEqual(w.length, 1, 'exactly one jump button'); return w[0]; };
const flip = () => wrap().children.find((c) => c.classList.contains('vn-scroll-flip'));
const tail = () => sentinels()[0];
const tailBelowFold = () => { tail().rect = { top: 900, bottom: 901, left: 0, right: 800 }; };

const cases = [];
function test(name, fn) { cases.push([name, fn]); }
function off() { TS.setClassic(false, { quiet: true }); }

// ── Tests ─────────────────────────────────────────────────────────────────

test('off by default: the current design runs, with a faint flip on its button', () => {
  off();
  setup();
  assert.strictEqual(TS.isClassic(), false);
  assert.ok(!wrap().classList.contains('vn-classic'), "the current design's button");
  assert.ok(flip(), 'flip control mounted on it');
  assert.ok(!flip().classList.contains('on'));
  assert.strictEqual(sentinels().length, 1);
});

test('current design unchanged while off: new content below the fold does not scroll', () => {
  off();
  setup();
  log.scrollTop = 0; log.scrollHeight = 2000;
  tailBelowFold();
  msg(600, 900);
  assert.strictEqual(log.scrollTop, 0);
  assert.strictEqual(TS.hasNew(), true, "forwarded to the current design");
});

test('the flip turns classic on, persists it, swaps the open thread, and catches up', () => {
  off();
  setup();
  log.scrollHeight = 2400;
  flip().dispatch('click', NOEV);
  try {
    assert.strictEqual(TS.isClassic(), true);
    assert.strictEqual(global.localStorage.getItem('vnClassicScroll'), 'on');
    assert.ok(wrap().classList.contains('vn-classic'), 'the old pill is now the one in the thread');
    assert.strictEqual(sentinels().length, 1, 'one sentinel, not two');
    assert.ok(flip().classList.contains('on'), 'the old pill carries the flip back, shown on');
    assert.strictEqual(log.scrollTop, 2400, 'turning it on catches up');
    assert.strictEqual(TS.state(), 'LIVE');
    assert.strictEqual(global.liveAutoScroll, true, 'the handlers will follow again');
  } finally { off(); }
});

test('classic: the old state machine, the old "N new" pill, one tap to the bottom', () => {
  TS.setClassic(true, { quiet: true });
  try {
    setup();
    global.liveSessionId = 'sess-1';
    global.sessionKinds = { 'sess-1': 'working' };
    // scrolling up pauses following (READING), as before
    log.dispatch('wheel', {});
    fireIO(false);
    assert.strictEqual(TS.state(), 'READING');
    assert.strictEqual(global.liveAutoScroll, false);
    // new entries while away: the old counting pill, no status colours
    msg(600, 900);
    msg(900, 1200);
    const pill = wrap().children[0];
    assert.ok(wrap().classList.contains('visible'));
    assert.ok(/2 new/.test(pill.innerHTML), 'old label: "N new"');
    assert.ok(!/Working/.test(pill.innerHTML));
    assert.ok(!pill.classList.contains('is-working'));
    // one tap: straight to the bottom, back to LIVE, no menu
    log.scrollHeight = 3000;
    pill.dispatch('click', NOEV);
    assert.strictEqual(log.scrollTop, 3000);
    assert.strictEqual(TS.state(), 'LIVE');
    assert.ok(!wrap().classList.contains('visible'));
  } finally {
    delete global.liveSessionId; delete global.sessionKinds;
    off();
  }
});

test("classic: the current design's hooks do nothing (the old thread had none)", () => {
  TS.setClassic(true, { quiet: true });
  try {
    setup();
    log.scrollTop = 0; log.scrollHeight = 2000;
    TS.holdBottom(); TS.noteNewContent(); TS.positioned(); TS.refresh();
    assert.strictEqual(log.scrollTop, 0);
    assert.strictEqual(TS.hasNew(), false);
    assert.strictEqual(global.window.vnClassicScroll(), true, 'the restored handler lines run');
  } finally { off(); }
});

test('the flip on the old pill turns classic off and gives the current design back', () => {
  TS.setClassic(true, { quiet: true });
  setup();
  flip().dispatch('click', NOEV);
  assert.strictEqual(TS.isClassic(), false);
  assert.strictEqual(global.localStorage.getItem('vnClassicScroll'), null);
  assert.ok(!wrap().classList.contains('vn-classic'));
  assert.strictEqual(sentinels().length, 1);
  log.scrollTop = 0; log.scrollHeight = 2000;
  tailBelowFold();
  msg(600, 900);
  assert.strictEqual(log.scrollTop, 0, 'static again');
});

test('the setting holds across sessions', () => {
  TS.setClassic(true, { quiet: true });
  try {
    setup();
    const log2 = new FakeEl('div');
    TS.attach(log2, 'sess-2');
    assert.strictEqual(TS.isClassic(), true);
    assert.ok(log2.children.some((c) => c.classList.contains('vn-classic')));
  } finally { off(); }
});

test("the current design's Auto scroll setting stays its own", () => {
  off();
  setup();
  assert.strictEqual(TS.isLiveScroll(), MODERN.isLiveScroll());
  TS.setClassic(true, { quiet: true });
  try { assert.strictEqual(TS.isLiveScroll(), MODERN.isLiveScroll()); }
  finally { off(); }
});

test('the restored code is the pre-2026-10-14 file verbatim, except [classic] lines', () => {
  let old;
  try {
    old = require('child_process').execFileSync('git', ['show', '46817f2~1:static/js/thread-scroll.js'],
      { cwd: path.join(__dirname, '..'), encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] });
  } catch (e) { console.log('        (skipped: git history not available)'); return; }
  const now = fs.readFileSync(path.join(JS, 'thread-scroll-classic.js'), 'utf8');
  const body = (s, endMark) => {
    const a = s.indexOf('(function () {');
    const b = s.indexOf(endMark);
    return s.slice(a, b).split('\n');
  };
  const oldBody = body(old, '  window.ThreadScroll = {');
  const newBody = body(now, '  // ════').filter((l) => !/\[classic\] the faint switch back/.test(l))
    .map((l) => l.replace(/ vn-classic';   \/\/ \[classic\].*$/, "';").replace(/ vn-jump-classic';   \/\/ \[classic\]$/, "';"));
  while (newBody.length && newBody[newBody.length - 1].trim() === '') newBody.pop();
  while (oldBody.length && oldBody[oldBody.length - 1].trim() === '') oldBody.pop();
  assert.deepStrictEqual(newBody, oldBody);
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
