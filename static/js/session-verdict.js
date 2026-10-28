/* session-verdict.js — every stopped session ends in a verdict (2026-10-27).
 *
 * The owner's design (scratch: static/scratch/status-workflow.html): a
 * session whose work is in front of you (Review / Read / Send, see
 * session-verb.js) gets two answers, on its sidebar card and at the end of
 * its thread:
 *
 *   Accept (Got it / Sent it)  it's right: the session goes to sleep, and a
 *                              sleeping session shows as Done.  Uses the
 *                              sidebar's own sleep (_dblclickSleepIfIdle), so
 *                              "Sleep must stick" (markUserStopped) and Undo
 *                              come with it.
 *   Send back                  say what's wrong: the session opens with the
 *                              composer focused and asking for it; sending
 *                              puts the session back to Working.
 *   Continue                   it's right, and there's more (added 2026-10-27,
 *                              the owner's pick): the composer opens asking
 *                              "What next?"; sending puts the session back to
 *                              Working.  Unlike Send back it does not say the
 *                              last result was wrong.  No resting state of its
 *                              own: the next card asks for a verdict again.
 *
 * So the workflow terminates: a session waits on you, is busy, or is Done.
 *
 * The strip lives after the last message in #live-log (before thread-scroll's
 * jump button and sentinel) and is present ONLY while the open
 * session is waiting for a verdict (never while it works), so it never sits
 * between a turn's entries.
 */
