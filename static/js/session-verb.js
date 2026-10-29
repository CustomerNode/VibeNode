/* session-verb.js — sidebar status oriented to the PERSON, not the process.
 *
 * Decided 2026-10-27 (scratch: static/scratch/status-icons.html, set A).  The
 * old statuses (question / working / idle / sleeping) described the CLI.  Idle
 * covered "needs your decision", "done", "waiting on its own tests" and
 * "nothing pending" alike, and "Question" fired only for a tool permission.
 * Now each session shows what YOU have to do:
 *
 *   You decide, it can't go on  decide · unblock · approve
 *   You check, when you can     review · read · send   (Accept or Send back)
 *   It is busy                  working · waiting
 *   Finished                    done (your verdict: a 'done' hold)
 *
 * Inputs, strongest first:
 *   - live state: a running turn is Working, an open permission is Approve,
 *     a scheduled wake-up or an armed retry / usage-limit countdown is Waiting;
 *   - the last message's handoff card (the ```handoff JSON block Smart
 *     termination asks for): read live from `session_entry`, or on load from
 *     the summary's `next_step` (common/handoff_status.py; the mapping below
 *     mirrors verb_from_card there, tests/test_session_verb.py keeps them in
 *     step);
 *   - nothing else: there is no "Idle".  A stopped, awake session is Review
 *     (or Read / Send per its card) until you Accept it (it sleeps: Done) or
 *     send it back.  Decide and Unblock stay until you answer, even asleep.
 *
 * `getSessionStatus()` is unchanged: behaviour (double-click sleep, queue,
 * kanban) still reads the process state.  This file only feeds what is DRAWN:
 * the list icon, the card avatar/label/colour, the "Active first" rank, the
 * status filters and the homepage lists.
 */
