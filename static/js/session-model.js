// ═══════════════════════════════════════════════════════════════════════════
// session-model.js — THE single owner of "what model does this session use?"
// ═══════════════════════════════════════════════════════════════════════════
//
// WHY THIS FILE EXISTS
// --------------------
// The per-session model selector was historically brittle because "the model
// for a session" was stored in SIX overlapping places (a global override in
// localStorage, a `window._sessionModelOverride` global, a `.model` field on
// the session object that conflated *desired* with *confirmed*, the badge DOM
// text, the sidebar element, and the daemon), and FOUR different functions each
// re-derived the effective value with a slightly different priority chain. When
// those chains disagreed, the badge showed one model while the session started
// on another, or a model picked for one new session silently leaked into the
// next one.
//
// THE NEW CONTRACT — one owner, one write path, derived rendering:
//
//   1. SYSTEM DEFAULT (the top-nav selector) is the ONE global preference.
//      Stored under a single localStorage key. Read via getDefault().
//
//   2. A PENDING (not-yet-started) session's chosen model is its own
//      `desiredModel` field on the session object in `allSessions`. It is NOT
//      global and does NOT persist across sessions — a brand-new session with
//      nothing chosen resolves to the system default. This kills the
//      cross-session leak at the root.
//
//   3. A RUNNING session's model is `.model` on the session object — a MIRROR
//      of the daemon's ground truth. It is written ONLY by ingestConfirmed()
//      (fed from server events). Nothing else may write it, and NOBODY writes
//      the model to the DOM directly — renderers derive from this store.
//
//   4. effective()/effectivePending() are the ONE resolver every renderer and
//      every start path calls. There is no second priority chain anywhere.
//
// `desiredModel` (pending intent) and `.model` (running truth) are deliberately
// SEPARATE fields even though they live on the same object, so the two
// lifecycle phases never overwrite each other.
//
// This module has no DOM dependencies and no side effects on load beyond a
// one-time cleanup of the obsolete global-override localStorage keys.
// ═══════════════════════════════════════════════════════════════════════════