(function () {
  'use strict';

  const BACK_PLACEHOLDER = 'What’s wrong, and what do you want fixed?';
  const CONTINUE_PLACEHOLDER = 'What next?';
  const BAR_OFF = true;   // see sync()
  let _armed = null;        // sid whose composer is asking (Send back or Continue)
  let _armedMode = 'back';  // 'back' | 'continue'


  function _verdictVerb(sid) {
    if (!sid || !window.SessionVerb) return '';
    const d = window.SessionVerb.display(sid);
    return window.SessionVerb.ACCEPT[d] ? d : '';
  }

  // Done and Standby are the person's verdicts (owner, 2026-10-28,
  // static/scratch/standby-status.html option A).  Both are kept as a hold
  // (SessionVerb.setHold, app/session_holds.py) until the session's next
  // message, so a session that ended on a question can be Done too, and both
  // sleep it with the sidebar's own sleep (markUserStopped + Undo).
  function _verdict(sid, to, label) {
    if (!sid) return;
    const st = (typeof getSessionStatus === 'function') ? getSessionStatus(sid) : '';
    if (st === 'working') {
      if (typeof showToast === 'function') showToast('It’s working again; decide when it stops');
      return;
    }
    const SV = window.SessionVerb;
    const prev = SV && SV.holdOf ? SV.holdOf(sid) : null;
    if (SV && SV.setHold) SV.setHold(sid, to);
    const undo = () => { if (SV && SV.setHold) SV.setHold(sid, prev); };
    if (st === 'sleeping') { if (typeof _holdUndoToast === 'function') _holdUndoToast(sid, label, undo); }
    else if (typeof _sleepOrWake === 'function') _sleepOrWake(sid, null, label, undo);
    else if (typeof _dblclickSleepIfIdle === 'function') _dblclickSleepIfIdle(sid, null, label);
    sync();
  }
  function accept(sid) { _verdict(sid, 'done', 'Done'); }
  function standby(sid) { _verdict(sid, 'standby', 'Standby'); }
  /** Off Standby, back to Needs you (the status only; it does not wake it). */
  function unpark(sid) {
    if (sid && window.SessionVerb && window.SessionVerb.setHold) window.SessionVerb.setHold(sid, null);
    sync();
  }

  /** Open the session and point the composer at "what's wrong". */
  function back(sid) { _arm(sid, 'back'); }

  /** Open the session and point the composer at "what next". */
  function cont(sid) { _arm(sid, 'continue'); }

  function _arm(sid, mode) {
    if (!sid) return;
    _armed = sid;
    _armedMode = mode;
    const open = (typeof liveSessionId !== 'undefined' && liveSessionId === sid);
    // The same call a click on the card makes (sessions.js singleOrDouble).
    if (!open && typeof openInGUI === 'function') openInGUI(sid);
    let tries = 0;
    (function arm() {
      const ta = document.getElementById('live-input-ta');
      const ready = ta && typeof liveSessionId !== 'undefined' && liveSessionId === sid;
      if (!ready) { if (++tries < 40) setTimeout(arm, 75); return; }
      ta.dataset.vsPlaceholder = ta.dataset.vsPlaceholder || ta.getAttribute('placeholder') || '';
      ta.setAttribute('placeholder', mode === 'continue' ? CONTINUE_PLACEHOLDER : BACK_PLACEHOLDER);
      ta.classList.toggle('vs-sendback', mode !== 'continue');
      ta.classList.toggle('vs-continue', mode === 'continue');
      try { ta.focus({ preventScroll: true }); } catch (_e) { ta.focus(); }
      sync();
    })();
  }

  function _stripHtml(sid, d) {
    const acc = window.SessionVerb.ACCEPT[d];
    const armed = _armed === sid;
    const backOn = armed && _armedMode === 'back', contOn = armed && _armedMode === 'continue';
    return '<div class="vs-vq">' + (backOn ? 'Say what’s wrong below, then send' : contOn ? 'Say what’s next below, then send' : 'Is this right?') + '</div>' +
      '<div class="vs-vrow">' +
        '<button type="button" class="vs-big vs-accept">✓ ' + acc + ' <small>it’s right, mark it done</small></button>' +
        '<button type="button" class="vs-big vs-back' + (backOn ? ' on' : '') + '">↩ Send back <small>say what’s wrong</small></button>' +
        '<button type="button" class="vs-big vs-cont' + (contOn ? ' on' : '') + '">→ Continue <small>it’s right, and there’s more</small></button>' +
      '</div>';
  }

  /** Mount, refresh or remove the strip for the open session. */
  function sync() {
    const log = document.getElementById('live-log');
    const sid = (typeof liveSessionId !== 'undefined') ? liveSessionId : null;
    // No separate bar under the reply (owner, 2026-10-27: "NO SECOND PANEL").
    // The strip is never mounted; any left on the page is removed below.
    const d = BAR_OFF ? '' : _verdictVerb(sid);
    let strip = log ? log.querySelector(':scope > .vs-verdict-strip') : null;
    if (_armed && _armed !== sid) _armed = null;
    if (!log || !d) {
      if (strip) strip.remove();
      _disarmComposer();
      return;
    }
    const key = sid + '|' + d + '|' + (_armed === sid ? _armedMode : 0);
    if (!strip) {
      strip = document.createElement('div');
      strip.className = 'vs-verdict-strip';
    }
    if (strip.dataset.key !== key) {
      strip.dataset.key = key;
      strip.className = 'vs-verdict-strip vs-' + d;
      strip.innerHTML = _stripHtml(sid, d);
      strip.querySelector('.vs-accept').onclick = () => accept(sid);
      strip.querySelector('.vs-back').onclick = () => back(sid);
      strip.querySelector('.vs-cont').onclick = () => cont(sid);
    }
    // Right after the last message, before the thread's own trailing chrome
    // (thread-scroll.js keeps its jump button and sentinel last; moving past
    // them would make the two observers trade places forever).
    const tail = log.querySelector(':scope > .vn-jump-wrap, :scope > .vn-thread-sentinel');
    if (tail) { if (strip.nextElementSibling !== tail) log.insertBefore(strip, tail); }
    else if (log.lastElementChild !== strip) log.appendChild(strip);
  }

  /** The verdict is over (it works again, or was accepted): put the composer back. */
  function _disarmComposer() {
    const ta = document.getElementById('live-input-ta');
    if (ta && (ta.classList.contains('vs-sendback') || ta.classList.contains('vs-continue'))) {
      ta.classList.remove('vs-sendback');
      ta.classList.remove('vs-continue');
      ta.setAttribute('placeholder', ta.dataset.vsPlaceholder || '');
      delete ta.dataset.vsPlaceholder;
    }
    if (_armed && _verdictVerb(_armed) === '') _armed = null;
  }

  // Keep the strip last and present across re-renders of the thread.
  let _queued = false;
  function _soon() {
    if (_queued) return;
    _queued = true;
    requestAnimationFrame(() => { _queued = false; sync(); });
  }
  // #live-log is rebuilt every time a session opens (live-panel.js writes
  // #main-body's innerHTML), so watch #main-body for that and re-attach.
  let _logObs = null, _logEl = null;
  function _attachLog() {
    const log = document.getElementById('live-log');
    if (log !== _logEl) {
      if (_logObs) _logObs.disconnect();
      _logEl = log;
      _logObs = log ? new MutationObserver(muts => {
        for (const m of muts) {
          for (const n of m.addedNodes) if (!(n.classList && n.classList.contains('vs-verdict-strip'))) { _soon(); return; }
          for (const n of m.removedNodes) if (n.classList && n.classList.contains('vs-verdict-strip')) { _soon(); return; }
        }
      }) : null;
      if (_logObs) _logObs.observe(log, { childList: true });
    }
    _soon();
  }
  function _observe() {
    const host = document.getElementById('main-body');
    if (!host) { setTimeout(_observe, 500); return; }
    new MutationObserver(_attachLog).observe(host, { childList: true });
    _attachLog();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _observe);
  else _observe();

  window.SessionVerdict = { accept, standby, unpark, back, cont, sync };
})();