(function () {
  'use strict';

  const ORDER = ['decide', 'unblock', 'approve', 'review', 'read', 'send', 'working', 'waiting', 'standby', 'done'];
  const RANK = { decide: 0, unblock: 0, approve: 0, review: 1, read: 1, send: 1, working: 2, waiting: 3, idle: 4, standby: 4.5, done: 5 };
  const LABEL = { decide: 'Decide', unblock: 'Unblock', approve: 'Approve', review: 'Review', read: 'Read', send: 'Send',
    working: 'Working', waiting: 'Waiting', idle: 'New', standby: 'Standby', done: 'Done' };
  // Review / Read / Send end with a verdict: accept (the session sleeps: Done)
  // or send it back (say what's wrong: Working).  session-verdict.js.
  const ACCEPT = { review: 'Accept', read: 'Got it', send: 'Sent it' };
  const MEANS = {
    decide: 'Pick between the options it laid out',
    unblock: 'Blocked on something only you can do',
    approve: 'A permission is waiting for your OK',
    review: 'It says the work is done. Check it',
    read: 'It wrote something for you to read',
    send: 'A draft for you to send',
    working: 'Working',
    waiting: 'It continues by itself',
    idle: 'Nothing sent yet',
    standby: 'Seen, not now. Chat with it to pick it up',
    done: 'Done',
  };
  // Outline glyphs, 24×24 (set A).
  const GLYPH = {
    decide: '<path d="M12 21v-8"/><path d="M12 13 6 7"/><path d="M12 13l6-6"/><path d="M6 12V7h5"/><path d="M13 7h5v5"/>',
    unblock: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    approve: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/>',
    review: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    read: '<path d="M2 4h6a4 4 0 0 1 4 4v13a3 3 0 0 0-3-3H2z"/><path d="M22 4h-6a4 4 0 0 0-4 4v13a3 3 0 0 1 3-3h7z"/>',
    send: '<path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4z"/>',
    waiting: '<path d="M5 22h14"/><path d="M5 2h14"/><path d="M17 22v-4.17a2 2 0 0 0-.59-1.41L12 12l-4.41 4.41A2 2 0 0 0 7 17.83V22"/><path d="M7 2v4.17a2 2 0 0 0 .59 1.41L12 12l4.41-4.41A2 2 0 0 0 17 6.17V2"/>',
    idle: '<circle cx="12" cy="12" r="9.5"/>',
    done: '<circle cx="12" cy="12" r="9.5"/><polyline points="16.5 9 10.5 15 7.5 12"/>',
    standby: '<circle cx="12" cy="12" r="9.5"/><path d="M10 9v6"/><path d="M14 9v6"/>',
  };
  // Three DRAWN statuses (owner's pick B, 2026-10-28, scratch:
  // static/scratch/status-merge.html): the sidebar says whose move it is, not
  // what kind of work it is.  display() keeps the fine verb (the hover title
  // says it, and behaviour reads it); everything drawn goes through shown().
  //   Needs you  decide · unblock · approve · review · read · send · new
  //   Working    working · waiting
  //   Done       done (the person's verdict, not sleep)
  const GROUP = { decide: 'needs', unblock: 'needs', approve: 'needs', review: 'needs', read: 'needs', send: 'needs', idle: 'needs',
    working: 'working', waiting: 'working', standby: 'standby', done: 'done' };
  // Standby (owner, 2026-10-28, static/scratch/standby-status.html option A):
  // seen, not now, still on the list.  Set by the person (double-click on a
  // Needs-you session, or its Standby button); any new message ends it.
  const GROUP_LABEL = { needs: 'Needs you', working: 'Working', standby: 'Standby', done: 'Done' };
  GLYPH.needs = '<circle cx="12" cy="12" r="9.5"/><path d="M12 7.5v5.5"/><path d="M12 16.5h.01"/>';
  function shown(d) { return GROUP[d] || 'done'; }
  const PICKAXE_FILTER = 'filter:brightness(0) saturate(100%) invert(55%) sepia(78%) saturate(1000%) hue-rotate(215deg);';

  // ── The card → verb (mirror of common/handoff_status.py) ──
  const READ_KINDS = /report|doc|summary|notes|analysis|audit|findings|plan|research/i;
  // Both fences start a line, as in the checker: a ``` inside the card's text
  // must not end it (2026-10-28).
  const BLOCK = /^[ \t]*(`{3,}|~{3,})[ \t]*handoff[^\n]*\n([\s\S]*?)^[ \t]*\1[`~]*[ \t]*$/gm;

  function lastCard(text) {
    if (!text || text.indexOf('handoff') === -1) return null;
    let m, last = null;
    BLOCK.lastIndex = 0;
    while ((m = BLOCK.exec(text)) !== null) last = m[2];
    if (last == null) return null;
    try { const c = JSON.parse(last); return (c && typeof c === 'object' && !Array.isArray(c)) ? c : null; }
    catch (_e) { return null; }
  }

  function verbFromCard(card) {
    if (!card || typeof card !== 'object') return null;
    const t = String(card.type || '').toLowerCase();
    const needs = (card.needs && typeof card.needs === 'object') ? card.needs : {};
    const nk = String(needs.kind || '').toLowerCase();
    if (nk === 'decision' || nk === 'approve') return 'decide';
    if (nk === 'action' || t === 'blocked') return 'unblock';
    if (t === 'needs_you') return 'decide';
    if (t === 'working') return 'waiting';
    if (t === 'deliverable') {
      const d = (card.deliverable && typeof card.deliverable === 'object') ? card.deliverable : {};
      // Files to download are taken, not sent: Got it (2026-10-28).
      if (Array.isArray(d.files) && d.files.length) return 'read';
      return READ_KINDS.test(String(d.kind || '')) ? 'read' : 'send';
    }
    if (t === 'explain') return 'read';
    if (t === 'done') return 'review';
    return null;
  }

  function cardLine(card) {
    const needs = (card && card.needs && typeof card.needs === 'object') ? card.needs : {};
    const ask = String(needs.kind || 'none') !== 'none' ? needs.ask : '';
    return String(ask || (card && card.headline) || '').slice(0, 200);
  }

  function nextStep(text) {
    const card = lastCard(text);
    const verb = card && verbFromCard(card);
    // Status comes from the card's type only; never guessed from free text.
    return verb ? { verb: verb, line: cardLine(card) } : null;
  }

  // ── What this page has seen live (overrides the summary from load) ──
  // sid -> {verb, line} | {verb: null}.  An explicit null means "the newest
  // message has no card", so a stale `next_step` from the list load can't win.
  const _live = new Map();
  // Sessions this page has seen an assistant reply from.
  const _replied = new Set();

  // ── Standby / marked Done: the person's choice (app/session_holds.py) ──
  // sid -> 'standby' | 'done' | null.  Seeded from the list's `hold`, set by
  // setHold() and the `session_hold` broadcast; any new message ends it
  // (noteEntry), as the server does by comparing the newest message.
  const _hold = new Map();
  // sid -> when the person made that choice (epoch seconds), for the text
  // box's "Set aside 12 min ago" (live-panel.js, standby-bar.html option A).
  const _holdSince = new Map();
  function holdSince(sid) {
    if (_holdSince.has(sid)) return _holdSince.get(sid) || 0;
    const row = (typeof SessionStore !== 'undefined' && SessionStore.get) ? SessionStore.get(sid) : null;
    return (row && row.hold && +row.hold_since > 0) ? +row.hold_since : 0;
  }
  /** The open session's text box shows Standby, so redraw it on a change. */
  function _refreshComposer(sid) {
    if (typeof liveSessionId !== 'undefined' && liveSessionId === sid && typeof updateLiveInputBar === 'function') {
      try { updateLiveInputBar(); } catch (_e) { /* drawing only */ }
    }
  }
  function holdOf(sid) {
    if (_hold.has(sid)) return _hold.get(sid);
    const row = (typeof SessionStore !== 'undefined' && SessionStore.get) ? SessionStore.get(sid) : null;
    return (row && (row.hold === 'standby' || row.hold === 'done')) ? row.hold : null;
  }
  /** Apply a hold seen from elsewhere (broadcast), without saving. */
  function applyHold(sid, state, since) {
    if (!sid) return;
    const before = display(sid);
    _hold.set(sid, state === 'standby' || state === 'done' ? state : null);
    _holdSince.set(sid, _hold.get(sid) ? (+since > 0 ? +since : Date.now() / 1000) : 0);
    const row = (typeof SessionStore !== 'undefined' && SessionStore.get) ? SessionStore.get(sid) : null;
    if (row) {
      if (_hold.get(sid)) { row.hold = _hold.get(sid); row.hold_since = _holdSince.get(sid); }
      else { delete row.hold; delete row.hold_since; }
    }
    if (display(sid) !== before) repaint(sid, before);
    _refreshComposer(sid);
    // The open session's card shows the answers that fit its new status.
    if (window.VNLastCard && window.VNLastCard.syncDone) { try { window.VNLastCard.syncDone(); } catch (_e) { /* drawing only */ } }
  }
  /** Put a session on Standby ('standby'), mark it Done ('done') or clear
   *  that (null).  Saved on the server so every device and a reload agree. */
  // The screen never shows a choice the server did not keep (2026-10-28, the
  // owner: "i had put a session in standby, refreshed the page, and it showed
  // as done").  The save used to be fire-and-forget: when it failed (a web
  // server not yet restarted onto the route, a dropped connection, a phone
  // between networks) the sidebar said Standby while nothing was stored, and
  // the next reload drew the sleeping session as Done.  Now a failed save is
  // retried once, then the screen goes back to what is saved and says so.
  const _holdSeq = new Map();   // sid -> number of the newest setHold
  const _HOLD_RETRY_MS = 1500;
  function _postHold(sid, state, project) {
    return fetch('/api/session-hold/' + encodeURIComponent(sid), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ state: state || null, project: project }),
    }).then(r => r.ok).catch(() => false);
  }
  function setHold(sid, state) {
    if (!sid) return Promise.resolve(false);
    const prev = holdOf(sid), prevSince = holdSince(sid);
    applyHold(sid, state, Date.now() / 1000);
    const project = (typeof localStorage !== 'undefined' && localStorage.getItem('activeProject')) || '';
    if (typeof fetch !== 'function') return Promise.resolve(true);
    const seq = (_holdSeq.get(sid) || 0) + 1;
    _holdSeq.set(sid, seq);
    return _postHold(sid, state, project)
      .then(ok => ok ? true : new Promise(res => setTimeout(res, _HOLD_RETRY_MS)).then(() => _postHold(sid, state, project)))
      .then(ok => {
        // A newer choice for this session owns the screen now; leave it be.
        if (ok || _holdSeq.get(sid) !== seq) return ok;
        applyHold(sid, prev, prevSince);
        const word = state === 'standby' ? 'Standby' : state === 'done' ? 'Done' : 'that change';
        if (typeof showToast === 'function') showToast('Couldn’t save ' + word + '. Try again.', true);
        return false;
      });
  }

  /** True only for a row known to be empty (message_count 0) that has not
   *  replied since the page loaded.  No row: unknown, so not "never". */
  function _neverReplied(sid) {
    if (_replied.has(sid)) return false;
    const row = (typeof SessionStore !== 'undefined' && SessionStore.get) ? SessionStore.get(sid) : null;
    return !!row && row.message_count === 0 && !row.next_step;
  }

  function _cardStep(sid) {
    if (_live.has(sid)) return _live.get(sid);
    const row = (typeof SessionStore !== 'undefined' && SessionStore.get) ? SessionStore.get(sid) : null;
    return (row && row.next_step && row.next_step.verb) ? row.next_step : null;
  }

  /**
   * The status to DRAW for a session.
   * @param {string} sid
   * @param {string} [status] getSessionStatus() value, when the caller has an
   *   authoritative one (the surgical update path does).
   */
  function display(sid, status) {
    const st = status || ((typeof getSessionStatus === 'function') ? getSessionStatus(sid) : 'sleeping');
    if (st === 'question') return 'approve';
    if (st === 'working') return 'working';
    const sub = window._sessionSubstatus && window._sessionSubstatus[sid];
    if (sub === 'auto-resuming') return 'waiting';                 // scheduled wake-up
    if (st === 'idle' && window._sessionRetryState && window._sessionRetryState[sid]) return 'waiting';  // retry / usage-limit countdown
    // The person's own choice beats the card until the next message.
    const hold = holdOf(sid);
    if (hold === 'done') return 'done';
    if (hold === 'standby') return 'standby';
    const step = _cardStep(sid);
    const v = step && step.verb;
    if (v === 'decide' || v === 'unblock') return v;                // still yours, awake or not
    // The workflow terminates (2026-10-27): a stopped session, awake or
    // asleep, is never "Idle": it waits for your verdict, as Review unless
    // its card says Read or Send.  Opening it does not clear that; Accept
    // or Send back does.
    // A session that has not replied yet (a new one, nothing sent) has
    // nothing to judge and nothing done: no "Is this right?", no Accept on
    // its card, and not Done (its card is drawn before it is marked open).
    if (!v && _neverReplied(sid)) return 'idle';
    // Done is the person's verdict, never an inference from sleep (owner,
    // 2026-10-28: "we are using sleep to determine status but is sleep
    // deterministic?").  Sleep has many causes he never sees (watchdogs,
    // limits, restarts), so a session slept without a verdict stays Needs
    // you.  A verdict is a 'done' hold (above): Accept / Got it / Sent it or
    // the double-click.  The backlog was marked once on the server
    // (app/session_holds.py backfill_reviewed).
    if (v === 'waiting') return 'waiting';
    if (v === 'read' || v === 'send') return v;
    return 'review';
  }

  function rank(sid, status) { const r = RANK[display(sid, status)]; return r == null ? 5 : r; }

  /** Bucket for the funnel's status filters (Working / Needs you / Done),
   *  in getSessionStatus() vocabulary so the filter code keeps its shape. */
  function filterBucket(sid, status) {
    const g = shown(display(sid, status));
    if (g === 'needs') return 'question';
    if (g === 'working') return 'working';
    if (g === 'standby') return 'standby';
    return 'sleeping';   // done
  }

  function _svg(d, size, sw, cls, title) {
    return '<svg class="' + cls + ' vs-' + d + '" width="' + size + '" height="' + size + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="' + sw +
      '" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + (title ? '<title>' + _esc(title) + '</title>' : '') + GLYPH[d] + '</svg>';
  }

  function _title(sid, d) {
    const step = _cardStep(sid);
    const line = (step && step.line && RANK[d] <= 1) ? step.line : '';
    return GROUP_LABEL[shown(d)] + ': ' + (line || MEANS[d]);
  }

  /** The list row's leading icon (replaces the old 4-state icon). */
  function listIconHtml(sid, status) {
    const d = display(sid, status), g = shown(d);
    if (g === 'working') return '<img class="state-icon" src="/static/svg/pickaxe.svg" width="12" height="12" style="' + PICKAXE_FILTER + '" title="' + _esc(_title(sid, d)) + '">';
    return _svg(g, 12, 2.2, 'state-icon vs-ic', _title(sid, d));
  }

  /** The card's avatar, label and colour class. */
  function cardParts(sid, status) {
    const d = display(sid, status), g = shown(d);
    let emoji;
    if (g === 'working') emoji = '<img src="/static/svg/pickaxe.svg" width="28" height="28" style="' + PICKAXE_FILTER + '">';
    else emoji = _svg(g, 28, 1.5, 'vs-ic');
    // No answer buttons on the sidebar card (owner, 2026-10-27: "CTAs make no
    // sense there").  Accept / Send back / Continue live only at the end of
    // the open session's thread (session-verdict.js); the card keeps its date.
    const foot = '';
    return { emoji: emoji, label: GROUP_LABEL[g], cls: 'vs-' + g, status: g, title: _title(sid, d), foot: foot, accept: '' };
  }

  function _esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  // ── Repaint one session when its verb changes ──
  function repaint(sid, before) {
    if (typeof _updateRowState === 'function') _updateRowState(sid);
    const statusSort = (typeof sessionDisplayMode !== 'undefined' && sessionDisplayMode === 'grid')
      ? (typeof wfSort !== 'undefined' && wfSort === 'status')
      : (typeof sortMode !== 'undefined' && sortMode === 'status');
    const ranked = before != null && RANK[before] !== RANK[display(sid)];
    const filtered = typeof _sessionFiltersDependOnState === 'function' && _sessionFiltersDependOnState();
    if ((statusSort && ranked) || (filtered && before !== display(sid))) {
      if (typeof _scheduleFilterSessions === 'function') _scheduleFilterSessions();
      else if (typeof filterSessions === 'function') filterSessions();
    }
    if (typeof viewMode !== 'undefined' && viewMode === 'homepage' && typeof _updateHomepageStats === 'function') _updateHomepageStats();
  }

  /** Called for every `session_entry` (socket.js).  The newest assistant text
   *  decides the card; a user message starts a new turn and clears it. */
  function noteEntry(sid, entry) {
    if (!sid || !entry) return;
    let step;
    if (entry.kind === 'asst' && entry.text) {
      step = nextStep(entry.text) || { verb: null };
      if (!_replied.has(sid)) {
        const before0 = display(sid);
        _replied.add(sid);
        if (display(sid) !== before0) repaint(sid, before0);
      }
    }
    else if (entry.kind === 'user') step = { verb: null };
    else return;
    // Any new message ends Standby or a marked Done (the server agrees: the
    // hold is pinned to the message that was newest when it was set).
    if (holdOf(sid)) { const b = display(sid); _hold.set(sid, null); _holdSince.set(sid, 0); const row = (typeof SessionStore !== 'undefined' && SessionStore.get) ? SessionStore.get(sid) : null; if (row) { delete row.hold; delete row.hold_since; } if (display(sid) !== b) repaint(sid, b); _refreshComposer(sid); }
    const prev = _cardStep(sid);
    if (prev && step.verb === prev.verb && step.line === prev.line) { _live.set(sid, step); return; }
    if (!prev && !step.verb) { _live.set(sid, step); return; }
    const before = display(sid);
    _live.set(sid, step);
    if (display(sid) !== before) repaint(sid, before);
  }

  const core = {
    ORDER, LABEL, ACCEPT, GROUP_LABEL, shown, display, rank, filterBucket, listIconHtml, cardParts, noteEntry, repaint,
    holdOf, holdSince, setHold, applyHold,
    // exposed for tests
    nextStep, verbFromCard, lastCard,
  };

  // ── Follows the Smart termination switch, live (2026-10-28) ──
  // The owner: flipping Smart termination must change the statuses on the
  // left with no refresh.  These statuses come from the handoff cards Smart
  // termination asks for, so with it off the sidebar draws the process
  // statuses (Question / Working / Idle / Sleeping) every caller already
  // falls back to when `window.SessionVerb` is absent.  `SessionVerbCore` is
  // always there, so cards seen while it is off still count once it is on.
  const LS_KEY = 'vnSmartTermination';
  function _on() {
    if (window._smartTerminationEnabled === true) return true;
    if (window._smartTerminationEnabled === false) return false;
    try { return localStorage.getItem(LS_KEY) === 'on'; } catch (_e) { return false; }  // before config loads
  }
  window.SessionVerbCore = core;
  Object.defineProperty(window, 'SessionVerb', {
    configurable: true,
    get() { return _on() ? core : undefined; },
  });

  function _repaintAll() {
    if (typeof filterSessions === 'function') filterSessions();
    if (typeof document === 'undefined') return;   // test harness
    // The homepage card is redrawn even while hidden, so going back to it
    // never shows the old set.
    if (typeof _updateHomepageStats === 'function') _updateHomepageStats();
    // The empty Sessions view's counts (no session open).
    const dash = document.querySelector('#main-body > .dashboard');
    if (dash && typeof _buildDashboard === 'function') document.getElementById('main-body').innerHTML = _buildDashboard();
    if (window.SessionVerdict && window.SessionVerdict.sync) window.SessionVerdict.sync();
    if (window.VNLastCard && window.VNLastCard.syncDone) window.VNLastCard.syncDone();
  }

  /** Set the Smart termination switch for this page and redraw every status
   *  on screen when it changed.  Called by the Preferences switch, the config
   *  load, another tab (storage event) and a re-read on focus. */
  window.applySmartTerminationView = function (on) {
    on = on === true;
    const was = _on();
    window._smartTerminationEnabled = on;
    try { localStorage.setItem(LS_KEY, on ? 'on' : 'off'); } catch (_e) { /* private mode */ }
    if (was !== on) _repaintAll();
  };

  if (typeof document === 'undefined' || typeof window.addEventListener !== 'function') return;  // test harness
  // Another tab on this device flipped it.
  window.addEventListener('storage', e => {
    if (e.key === LS_KEY && e.newValue) window.applySmartTerminationView(e.newValue === 'on');
  });
  // Another device flipped it: re-read the saved value when this tab comes
  // back into view (at most every 5 s).
  let _lastRead = 0;
  function _reread() {
    if (document.visibilityState === 'hidden' || Date.now() - _lastRead < 5000) return;
    _lastRead = Date.now();
    fetch('/api/kanban/config').then(r => r.ok ? r.json() : null).then(cfg => {
      if (cfg && typeof cfg.smart_termination === 'boolean') window.applySmartTerminationView(cfg.smart_termination);
    }).catch(() => {});
  }
  document.addEventListener('visibilitychange', _reread);
  window.addEventListener('focus', _reread);
})();