window.SessionModel = (function () {
  'use strict';

  var DEFAULT_MODEL_KEY = 'defaultModel';
  var DEFAULT_THINKING_KEY = 'defaultThinking';
  // Last-resort model id if nothing is configured. Kept in sync with the
  // hardcoded fallbacks in app.js/openModelSelector and /api/models.
  var FALLBACK_MODEL = 'claude-opus-4-7';

  // The CLI reports "[1m]" (1M context active) as a display suffix on model
  // ids (e.g. "claude-opus-5-5[1m]"). That suffix is NOT part of a valid SDK
  // model id — sending one as --model / set_model fails with an API 400 —
  // but bracketed ids can reach us through the /api/models confirmed cache
  // or an old localStorage value. Strip markers from every id this store
  // HANDS OUT for starting/switching, so no start path can ever send one.
  // Confirmed ids (daemon ground truth, `.model`) are deliberately NOT
  // stripped — the badge honestly displays "[1m]".
  function _cleanId(id) {
    return (id || '').replace(/\[[^\]]*\]/g, '');
  }

  // One-time migration: the pre-rebuild architecture armed a GLOBAL one-shot
  // override in localStorage that leaked across sessions. Remove it so a stale
  // value saved before this upgrade can never attach to a new session.
  try {
    localStorage.removeItem('_sessionModelOverride');
    localStorage.removeItem('_sessionThinkingOverride');
  } catch (e) { /* localStorage unavailable — nothing to clean */ }

  /** Look up a session object in the global registry, or null. */
  function _sess(id) {
    if (!id || typeof allSessions === 'undefined' || !Array.isArray(allSessions)) {
      return null;
    }
    for (var i = 0; i < allSessions.length; i++) {
      if (allSessions[i] && allSessions[i].id === id) return allSessions[i];
    }
    return null;
  }

  // ── System default (the top-nav selector) ──────────────────────────────

  /** The system-default model id used by any session that hasn't chosen one. */
  function getDefault() {
    try {
      return _cleanId(localStorage.getItem(DEFAULT_MODEL_KEY)) || FALLBACK_MODEL;
    } catch (e) {
      return _cleanId(typeof defaultModel !== 'undefined' && defaultModel) || FALLBACK_MODEL;
    }
  }

  /** The system-default thinking level ('' means "model default"). */
  function getDefaultThinking() {
    try {
      return localStorage.getItem(DEFAULT_THINKING_KEY) || '';
    } catch (e) {
      return (typeof defaultThinking !== 'undefined' && defaultThinking) || '';
    }
  }

  // ── Pending-session desired model (owner: the session object) ───────────

  /** The model explicitly chosen for this pending session, or '' if none. */
  function getDesired(id) {
    var s = _sess(id);
    return _cleanId((s && s.desiredModel) || '');
  }

  /** The thinking level chosen for this pending session, else system default. */
  function getDesiredThinking(id) {
    var s = _sess(id);
    if (s && typeof s.desiredThinking === 'string') return s.desiredThinking;
    return getDefaultThinking();
  }

  /**
   * Record a pending session's chosen model/thinking. Stored ON the session
   * object so it can never bleed into a different session. Returns true if the
   * session was found and updated.
   */
  function setDesired(id, model, thinking) {
    var s = _sess(id);
    if (!s) return false;
    model = _cleanId(model);
    if (model) s.desiredModel = model; else delete s.desiredModel;
    if (thinking) s.desiredThinking = thinking; else delete s.desiredThinking;
    return true;
  }

  /** Forget a pending session's chosen model/thinking (revert to defaults). */
  function clearDesired(id) {
    var s = _sess(id);
    if (s) { delete s.desiredModel; delete s.desiredThinking; }
  }

  // ── Running-session confirmed model (owner: the daemon, mirrored here) ──

  /** The confirmed model of a RUNNING session (daemon truth), or '' if unknown. */
  function getConfirmed(id) {
    var s = _sess(id);
    return (s && s.model) || '';
  }

  /**
   * THE single write path for a server-reported running model. Callers hand us
   * whatever the daemon just reported (state event, model-switch result, or the
   * CLI init message); we update the mirror and return true IF it changed, so
   * the caller can trigger exactly one re-render. Nobody else writes `.model`.
   */
  function ingestConfirmed(id, model) {
    if (!model) return false;
    var s = _sess(id);
    if (!s || s.model === model) return false;
    s.model = model;
    return true;
  }

  // ── The one resolver everything uses ────────────────────────────────────

  /**
   * The effective model id to DISPLAY or START for a session:
   *   running/confirmed model wins → else the pending desired → else default.
   * Pass {pendingOnly:true} to ignore any confirmed value (used for brand-new
   * session bars that must reflect the choice, never a stale confirmed id).
   */
  function effective(id, opts) {
    opts = opts || {};
    if (!opts.pendingOnly) {
      var confirmed = getConfirmed(id);
      if (confirmed) return confirmed;
    }
    return getDesired(id) || getDefault();
  }

  /** The effective model for a session that has NOT started yet. */
  function effectivePending(id) {
    return getDesired(id) || getDefault();
  }

  /**
   * The model id to PIN when waking a sleeping session ('' = send nothing and
   * let the daemon resume on the model it has recorded).
   *
   * Daemon-confirmed truth wins over this client's local pending choice. The
   * confirmed mirror is kept fresh from EVERY device (session_model_changed
   * broadcasts, reconnect snapshots, state events); `desiredModel` is only
   * ever written on the tab that made a choice and never persists. So when
   * the two disagree — picked A here, then switched to B from the phone —
   * `desiredModel` is the stale one, and sending it would wake the session on
   * A and silently undo the switch. That was the "doesn't stick" bug.
   *
   * Marker-stripped so it is always a valid --model id.
   */
  function resumeModel(id) {
    return _cleanId(getConfirmed(id)) || getDesired(id);
  }

  return {
    getDefault: getDefault,
    getDefaultThinking: getDefaultThinking,
    getDesired: getDesired,
    getDesiredThinking: getDesiredThinking,
    setDesired: setDesired,
    clearDesired: clearDesired,
    getConfirmed: getConfirmed,
    ingestConfirmed: ingestConfirmed,
    effective: effective,
    effectivePending: effectivePending,
    resumeModel: resumeModel,
  };
})();
