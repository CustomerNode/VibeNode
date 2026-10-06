/* invoke-workforce.js — Invoke Workforce modal, selection state, message wrapping */

// ═══════════════════════════════════════════════════════════════════════
// STATE
// ═══════════════════════════════════════════════════════════════════════

/** @type {{ id:string, name:string, systemPrompt:string, path:string, source:string }|null} */
window._pendingInvoke = null;

let _invokeModalOpen = false;
let _localWfCache = null;   // { ts, skills, agents }
const _LOCAL_WF_TTL = 15000; // 15s cache
let _invokeRegistry = [];   // temp registry for modal items (avoids inline JSON in onclick)

// ═══════════════════════════════════════════════════════════════════════
// NAME PRETTIFICATION
// ═══════════════════════════════════════════════════════════════════════

function _prettifyName(stem) {
  if (!stem) return '';
  // Split camelCase
  let n = stem.replace(/([a-z])([A-Z])/g, '$1 $2');
  n = n.replace(/[-_]+/g, ' ');
  return n.replace(/\b\w/g, c => c.toUpperCase());
}

// ═══════════════════════════════════════════════════════════════════════
// SLASH COMMAND INTERCEPTION
// ═══════════════════════════════════════════════════════════════════════

function _interceptSlashCommand(text) {
  if (!text || text[0] !== '/') return false;
  const parts = text.split(/\s+/);
  const cmd = parts[0].toLowerCase();
  const arg = parts.slice(1).join(' ').trim();

  switch (cmd) {
    case '/invoke':
      if (!arg) { _openInvokeModal(); return true; }
      _invokeAssetById(arg);
      return true;
    case '/as':
      if (!arg) { showToast('Usage: /as <asset-id>'); return true; }
      _invokeAssetById(arg);
      return true;
    case '/team':
    case '/departments':
      _openInvokeModal();
      return true;
    default:
      return false;
  }
}

function _invokeAssetById(assetId) {
  if (typeof FOLDER_SUPERSET !== 'object' || !FOLDER_SUPERSET) { showToast('No departments loaded'); return; }
  const def = FOLDER_SUPERSET[assetId];
  if (!def || !def.skill || !def.skill.systemPrompt) {
    showToast('Asset not found: ' + assetId);
    return;
  }
  _selectInvoke({
    id: assetId,
    name: def.skill.label || def.name || assetId,
    systemPrompt: def.skill.systemPrompt,
    path: '',
    source: 'department',
  });
}

// ═══════════════════════════════════════════════════════════════════════
// PER-SESSION MODEL SELECTOR (rendering + interaction)
//
// All model STATE lives in the SessionModel store (session-model.js) — the
// single owner of "what model does this session use?".  This section only
// renders that state and wires the popup.  There is deliberately NO global
// override here: a pending session's chosen model lives on the session object
// (SessionModel.setDesired) and can never bleed into a different session.
//
// The three functions below are thin compatibility shims over the store, kept
// so existing call sites keep working while every resolution flows through the
// one resolver.
// ═══════════════════════════════════════════════════════════════════════

/** System-default model (compat shim → SessionModel.getDefault). */
function _effectiveModel() {
  return (typeof SessionModel !== 'undefined')
    ? SessionModel.getDefault()
    : ((typeof defaultModel !== 'undefined' && defaultModel) || 'claude-opus-4-7');
}

/** System-default thinking level (compat shim → SessionModel.getDefaultThinking). */
function _effectiveThinking() {
  return (typeof SessionModel !== 'undefined')
    ? SessionModel.getDefaultThinking()
    : ((typeof defaultThinking !== 'undefined' && defaultThinking) || '');
}

/**
 * Compat shim retained for existing post-start call sites.  In the new
 * architecture a pending session's choice lives on the session object and is
 * consumed by start_session itself, so there is no global one-shot to clear —
 * nothing can leak into the next session.  Kept as a harmless no-op (plus a
 * belt-and-suspenders cleanup of the obsolete legacy keys) so callers that
 * still invoke it don't throw.
 */
function _clearSessionModelOverride() {
  try {
    localStorage.removeItem('_sessionModelOverride');
    localStorage.removeItem('_sessionThinkingOverride');
  } catch (e) { /* localStorage unavailable */ }
}

/**
 * Build the model badge button shown next to /invoke in the input bar.
 * Displays the effective model label; clicking opens the per-session
 * model + thinking selector popup.
 *
 * @param {boolean} [isNewSession=false] — true for brand-new sessions
 *   (button is clickable).  For already-running sessions the button is
 *   informational only (shows the session's current model).
 * @param {string}  [sessionModel=''] — model of the current live session,
 *   used in the running/idle bars to show what model is active.
 */
function _buildSessionModelBtn(isNewSession, sessionModel, sessionId) {
  if (isNewSession) {
    // Pending session: the model chosen for THIS session (desired), else the
    // system default — resolved by the one store.  No global override exists,
    // so nothing another session chose can appear here.
    const _desired = (typeof SessionModel !== 'undefined') ? SessionModel.getDesired(sessionId) : '';
    const isOverridden = !!_desired;
    const label = _runningModelLabel((typeof SessionModel !== 'undefined')
      ? SessionModel.effectivePending(sessionId)
      : ((typeof defaultModel !== 'undefined' ? defaultModel : '')), '');
    const _safeId = (sessionId || '').replace(/['"\\]/g, '');
    const _chosen = isOverridden || _hasChosenThinking(sessionId);
    return '<span class="session-model-btn' + (_chosen ? ' session-model-overridden' : '') + '" ' +
      'id="session-model-btn" ' +
      'title="' + _bubbleTitle(label, _sessionThinkingFor(true, sessionId), _chosen
        ? 'Chosen for this session' : 'Click to choose model and thinking for this session') + '">' +
      '<span class="smb-model">' + _modelLabelHtml(label) + '</span>' +
      _thinkingSegment(true, sessionId) + '</span>';
  }
  // Running/idle session: show the confirmed session model when known.
  // If not yet confirmed (dormant/sleeping session that hasn't sent an init
  // message yet), fall back to the system default — that's what it WILL run
  // on when it wakes up, so showing "—" is less useful than showing the
  // actual expectation.  The tooltip distinguishes confirmed from assumed.
  const _sysDefault = (typeof SessionModel !== 'undefined')
    ? SessionModel.getDefault()
    : (typeof defaultModel !== 'undefined' ? defaultModel : '');
  const confirmed = !!(sessionModel);
  const effectiveModel = sessionModel || _sysDefault;
  const _liveSid = (typeof liveSessionId !== 'undefined') ? liveSessionId : '';
  const label = effectiveModel ? _runningModelLabel(effectiveModel, _liveSid) : '—';
  const title = confirmed
    ? 'Session model: ' + label + ' — click to switch (applies from the next message)'
    : 'Will use system default (' + label + ') — click to switch before waking';
  return '<span class="session-model-badge' + (!confirmed ? ' session-model-default' : '') + '" ' +
    'title="' +
    _bubbleTitle(label, _sessionThinkingFor(false, _liveSid), title) + '">' +
    '<span class="smb-model">' + _modelLabelHtml(label) + '</span>' +
    _thinkingSegment(false, _liveSid) + '</span>';
}

/**
 * Open a per-session model + thinking level selector popup.
 * Uses the existing pm-overlay.  Selecting a model/thinking level sets
 * the per-session override WITHOUT affecting the system-level defaults
 * stored in localStorage.
 */
async function _openSessionModelSelector(liveMode, pendingSessionId, preset) {
  // `preset` = {model, thinking, thinkingTouched}: headless mode, used by the
  // status panel's Apply.  Nothing is shown; the function sets up exactly the
  // same state and then runs the same live apply path the modal's Apply button
  // runs, so there is one implementation of the switch/restart logic (and of
  // its confirmation, fallback and honesty rules), not two.
  const overlay = document.getElementById('pm-overlay');
  if (!overlay) return;
  if (preset) overlay.innerHTML = '';   // no stale #sm-apply-btn: the panel's Apply button owns that id

  // Live mode: switch the model of the CURRENTLY OPEN session via the
  // daemon (CLI control protocol).  Capture the target id NOW so switching
  // panels while the modal is open can't redirect the request.
  const liveSid = liveMode ? (typeof liveSessionId !== 'undefined' ? liveSessionId : null) : null;
  if (liveMode && !liveSid) {
    if (typeof showToast === 'function') showToast('No active session to switch');
    return;
  }
  const liveSess = (liveSid && typeof allSessions !== 'undefined')
    ? allSessions.find(x => x.id === liveSid) : null;
  const currentLiveModel = (liveSess && liveSess.model) ? liveSess.model : '';
  // Normalize "claude-opus-5[1m]" / dated ids to the base id so the matching
  // chip pre-selects (one chip per model; 1M comes with the model).
  const currentLiveBase = currentLiveModel.replace(/\[[^\]]*\]/g, '').replace(/-\d{8}$/, '');
  // The effort the live session is running at: daemon-reported when this
  // daemon reports it (undefined otherwise), else this tab's recorded choice.
  const _confirmedLiveThinking = (liveSid && typeof SessionModel !== 'undefined')
    ? SessionModel.getConfirmedThinking(liveSid) : undefined;
  const currentLiveThinking = (liveSid && typeof SessionModel !== 'undefined')
    ? SessionModel.resumeThinking(liveSid) : '';
  const _liveThinkingKnown = _confirmedLiveThinking !== undefined || !!currentLiveThinking;
  const _thinkingLbl = k => (typeof SessionModel !== 'undefined') ? SessionModel.thinkingLabel(k) : (k || 'Default');

  // Layout (styles: ".sm-picker" in style.css): a one-line summary, the models
  // as chips grouped one row per family, and the thinking levels as a single
  // segmented control.  The caveats about WHEN a change applies sit beside the
  // section they belong to instead of in a paragraph above everything.
  if (!preset) {
  overlay.innerHTML = '<div class="pm-card pm-enter sm-picker">' +
    '<h2 class="pm-title">Model &amp; thinking</h2>' +
    '<p class="sm-sub">' + (liveMode
      ? 'Running <strong>' + (currentLiveModel ? _modelLabel(currentLiveModel) : 'a model not yet confirmed') + '</strong>' +
        (_liveThinkingKnown ? ' at <strong>' + _thinkingLbl(currentLiveThinking) + '</strong> thinking' : '')
      : 'For <strong>this session</strong> only. The system default is unchanged.') + '</p>' +
    '<div class="sm-sec"><span>Model</span>' +
    (liveMode ? '<span class="sm-note">Applies from the next message</span>' : '') + '</div>' +
    '<div class="sm-models" id="sm-model-list"><span class="spinner"></span></div>' +
    '<div id="sm-thinking-section" style="display:none;">' +
    '<div class="sm-sec"><span>Thinking</span>' +
    (liveMode ? '<span class="sm-note">Restarts the session once idle, history kept</span>' : '') + '</div>' +
    '<div class="msel-grid" id="sm-thinking-list"></div>' +
    '<div class="msel-hint" id="sm-thinking-hint"></div>' +
    '</div>' +
    '<div class="pm-actions">' +
    (liveMode
      ? '<button class="pm-btn pm-btn-secondary" onclick="_closePm()">Cancel</button>' +
        '<button class="pm-btn pm-btn-primary" id="sm-apply-btn" disabled onclick="_applyLiveSessionChoice()">Apply</button>'
      : '<button class="pm-btn pm-btn-secondary" onclick="_clearSessionModelOverrideAndClose()">Reset to default</button>' +
        '<button class="pm-btn pm-btn-primary" id="sm-apply-btn" disabled onclick="_applySessionModelOverride()">Apply</button>') +
    '</div></div>';
  overlay.classList.add('show');
  requestAnimationFrame(() => { const c = overlay.querySelector('.pm-card'); if (c) c.classList.remove('pm-enter'); });
  overlay.onclick = e => { if (e.target === overlay) _closePm(); };
  }

  // Fetch models (only needed to draw the list)
  let models;
  if (preset) models = [];
  else try {
    const resp = await fetch('/api/models');
    models = await resp.json();
  } catch (e) {
    models = [
      {id: 'claude-fable-5-1', name: 'Fable 5.1',  desc: 'Most capable, 1M context'},
      {id: 'claude-fable-5',   name: 'Fable 5',    desc: 'Deep reasoning, 1M context'},
      {id: 'claude-opus-5-5',  name: 'Opus 5.5',   desc: 'Newest Opus, Fable-level, faster + cheaper'},
      {id: 'claude-opus-5',    name: 'Opus 5',     desc: 'Agentic coding, 1M context'},
      {id: 'claude-opus-4-8',  name: 'Opus 4.8',   desc: 'Deep reasoning, 1M context'},
      {id: 'claude-opus-4-7',  name: 'Opus 4.7',   desc: '1M context, deepest reasoning'},
      {id: 'claude-opus-4-6',  name: 'Opus 4.6',   desc: 'Deep reasoning, 200K context'},
      {id: 'claude-sonnet-5-5',name: 'Sonnet 5.5', desc: 'Newest Sonnet, 1M context'},
      {id: 'claude-sonnet-5',  name: 'Sonnet 5',   desc: 'Fast + capable, 1M context'},
      {id: 'claude-sonnet-4-6',name: 'Sonnet 4.6', desc: 'Fast, capable, balanced'},
      {id: 'claude-haiku-4-5', name: 'Haiku 4.5',  desc: 'Fastest, most cost-efficient'},
    ];
  }

  // Track pending selections.  Live mode pre-selects the session's ACTUAL
  // model; new-session mode pre-selects the override/default.
  // For new-session mode: prefer model explicitly assigned to this pending session,
  // then global override, then system default.
  let pendingModel = liveMode
    ? currentLiveBase
    : ((typeof SessionModel !== 'undefined')
        ? SessionModel.effectivePending(pendingSessionId)
        : _effectiveModel());
  let pendingThinking = liveMode
    ? currentLiveThinking
    : ((typeof SessionModel !== 'undefined')
        ? SessionModel.getDesiredThinking(pendingSessionId)
        : (typeof defaultThinking !== 'undefined' ? defaultThinking : ''));
  // Live mode restarts the session only when the user actually picked a
  // different level; merely opening the modal must never restart anything.
  let thinkingTouched = false;
  if (preset) {
    if (preset.model) pendingModel = String(preset.model).replace(/\[[^\]]*\]/g, '');
    if (typeof preset.thinking === 'string') pendingThinking = preset.thinking;
    thinkingTouched = !!preset.thinkingTouched;
  }

  function _renderModels() {
    const list = document.getElementById('sm-model-list');
    if (!list) return;
    list.innerHTML = _modelSelectorGroupsHtml(models, pendingModel);
    _groupModelChips(list);
    list.querySelectorAll('.msel-row').forEach(row =>
      row.onclick = () => window._smSelectModel(row));
  }

  function _renderThinking() {
    const section = document.getElementById('sm-thinking-section');
    const list = document.getElementById('sm-thinking-list');
    if (!section || !list) return;
    section.style.display = '';
    const levels = SessionModel.THINKING_LEVELS;
    let html = '';
    for (const l of levels) {
      // Live session with an unknown current level (older daemon): pre-select
      // nothing rather than claim a level we can't verify.
      const active = (!liveMode || _liveThinkingKnown) && l.key === pendingThinking;
      html += '<div class="msel-row msel-chip' + (active ? ' active' : '') + '" data-level="' + l.key + '" role="button" tabindex="0" title="' + l.desc + '">' +
        _MSEL_CHECK +
        '<span class="msel-name">' + l.label + '</span>' +
        '</div>';
    }
    list.innerHTML = html;
    list.querySelectorAll('.msel-row').forEach(row =>
      row.onclick = () => window._smSelectThinking(row));
    _renderThinkingHint();
  }

  // One-line description of the selected level under the chip grid.
  function _renderThinkingHint() {
    const hint = document.getElementById('sm-thinking-hint');
    if (!hint) return;
    const known = !liveMode || _liveThinkingKnown || thinkingTouched;
    const l = SessionModel.THINKING_LEVELS.find(x => x.key === pendingThinking);
    hint.textContent = (known && l) ? l.label + ': ' + l.desc
      : 'Current level not reported by this server version. Pick one to set it.';
  }

  _renderModels();
  // Thinking level is launch-time configuration (the CLI's --effort flag), so
  // in live mode a change is applied by restarting the session's CLI with
  // --resume --effort.  The modal text says so; see _applyLiveSessionChoice.
  _renderThinking();

  // Enable apply only when a selection differs from current state
  function _refreshApply() {
    const btn = document.getElementById('sm-apply-btn');
    if (btn) btn.disabled = false; // always allow apply after any interaction
  }

  // Expose helpers to inline onclick handlers
  window._smSelectModel = function(row) {
    document.querySelectorAll('#sm-model-list .msel-row').forEach(c => c.classList.remove('active'));
    row.classList.add('active');
    // Chips carry plain ids; strip any marker defensively.
    pendingModel = (row.dataset.model || '').replace(/\[[^\]]*\]/g, '');
    _refreshApply();
  };
  window._smSelectThinking = function(row) {
    document.querySelectorAll('#sm-thinking-list .msel-row').forEach(c => c.classList.remove('active'));
    row.classList.add('active');
    pendingThinking = row.dataset.level;
    thinkingTouched = true;
    _renderThinkingHint();
    _refreshApply();
  };
  window._applySessionModelOverride = function() {
    // Store the choice ON this pending session only (SessionModel owns it).
    // It is consumed by start_session for THIS session and can never bleed
    // into another — there is no global override anymore.
    if (typeof SessionModel !== 'undefined') {
      SessionModel.setDesired(pendingSessionId, pendingModel, pendingThinking);
    }
    _closePm();
    // Refresh any visible session-model-btn to show updated label
    _refreshSessionModelBtn(pendingSessionId);
    const label = _modelLabel(pendingModel);
    const thinking = pendingThinking
      ? ' + ' + _thinkingLbl(pendingThinking) + ' thinking'
      : '';
    if (typeof showToast === 'function') showToast('Session: ' + label + thinking);
  };
  window._clearSessionModelOverrideAndClose = function() {
    // Forget this pending session's choice; it reverts to the system default.
    if (typeof SessionModel !== 'undefined') SessionModel.clearDesired(pendingSessionId);
    _closePm();
    _refreshSessionModelBtn(pendingSessionId);
    if (typeof showToast === 'function') showToast('Session model reset to system default');
  };

  // ─────────────────────────────────────────────────────────────────────
  // CLI-version-too-old recovery
  // ─────────────────────────────────────────────────────────────────────
  //
  // When Anthropic ships a new model, the API rejects requests from a CLI
  // that predates it with a 400 whose ``details.error_code`` is exactly
  // ``claude_code_version_too_old``. That is a distinct failure from the
  // "not supported" case handled by _sleepThenRetry above — no amount of
  // restarting the *session* fixes it, because the *binary* on disk is
  // stale. Match either the specific error code or the human-readable
  // "version X.Y.Z or newer is required" line the API returns.
  function _isClaudeVersionTooOldError(err) {
    if (!err) return false;
    const s = String(err);
    return /claude_code_version_too_old/i.test(s)
        || /or newer is required/i.test(s)
        || /run\s+['"`]?claude update['"`]?/i.test(s);
  }

  // Show the one-click "Update Claude Code" modal, run the update, and
  // resolve to true iff the caller should retry the model switch.
  //
  // Confirms with the user first — running `claude update` closes any
  // live session's CLI on that binary. The backend refuses to update
  // when the daemon reports live sessions unless force=true, and we
  // pass force=true only after this explicit confirmation.
  function _offerClaudeUpdate(errorText, pendingModel) {
    if (typeof showConfirm !== 'function') {
      // Bare-toast fallback: no modal system loaded (e.g. an early boot
      // race). Surface the raw error so the user still sees the reason.
      if (typeof showToast === 'function') {
        showToast('Model switch FAILED: ' + errorText);
      }
      return Promise.resolve(false);
    }
    const modelLabel = _modelLabel(pendingModel);
    const body =
      '<p>The Claude Code CLI installed on this machine is too old to ' +
      'run <strong>' + escHtml(modelLabel) + '</strong>. ' +
      'Update it now to unlock the model.</p>' +
      '<p style="opacity:.75;font-size:12px;margin-top:8px">' +
      'Running <code>claude update</code> will interrupt any live ' +
      'Claude sessions on this machine. They can be resumed afterward.' +
      '</p>';
    return showConfirm('Update Claude Code', body, {
      confirmText: 'Update Now',
      cancelText: 'Not Now',
    }).then((ok) => {
      if (!ok) return false;
      if (typeof showToast === 'function') {
        showToast('Updating Claude Code — this can take up to a minute…');
      }
      return fetch('/api/admin/claude-update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ force: true }),
      })
        .then((r) => r.json().then((data) => ({ ok: r.ok, data: data || {} })))
        .then(({ ok, data }) => {
          if (!ok || !data.ok) {
            const msg = data && data.error
              ? ('Update failed: ' + data.error)
              : 'Update failed — try running `claude update` from a terminal.';
            if (typeof showToast === 'function') showToast(msg, true);
            return false;
          }
          const before = data.before || '?';
          const after = data.after || '?';
          if (data.updated) {
            if (typeof showToast === 'function') {
              showToast('Claude CLI updated: ' + before + ' → ' + after);
            }
            return true;
          }
          // No version change — usually means the CLI was already current.
          // Retry the switch anyway; if the API still refuses, the caller
          // will surface the raw error normally.
          if (typeof showToast === 'function') {
            showToast('Claude CLI already at ' + after + ' — retrying switch…');
          }
          return true;
        })
        .catch((e) => {
          if (typeof showToast === 'function') {
            showToast('Update request failed: ' + (e && e.message || e), true);
          }
          return false;
        });
    });
  }

  // Live mid-session switch.  HONESTY CONTRACT: nothing in the UI changes
  // until the daemon confirms the CLI accepted the set_model control
  // request.  On failure or timeout the badge keeps showing the real model
  // and the user gets an explicit error — never silent fake success.
  // Live-mode Apply.  A thinking change (effort is a launch flag) takes the
  // restart path, which also carries any model change in the same restart.
  // A model-only change keeps the existing live set_model path.
  window._applyLiveSessionChoice = function() {
    if (!liveMode || !liveSid) return;
    const thinkingChanged = thinkingTouched &&
      (!_liveThinkingKnown || pendingThinking !== currentLiveThinking);
    if (thinkingChanged) { _applyLiveSessionThinking(); return; }
    window._applyLiveSessionModel();
  };

  // Restart the session's CLI with --resume --effort <level> (and --model if
  // that changed too).  Honest by construction: nothing is recorded until the
  // daemon confirms the session came back idle.
  function _applyLiveSessionThinking() {
    const btn = document.getElementById('sm-apply-btn');
    const kind = (typeof sessionKinds !== 'undefined') ? sessionKinds[liveSid] : '';
    const running = (typeof runningIds !== 'undefined') && runningIds.has(liveSid);
    if (running && (kind === 'working' || kind === 'question')) {
      // Restarting mid-turn would kill the turn in flight.
      if (typeof showToast === 'function') {
        showToast('Session is busy. Change thinking once the current turn finishes.');
      }
      return;
    }
    if (typeof socket === 'undefined') {
      if (typeof showToast === 'function') showToast('Not connected, thinking NOT changed');
      return;
    }
    const level = pendingThinking || '';
    const modelChanged = !!pendingModel && pendingModel !== currentLiveBase;
    const resumeModel = modelChanged ? pendingModel
      : ((SessionModel.resumeModel ? SessionModel.resumeModel(liveSid) : '') || '');
    if (btn) { btn.disabled = true; btn.textContent = 'Restarting…'; }

    let settled = false;
    let started = false;
    const cleanup = () => {
      settled = true;
      clearTimeout(timer);
      socket.off('session_state', onState);
    };
    const timer = setTimeout(() => {
      if (settled) return;
      cleanup();
      if (btn) { btn.disabled = false; btn.textContent = 'Apply'; }
      if (typeof showToast === 'function') {
        showToast('No confirmation from the server. Check the session before retrying.');
      }
    }, 30000);

    function startAtLevel() {
      if (started) return;
      started = true;
      // Explicit restart supersedes the sleep intent marked below.
      if (typeof clearUserStopped === 'function') clearUserStopped(liveSid);
      socket.emit('start_session', {
        session_id: liveSid,
        cwd: (typeof _currentProjectDir === 'function') ? _currentProjectDir() : '',
        resume: true,
        model: resumeModel || undefined,
        // 'default' = explicit reset to the model default, so the daemon
        // does not re-pin the level the session was running at.
        thinking_level: level || 'default',
      });
    }

    function onState(d) {
      if (settled || !d || d.session_id !== liveSid) return;
      if (!started) {
        if (d.state === 'stopped') startAtLevel();
        return;
      }
      if (d.state === 'stopped' && d.error) {
        cleanup();
        if (btn) { btn.disabled = false; btn.textContent = 'Apply'; }
        if (typeof showToast === 'function') showToast('Restart FAILED: ' + d.error);
        return;
      }
      if (d.state !== 'idle' && d.state !== 'working') return;
      cleanup();
      // Daemon brought the session back.  Record the level on the session so
      // the next wake from this tab pins it; a daemon that reports `effort`
      // overwrites this with ground truth through the same write path.
      SessionModel.setDesired(liveSid, resumeModel, level);
      if (typeof d.effort === 'string') {
        SessionModel.ingestConfirmedThinking(liveSid, d.effort);
      } else {
        SessionModel.ingestConfirmedThinking(liveSid, level);
      }
      _renderSessionThinkingBadge(liveSid);
      if (modelChanged) _renderSessionModelBadge(liveSid);
      _closePm();
      if (typeof showToast === 'function') {
        showToast('Thinking set to ' + _thinkingLbl(level) +
          (modelChanged ? ' on ' + _modelLabel(pendingModel) : '') +
          '. Applies from the next message.');
      }
    }
    socket.on('session_state', onState);

    if (running) {
      // Explicit stop path: mark intent BEFORE close_session so a pending
      // ghost-recovery timer can't resurrect the session mid-restart (see the
      // sleep-must-stick rules in live-panel.js).
      if (typeof markUserStopped === 'function') markUserStopped(liveSid);
      socket.emit('close_session', { session_id: liveSid });
      // If the stopped push is lost, start anyway after a grace period.
      setTimeout(() => { if (!settled) startAtLevel(); }, 8000);
    } else {
      startAtLevel();
    }
  }

  window._applyLiveSessionModel = function() {
    if (!liveMode || !liveSid || !pendingModel) return;
    if (pendingModel === currentLiveBase) {
      _closePm();
      if (typeof showToast === 'function') showToast('Already running on ' + _modelLabel(pendingModel));
      return;
    }
    const btn = document.getElementById('sm-apply-btn');

    // STALE-CLI FALLBACK.  A live set_model control request is answered by
    // the session's already-running CLI process, which only knows the models
    // that existed when it was spawned.  A model released after that (e.g. a
    // day-one switch to a brand-new Opus) is rejected with a "… not
    // supported …" error even though the CLI binary on disk has since
    // auto-updated.  The daemon already resumes a STOPPED session with
    // --model on a FRESH CLI, so on that specific error we sleep the session
    // and re-apply exactly once — the second attempt takes the resume path
    // and succeeds.  One-shot: a "not supported" from a freshly spawned CLI
    // means the model genuinely isn't available, and that error must surface.
    let resumeFallbackUsed = false;

    function attempt() {
      if (btn) { btn.disabled = true; btn.textContent = 'Switching…'; }

      let settled = false;
      const finish = () => {
        settled = true;
        clearTimeout(timer);
        if (typeof socket !== 'undefined') {
          socket.off('session_model_result', onResult);
          socket.off('session_model_changed', onChanged);
        }
      };
      // 30s covers the daemon's own 15s CLI control-request timeout plus IPC
      // and a slow mobile link.  The old 20s could fire while the daemon was
      // still legitimately working, reporting "NOT changed" for a switch
      // that then went through.
      const timer = setTimeout(() => {
        if (settled) return;
        finish();
        if (btn) { btn.disabled = false; btn.textContent = 'Apply'; }
        if (typeof showToast === 'function') {
          showToast('No confirmation from the server — if the model badge updates, the switch went through; otherwise try again');
        }
      }, 30000);

      // CONFIRMATION FALLBACK.  `session_model_result` is a reply-only emit
      // — it goes to the exact socket that asked.  On a phone the transport
      // can silently reconnect between the request and the reply (tab
      // backgrounded, wifi↔cellular handoff), and the reply is lost.  The
      // daemon ALSO broadcasts `session_model_changed` to every client, this
      // one included, and that broadcast survives a reconnect.  Accept it as
      // confirmation when it names this session and the model we asked for,
      // so a lost reply can never turn a successful switch into a bogus
      // "NOT changed" toast while the badge quietly updates behind it.
      const _base = m => String(m || '').replace(/\[[^\]]*\]/g, '').replace(/-\d{8}$/, '');
      function onChanged(data) {
        if (settled || !data || data.session_id !== liveSid || !data.model) return;
        if (_base(data.model) !== _base(pendingModel)) return;
        onResult({ ok: true, session_id: liveSid, model: data.model,
                   resumed: !!data.resumed, turn_resumed: !!data.turn_resumed });
      }

      function onResult(data) {
        if (settled || !data || data.session_id !== liveSid) return;
        finish();
        if (data.ok) {
          // Daemon confirmed via the CLI control protocol — safe to display.
          // Route through the store's single write path, then the single badge
          // renderer.  Never write model text to the DOM directly here.
          if (typeof SessionModel !== 'undefined') SessionModel.ingestConfirmed(liveSid, data.model);
          // Mirror the choice into `desiredModel` as well.  The wake path
          // (liveSubmitContinue) sends `desiredModel` on resume, so leaving it
          // un-updated here would mean: switch live to model B, let the session
          // sleep, type to wake it — and it comes back on a STALE earlier
          // choice instead of B.  Keeping the two fields in step is what makes
          // "the model I picked is the model it wakes up on" actually true.
          if (typeof SessionModel !== 'undefined' && SessionModel.setDesired) {
            // Keep the session's thinking level: a model switch must not
            // silently reset it on the next wake.
            SessionModel.setDesired(liveSid, data.model,
              SessionModel.resumeThinking ? SessionModel.resumeThinking(liveSid) : '');
          }
          // The fallback's sleep was plumbing, not user intent — let ghost
          // recovery protect the resumed session again.
          if (resumeFallbackUsed && typeof clearUserStopped === 'function') {
            clearUserStopped(liveSid);
          }
          _renderSessionModelBadge(liveSid);
          _closePm();
          if (typeof showToast === 'function') {
            showToast('Model switched to ' + _modelLabel(data.model) + ' — applies from the next message');
          }
        } else if (!resumeFallbackUsed && /not supported/i.test(String(data.error || ''))) {
          resumeFallbackUsed = true;
          _sleepThenRetry();
        } else if (_isClaudeVersionTooOldError(data.error)) {
          // CLI binary predates this model. The daemon-restart fallback
          // above can't fix this — the CLI itself needs updating. Offer
          // one-click `claude update`, then re-attempt the switch.
          if (btn) { btn.disabled = false; btn.textContent = 'Apply'; }
          _offerClaudeUpdate(String(data.error), pendingModel).then((didUpdate) => {
            if (didUpdate) attempt();
          });
        } else {
          if (btn) { btn.disabled = false; btn.textContent = 'Apply'; }
          if (typeof showToast === 'function') {
            showToast('Model switch FAILED: ' + (data.error || 'unknown error'));
          }
        }
      }

      if (typeof socket === 'undefined') {
        finish();
        if (typeof showToast === 'function') showToast('Not connected — model NOT changed');
        return;
      }
      socket.on('session_model_result', onResult);
      socket.on('session_model_changed', onChanged);
      socket.emit('set_session_model', { session_id: liveSid, model: pendingModel });
    }

    // Sleep the session, wait for the daemon to confirm it stopped, then
    // re-apply the switch (which now takes the resume-with---model path).
    function _sleepThenRetry() {
      if (btn) { btn.disabled = true; btn.textContent = 'Restarting session…'; }
      if (typeof showToast === 'function') {
        showToast('Session’s CLI predates ' + _modelLabel(pendingModel) +
          ' — restarting the session on it…');
      }
      // Explicit stop path — mark intent BEFORE close_session so a pending
      // ghost-recovery timer can never resurrect the session mid-fallback
      // (see the sleep-must-stick rules in live-panel.js).
      if (typeof markUserStopped === 'function') markUserStopped(liveSid);

      let done = false;
      const onState = (d) => {
        if (done || !d || d.session_id !== liveSid || d.state !== 'stopped') return;
        done = true;
        clearTimeout(guard);
        socket.off('session_state', onState);
        attempt();
      };
      // If the stopped push never arrives (dropped event), retry anyway —
      // worst case the session is still live and the real error surfaces.
      const guard = setTimeout(() => {
        if (done) return;
        done = true;
        socket.off('session_state', onState);
        attempt();
      }, 8000);
      socket.on('session_state', onState);
      socket.emit('close_session', { session_id: liveSid });
    }

    attempt();
  };

  if (preset && liveMode) window._applyLiveSessionChoice();
}

/** The shared model-list builder emits a flat run (family header, rows, header,
 *  rows…).  Regroup it into one line per family: header on the left, its
 *  versions as chips on the right.  Used by the picker and the status panel. */
function _groupModelChips(list) {
  let fam = null;
  Array.from(list.children).forEach(el => {
    if (el.classList.contains('msel-group-hd')) {
      fam = document.createElement('div');
      fam.className = 'sm-fam';
      const chips = document.createElement('div');
      chips.className = 'sm-chips';
      list.insertBefore(fam, el);
      fam.appendChild(el);
      fam.appendChild(chips);
    } else if (fam) {
      fam.lastChild.appendChild(el);
    }
  });
}

/**
 * Refresh the session-model-btn in the current input bar after
 * an override is applied or cleared, without re-rendering the full bar.
 */
function _refreshSessionModelBtn(sessionId) {
  // New-session button (has id) — shows the model THIS pending session will
  // use: its desired choice, else the system default.  Resolved by the store,
  // so it can never show a value another session chose.
  const btn = document.getElementById('session-model-btn');
  if (btn && typeof SessionModel !== 'undefined') {
    const isOverridden = !!SessionModel.getDesired(sessionId) || _hasChosenThinking(sessionId);
    const _lbl = _modelLabel(SessionModel.effectivePending(sessionId));
    const _seg = btn.querySelector('.smb-model');
    if (_seg) _seg.innerHTML = _modelLabelHtml(_lbl); else btn.textContent = _lbl;
    btn.classList.toggle('session-model-overridden', isOverridden);
    btn.title = _bubbleTitle(_lbl, _sessionThinkingFor(true, sessionId), isOverridden
      ? 'Chosen for this session' : 'Click to choose model and thinking for this session');
  }

  // Running-session badge shows only the session's ACTUAL (confirmed) model —
  // delegate to the single badge renderer so there is exactly one DOM writer.
  _renderSessionModelBadge(typeof liveSessionId !== 'undefined' ? liveSessionId : '');
  _renderSessionThinkingBadge();
}

/**
 * THE single DOM writer for the running-session model badge.
 *
 * Reads the confirmed model from the store (never from a parameter, never from
 * the DOM) so every caller — socket state events, mid-session switch
 * confirmations, and bar refreshes — produces identical, race-free output.
 * Because the value always comes from the store, a late re-render can never
 * "win" with stale text.  Only updates the badge for the currently-live
 * session; it is a no-op if that badge isn't on screen.
 */
function _renderSessionModelBadge(sessionId) {
  _renderStatusPanel();
  const badge = document.querySelector('.session-model-badge');
  if (!badge) return;
  if (typeof liveSessionId === 'undefined' || !liveSessionId) return;
  // Only the live session owns the visible badge.
  if (sessionId && sessionId !== liveSessionId) return;
  const model = (typeof SessionModel !== 'undefined')
    ? SessionModel.getConfirmed(liveSessionId)
    : '';
  if (!model) return;
  const lbl = _runningModelLabel(model, liveSessionId);
  const seg = badge.querySelector('.smb-model');
  if (seg) seg.innerHTML = _modelLabelHtml(lbl); else badge.textContent = lbl;
  badge.title = _bubbleTitle(lbl, _sessionThinkingFor(false, liveSessionId),
    'Click to switch (model applies from the next message)');
}

// ═══════════════════════════════════════════════════════════════════════
// INVOKE BUTTON BUILDER (for the input bar)
// ═══════════════════════════════════════════════════════════════════════

function _buildInvokeBtn() {
  return '<button class="invoke-btn" id="invoke-btn" onclick="_openInvokeModal()" title="Invoke Workforce">' +
    '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="url(#invoke-grad)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">' +
    '<defs><linearGradient id="invoke-grad" x1="0%" y1="0%" x2="100%" y2="100%"><stop offset="0%" stop-color="#a855f7"/><stop offset="100%" stop-color="#3b82f6"/></linearGradient></defs>' +
    '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>' +
    '</svg>' +
    '<span class="invoke-btn-label">/invoke</span>' +
    '</button>';
}

/**
 * Wrap invoke button + model badge + context circle in a left-pinned group.
 *
 * @param {string}  ctxHtml       - Context bar HTML (from _buildCtxBarCompact).
 * @param {boolean} [isNewSession=false] - true only for brand-new sessions
 *   where the model button is interactive.
 * @param {string}  [sessionModel=''] - model of the current live session
 *   (used in idle/waiting/working bars for display purposes).
 */
/**
 * The thinking level a bar should show: for a pending session its chosen
 * level (else the system default it WILL start at); for a running session the
 * daemon-reported launch level (else this tab's recorded choice).  Returns
 * {key, known}; known=false when a running session's level can't be verified.
 */
function _sessionThinkingFor(isNewSession, sessionId) {
  if (typeof SessionModel === 'undefined') return { key: '', known: false };
  if (isNewSession) return { key: SessionModel.getDesiredThinking(sessionId), known: true };
  const confirmed = SessionModel.getConfirmedThinking(sessionId);
  const recorded = SessionModel.resumeThinking(sessionId);
  return { key: recorded, known: confirmed !== undefined || !!recorded };
}

/** True when this pending session has its own thinking choice (vs default). */
function _hasChosenThinking(sid) {
  if (typeof allSessions === 'undefined' || !Array.isArray(allSessions)) return false;
  const s = allSessions.find(x => x && x.id === sid);
  return !!(s && typeof s.desiredThinking === 'string');
}

// Thinking level in the bar: a small brain after the model name that fills
// from the bottom with the level (low 1/5 … max 5/5).  No word, nothing under
// the name.  "Default" is NOT "no thinking" (the model thinks at its own
// level), so it is a whole brain in the mid grey used for anything you did not
// pick; a level you chose fills in the strong neutral.  Never the accent
// colour: accent is reserved for the bar's one primary action (mic / send).
const _THINK_STEPS = {low: 1, medium: 2, high: 3, xhigh: 4, max: 5};
const _BRAIN_D = 'M12 4.2C10.6 2.6 7.6 2.9 6.9 5.1 4.7 5.3 3.5 7.5 4.4 9.4 3 10.7 3.1 13 4.7 14.1 4.4 16.3 6.1 18.2 8.3 18 9.1 19.8 11 20.3 12 19.2 13 20.3 14.9 19.8 15.7 18 17.9 18.2 19.6 16.3 19.3 14.1 20.9 13 21 10.7 19.6 9.4 20.5 7.5 19.3 5.3 17.1 5.1 16.4 2.9 13.4 2.6 12 4.2Z';
const _BRAIN_FOLDS = '<path class="smb-brain-folds" d="M12 4.6V19M8.2 8.2c1.3.2 2 1 2 2.2M15.8 8.2c-1.3.2-2 1-2 2.2M7.4 13.6c1.2-.5 2.3-.2 2.9.8M16.6 13.6c-1.2-.5-2.3-.2-2.9.8" fill="none" stroke-width="1.3" stroke-linecap="round"/>';
let _brainSeq = 0;

function _brainSvg(t) {
  if (!t || !t.known) return '';
  const n = _THINK_STEPS[t.key] || 0;
  const open = '<svg class="smb-brain" width="17" height="17" viewBox="0 0 24 24" aria-hidden="true">';
  if (!n) return open + '<path class="smb-brain-auto" d="' + _BRAIN_D + '"/>' + _BRAIN_FOLDS + '</svg>';
  const id = 'smb-bc' + (++_brainSeq);
  const y = 3 + 17.4 * (1 - n / 5);
  return open + '<defs><clipPath id="' + id + '"><rect x="0" y="' + y.toFixed(2) + '" width="24" height="24"/></clipPath></defs>' +
    '<path class="smb-brain-trk" d="' + _BRAIN_D + '"/>' +
    '<path class="smb-brain-fill" d="' + _BRAIN_D + '" clip-path="url(#' + id + ')"/>' + _BRAIN_FOLDS + '</svg>';
}

/** Badge label for a RUNNING session: the model plus "1M" when the session's
 *  context window is 1M.  Uses the same rule as the context readouts
 *  (window._ctxWindowFor), so the badge and the context bar can never
 *  disagree.  The CLI's "[1m]" marker alone undercounted: it is missing on
 *  most sessions that are in fact running at 1M. */
function _runningModelLabel(model, sessionId) {
  const m = String(model || '');
  const base = _modelLabel(m.replace(/\[[^\]]*\]/g, ''));
  const w = (typeof window._ctxWindowFor === 'function' && sessionId) ? window._ctxWindowFor(sessionId) : null;
  // The family rule is applied to `model` directly too, because a pending or
  // not-yet-confirmed session has no recorded model for _ctxWindowFor to read.
  const is1M = /\[1m\]/.test(m) || /^claude-(fable|opus|sonnet)-/.test(m) || !!(w && w.size >= 1000000);
  return base + (is1M ? ' 1M' : '');
}

/** "Opus 5.5" -> label "Opus" + value "5.5". */
function _modelLabelHtml(label) {
  const s = String(label || '').trim();
  const i = s.indexOf(' ');
  if (i < 0) return '<span class="smb-val">' + escHtml(s) + '</span>';
  return '<span class="smb-lbl">' + escHtml(s.slice(0, i)) + '</span> ' +
    '<span class="smb-val">' + escHtml(s.slice(i + 1)) + '</span>';
}

/** The brain after the model name.  Hidden when a running session's level
 *  can't be verified (older daemon) rather than guess. */
function _thinkingSegment(isNewSession, sessionId) {
  if (typeof SessionModel === 'undefined') return '';
  const t = _sessionThinkingFor(isNewSession, sessionId);
  return '<span class="smb-think" data-new="' + (isNewSession ? '1' : '') + '" data-sid="' +
    escHtml(sessionId || '') + '"' + (t.known ? '' : ' hidden') + ' aria-hidden="true">' + _brainSvg(t) + '</span>';
}

/** Combined tooltip for the bubble. */
function _bubbleTitle(modelLabel, t, hint) {
  const think = (t && t.known && typeof SessionModel !== 'undefined')
    ? SessionModel.thinkingLabel(t.key) : 'unknown';
  return 'Model: ' + modelLabel + ' · Thinking: ' + think + (hint ? ' — ' + hint : '');
}

/** THE single DOM writer for the bubble's thinking segment.  Reads the store. */
function _renderSessionThinkingBadge(sessionId) {
  const seg = document.querySelector('.smb-think');
  if (!seg || typeof SessionModel === 'undefined') return;
  const isNew = seg.dataset.new === '1';
  const sid = seg.dataset.sid || '';
  if (sessionId && sid && sessionId !== sid) return;
  const t = _sessionThinkingFor(isNew, sid);
  seg.hidden = !t.known;
  seg.innerHTML = _brainSvg(t);
  const btn = seg.closest('.session-model-badge, .session-model-btn');
  if (!btn) { _renderStatusPanel(); return; }
  const modelLbl = (btn.querySelector('.smb-model') || btn).textContent;
  if (isNew) {
    const chosen = !!SessionModel.getDesired(sid) || _hasChosenThinking(sid);
    btn.classList.toggle('session-model-overridden', chosen);
    btn.title = _bubbleTitle(modelLbl, t, chosen
      ? 'Chosen for this session' : 'Click to choose model and thinking for this session');
  } else {
    btn.title = _bubbleTitle(modelLbl, t, 'Click to switch (model applies from the next message)');
  }
  _renderStatusPanel();
}

// ═══════════════════════════════════════════════════════════════════════
// USAGE LIMITS GAUGES — "5h 16% · 7d 46% · Fable 14%", each over a hairline meter
//
// Account-wide, so every bar shows the same values.  Source of truth is the
// daemon (daemon/usage_limits.py), fed by the CLI's rate_limit_event on every
// turn: loaded once from /api/usage-limits, then kept live by the
// `usage_limits` socket event.  Values are "as of the last Claude reply";
// usage elsewhere (claude.ai, other machines) shows up on the next reply.
// ═══════════════════════════════════════════════════════════════════════

window._usageLimits = window._usageLimits || null;

const _USAGE_WINDOW_ORDER = [
  {key: 'five_hour', label: '5h', name: 'Session (5-hour) limit'},
  {key: 'seven_day', label: '7d', name: 'Weekly limit'},
  {key: 'seven_day_overage_included', label: 'Fable', name: 'Weekly Fable limit'},
];

function _fmtUsageTime(sec) {
  if (!sec) return '';
  const d = new Date(sec * 1000);
  const sameDay = d.toDateString() === new Date().toDateString();
  const t = d.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
  return sameDay ? t : d.toLocaleDateString([], {weekday: 'short'}) + ' ' + t;
}

/** Collapsed markup + tooltip for the status control, or null when nothing is
 *  known yet.
 *
 * One mini column per window that has a reading, filled to its percentage.
 * Grey is a normal reading; amber past 80% and red past 95% are the only colours.
 * No numbers here: the numbers, reset times and everything else are in the
 * panel the control opens (_statusPanelHtml).  A window the CLI has never
 * reported is left out; one whose reset time has passed is drawn empty.
 */
function _usageLimitsParts() {
  const rows = _usageRows();
  const shown = rows.filter(r => r.known);
  if (!shown.length) return null;  // no turn seen yet: show nothing, not zeros
  const data = window._usageLimits;
  const tips = rows.map(r => r.tip);
  if (data.updated_at) tips.push('As of ' + _fmtUsageTime(data.updated_at));
  return {
    html: shown.map(r => '<span class="ulp-c' + r.cls + '"><i style="height:' +
      (r.pct > 0 ? Math.max(8, Math.min(100, r.pct)) : 0) + '%"></i></span>').join(''),
    title: tips.join('\n'),
  };
}

/** One entry per usage window, shared by the collapsed columns and the panel. */
function _usageRows() {
  const windows = (window._usageLimits && window._usageLimits.windows) || {};
  const nowSec = Date.now() / 1000;
  return _USAGE_WINDOW_ORDER.map(w => {
    const v = windows[w.key];
    const fable = w.key === 'seven_day_overage_included';
    if (!v) {
      return {name: w.name, known: false, pct: 0, cls: '', note: 'Not reported yet',
        tip: w.name + ': not reported yet' + (fable ? ' (appears after a Fable reply)' : '')};
    }
    let cls = '';
    let pct = Math.max(0, Math.round(v.percent));
    if (v.resets_at && v.resets_at <= nowSec) {
      return {name: w.name, known: true, pct: 0, cls: cls + ' ulp-stale',
        note: 'Reset at ' + _fmtUsageTime(v.resets_at) + ', updates after the next reply',
        tip: w.name + ': reset at ' + _fmtUsageTime(v.resets_at) + ', updates after the next reply'};
    }
    if (pct >= 95) cls += ' ulp-crit';
    else if (pct >= 80) cls += ' ulp-warn';
    return {name: w.name, known: true, pct: pct, cls: cls,
      note: v.resets_at ? 'Resets ' + _fmtUsageTime(v.resets_at) : '',
      tip: w.name + ': ' + pct + '% used' + (v.resets_at ? ', resets ' + _fmtUsageTime(v.resets_at) : '')};
  });
}

function _usageLimitsPillHtml() {
  const p = _usageLimitsParts();
  return '<span class="usage-limits-pill"' + (p ? '' : ' hidden') +
    ' title="' + escHtml(p ? p.title : '') + '">' + (p ? p.html : '') + '</span>';
}

/** The context window size a session runs in.  Exposed on window so
 *  live-panel's _buildCtxBarCompact, the status panel and the badge share one
 *  rule (no two places diverging on what % means). */
window._ctxWindowFor = function (sessionId) {
  const sess = (typeof allSessions !== 'undefined' && Array.isArray(allSessions))
    ? allSessions.find(x => x && x.id === sessionId) : null;
  // The [1m] marker alone is NOT reliable: the CLI turns 1M on by itself, and
  // the daemon logs show Opus 4.6 through 5.5 and Fable sessions launched on
  // plain ids running to ~1M tokens (checked 2026-10-05).  So every 1M-capable
  // family (Fable, Opus, Sonnet) is measured against 1M; Haiku, which rejects
  // 1M, against 200K.  A reading above 200K proves a 1M window regardless.
  const u = window._sessionUsage && window._sessionUsage[sessionId];
  // Session list not loaded yet: the reply's own model (captured with the
  // reading), else the default it would run on.
  const m = (sess && sess.model) || (u && u.model) ||
    ((typeof SessionModel !== 'undefined') ? SessionModel.getDefault() : '');
  const tokens = u ? (u.input_tokens || 0) + (u.cache_read_input_tokens || 0) + (u.cache_creation_input_tokens || 0) : 0;
  const is1M = /\[1m\]/.test(m) || /^claude-(fable|opus|sonnet)-/.test(m) || tokens > 200000;
  return is1M ? {size: 1000000, label: '1M'} : {size: 200000, label: '200K'};
};

/** No live reading yet (page just loaded, session idle): ask the server for
 *  the transcript's last reply, once per session per 30s.  A live
 *  `message_start` reading always wins, because it is written straight into
 *  window._sessionUsage and this only fills an EMPTY slot. */
function _fetchContextUsage(sid) {
  window._ctxFetchAt = window._ctxFetchAt || {};
  const now = Date.now();
  if (window._ctxFetchAt[sid] && now - window._ctxFetchAt[sid] < 30000) return;
  window._ctxFetchAt[sid] = now;
  const proj = localStorage.getItem('activeProject') || '';
  fetch('/api/session-context/' + encodeURIComponent(sid) + (proj ? '?project=' + encodeURIComponent(proj) : ''))
    .then(r => r.ok ? r.json() : null)
    .then(d => {
      if (!d || !d.usage) return;
      window._sessionUsage = window._sessionUsage || {};
      if (window._sessionUsage[sid]) return;          // a live reading arrived first
      window._sessionUsage[sid] = d.usage;
      if (typeof liveSessionId !== 'undefined' && sid === liveSessionId) {
        if (typeof liveBarState !== 'undefined') liveBarState = null;
        if (typeof updateLiveInputBar === 'function') updateLiveInputBar();
        _renderStatusPanel();                           // its Context row too
      }
    })
    .catch(() => {});
}

/** Context-window pill, drawn as one column matching the usage-limits pill's
 *  grammar.  Shows how much of the live session's context window is consumed
 *  (same arithmetic as _buildCtxBarCompact and the panel's Context row).
 *  Thresholds mirror the panel: amber at 70%, red at 90% — the pill is a
 *  smaller echo of the same number the panel shows in words.  Hidden
 *  entirely when no usage reading exists for this session yet, so a pending
 *  or just-woken session's bar never gets an empty-looking column. */
function _contextPillParts(sessionId) {
  const sid = sessionId || (typeof liveSessionId !== 'undefined' ? liveSessionId : '');
  if (!sid) return null;
  const u = (window._sessionUsage && window._sessionUsage[sid]) || null;
  if (!u) { _fetchContextUsage(sid); return null; }
  const tokens = (u.input_tokens || 0) + (u.cache_read_input_tokens || 0) + (u.cache_creation_input_tokens || 0);
  const w = window._ctxWindowFor(sid);
  if (tokens <= 0 || tokens > w.size * 1.5) return null;
  const pct = Math.min(100, Math.round((tokens / w.size) * 100));
  const cls = pct >= 90 ? ' ulp-crit' : pct >= 70 ? ' ulp-warn' : '';
  return {pct: pct, cls: cls, title: 'Context: ' + pct + '% of ' + w.label + ' used'};
}

function _contextPillHtml(sessionId) {
  const p = _contextPillParts(sessionId);
  return '<span class="context-pill"' + (p ? '' : ' hidden') +
    ' title="' + escHtml(p ? p.title : '') + '">' +
    (p ? '<span class="ulp-c' + p.cls + '"><i style="height:' +
      (p.pct > 0 ? Math.max(8, Math.min(100, p.pct)) : 0) + '%"></i></span>' +
      '<span class="ctx-pct' + p.cls + '">' + p.pct + '%</span>' : '') +
    '</span>';
}

/** THE single DOM writer for every visible pill. */
function _renderUsageLimits() {
  const p = _usageLimitsParts();
  document.querySelectorAll('.usage-limits-pill').forEach(el => {
    el.hidden = !p;
    el.innerHTML = p ? p.html : '';
    el.title = p ? p.title : '';
  });
  _renderStatusPanel();
}

/** Store a snapshot (from the REST load or the socket event) and repaint. */
function _ingestUsageLimits(data) {
  if (!data || typeof data !== 'object' || !data.windows) return;
  window._usageLimits = data;
  _renderUsageLimits();
}

/** "Refresh" in the status panel: ask the server for a fresh Fable reading (a hidden
 *  one-word Fable turn; see app/usage_probe.py).  The new numbers arrive on
 *  the `usage_limits` socket event, so there is nothing to do with the reply
 *  beyond telling the user whether a refresh actually started. */
function _refreshUsageLimits() {
  fetch('/api/usage-limits/refresh', {method: 'POST'}).then(r => {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }).then(d => {
    if (typeof showToast === 'function') {
      showToast(d && d.started ? 'Refreshing usage limits…' : 'Usage limits were refreshed a moment ago');
    }
  }).catch(() => {
    if (typeof showToast === 'function') showToast('Could not refresh usage limits', true);
  });
}

function _loadUsageLimits() {
  fetch('/api/usage-limits').then(r => r.json()).then(_ingestUsageLimits).catch(() => {});
}

// A window can expire while the page sits open; re-evaluate once a minute.
// DOM-only (no network, no socket), so it cannot disturb the connection.
if (!window._usageLimitsTicker) {
  window._usageLimitsTicker = setInterval(() => {
    if (window._usageLimits) _renderUsageLimits();
  }, 60000);
}

// ═══════════════════════════════════════════════════════════════════════
// STATUS CONTROL — the bar's whole left side is ONE small control: the model
// gauge (name + thinking-level track) and a mini column per usage limit.
// Clicking it opens a panel with everything in words and numbers: model and
// thinking (with "Change" → the picker), every limit with its reset time,
// this session's context (with "Compact"), and a refresh.
// ═══════════════════════════════════════════════════════════════════════

const _STATUS_CARET = '<svg class="vn-status-caret" width="10" height="10" viewBox="0 0 24 24" fill="none" ' +
  'stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<path d="m6 15 6-6 6 6"/></svg>';

let _statusPanelFor = null;   // {isNew, sid} of the control that opened the panel

function _buildBarLeftGroup(ctxHtml, isNewSession, sessionModel, sessionId) {
  // ctxHtml (the context ring) is no longer drawn in the bar: context lives in
  // the panel.  The parameter stays so the five render paths need no change.
  const open = !!document.getElementById('vn-status-panel');
  return '<div class="bar-left-group">' +
    '<div class="vn-status' + (open ? ' open' : '') + '" role="button" tabindex="0" aria-haspopup="dialog"' +
    ' data-new="' + (isNewSession ? '1' : '') + '" data-sid="' + escHtml(sessionId || '') + '"' +
    ' onclick="_toggleStatusPanel(this)"' +
    ' onkeydown="if(event.key===\'Enter\'||event.key===\' \'){event.preventDefault();_toggleStatusPanel(this)}">' +
    _buildSessionModelBtn(isNewSession || false, sessionModel || '', sessionId || '') +
    _usageLimitsPillHtml() +
    _contextPillHtml(isNewSession ? '' : (sessionId || (typeof liveSessionId !== 'undefined' ? liveSessionId : ''))) +
    _STATUS_CARET +
    '</div></div>';
}

/** What the panel's session is running, and what is staged on top of it. */
function _statusState() {
  const c = _statusPanelFor;
  const SM = (typeof SessionModel !== 'undefined') ? SessionModel : null;
  const sid = c.isNew ? c.sid : ((typeof liveSessionId !== 'undefined' && liveSessionId) || '');
  // Normalizes for comparison and apply: one chip per model, so "[1m]" and a
  // date suffix must not make the running model look different from its chip.
  const strip = m => String(m || '').replace(/\[[^\]]*\]/g, '').replace(/-\d{8}$/, '');
  const model = strip(SM ? (c.isNew ? SM.effectivePending(sid) : (SM.getConfirmed(sid) || SM.getDefault())) : '');
  const t = _sessionThinkingFor(c.isNew, sid);
  const think = t.known ? (t.key || '') : null;            // null = level not verifiable
  const pModel = (c.pModel != null) ? c.pModel : model;    // staged (live sessions only)
  const pThink = (c.pThink != null) ? c.pThink : think;
  return {c, SM, sid, model, think, pModel, pThink, strip,
    dModel: strip(pModel) !== model, dThink: c.pThink != null && c.pThink !== think};
}

function _statusPanelHtml() {
  if (!_statusPanelFor) return '';
  const st = _statusState(), c = st.c, SM = st.SM, sid = st.sid;

  // Model: chips, one row per family.  Selection is strong neutral, never accent.
  let h = '<div class="vsp-sec"><span>Model</span></div><div class="sm-models vsp-models" id="vsp-models">';
  const models = window._statusModels;
  if (!models) h += '<span class="spinner"></span>';
  else if (!models.length) h += '<div class="vsp-empty">Model list unavailable.</div>';
  else h += _modelSelectorGroupsHtml(models, st.pModel);
  h += '</div>';

  if (SM) {
    h += '<div class="vsp-sec"><span>Thinking</span></div><div class="msel-grid vsp-think">';
    for (const l of SM.THINKING_LEVELS) {
      const on = st.pThink !== null && l.key === st.pThink;
      const was = !on && st.dThink && st.think !== null && l.key === st.think;
      h += '<div class="msel-row msel-chip' + (on ? ' active' : '') + (was ? ' was' : '') + '" data-level="' + l.key +
        '" role="button" tabindex="0" title="' + escHtml(l.desc) + '"><span class="msel-name">' + escHtml(l.label) + '</span></div>';
    }
    h += '</div>';
    // No level reported (a sleeping session the engine has not loaded): say so
    // instead of leaving an unexplained strip with nothing selected.
    if (st.pThink === null) h += '<div class="vsp-hint">Level not reported while this session is asleep. Pick one to set it.</div>';
  }

  const at = window._usageLimits && window._usageLimits.updated_at;
  h += '<div class="vsp-sec"><span>Usage limits</span><em>' + (at ? 'Updated ' + _fmtUsageTime(at) + ' · ' : '') +
    '<a role="button" tabindex="0" data-act="refresh">Refresh</a></em></div>';
  const rows = _usageRows();
  if (!rows.some(r => r.known)) {
    h += '<div class="vsp-empty">Shown after the first Claude reply.</div>';
  } else {
    rows.forEach((r, i) => {
      h += '<div class="vsp-row' + r.cls + '"' + (i ? '' : ' style="margin-top:0"') + '><span>' + escHtml(r.name) + '</span>' +
        '<span class="vsp-pct">' + (r.known ? r.pct + '%' : '') + '</span>' +
        (r.known ? '<span class="vsp-trk"><i style="width:' + Math.min(100, r.pct) + '%"></i></span>' : '') +
        (r.note ? '<small>' + escHtml(r.note) + '</small>' : '') + '</div>';
    });
  }

  // Context: same arithmetic as _buildCtxBarCompact (live-panel.js) and
  // _contextPillParts above.  The window size tracks the CLI's [1m] marker
  // so a 1M session doesn't read 100% full at 20% actual fill.
  const u = (!c.isNew && sid && window._sessionUsage && window._sessionUsage[sid]) || null;
  const tokens = u ? (u.input_tokens || 0) + (u.cache_read_input_tokens || 0) + (u.cache_creation_input_tokens || 0) : 0;
  const cw = window._ctxWindowFor(sid);
  if (tokens > 0 && tokens <= cw.size * 1.5) {
    const pct = Math.min(100, Math.round((tokens / cw.size) * 100));
    const working = (typeof liveBarState === 'string') && liveBarState.indexOf('working') === 0;
    const cls = pct >= 90 ? ' ulp-crit' : pct >= 70 ? ' ulp-warn' : '';
    h += '<div class="vsp-sec"><span>Context</span>' +
      (working ? '' : '<em><a role="button" tabindex="0" data-act="compact">Compact</a></em>') + '</div>' +
      '<div class="vsp-row' + cls + '" style="margin-top:0"><span>' + cw.label + ' window</span><span class="vsp-pct">' + pct + '%</span>' +
      '<span class="vsp-trk"><i style="width:' + pct + '%"></i></span></div>';
  }

  // Apply appears only when something staged differs from what is running,
  // with the consequence next to it.  It is the panel's one accent element.
  if (!c.isNew && (st.dModel || st.dThink)) {
    h += '<div class="vsp-foot"><span>' + (st.dThink ? 'Restarts the session once idle. History is kept.'
      : 'Applies from your next message.') + '</span><span class="vsp-actions">' +
      '<a role="button" tabindex="0" data-act="cancel">Cancel</a>' +
      '<button class="vsp-apply" id="sm-apply-btn" data-act="apply">Apply</button></span></div>';
  }
  return h;
}

/** One delegated handler for everything clickable in the panel. */
function _statusPanelClick(e) {
  const c = _statusPanelFor;
  if (!c) return;
  const row = e.target.closest('.msel-row'), act = e.target.closest('[data-act]');
  if (act) {
    const a = act.dataset.act;
    if (a === 'refresh') _refreshUsageLimits();                       // numbers arrive by push; panel stays open
    else if (a === 'compact') { _closeStatusPanel(); if (typeof liveCompact === 'function') liveCompact(); }
    else if (a === 'cancel') { c.pModel = c.pThink = null; _renderStatusPanel(); }
    else if (a === 'apply') {
      const st = _statusState();
      // Same code path as the picker modal's Apply (see `preset` there).
      _openSessionModelSelector(true, undefined, {model: st.strip(st.pModel),
        thinking: st.pThink === null ? '' : st.pThink, thinkingTouched: st.dThink});
    }
    return;
  }
  if (!row) return;
  const st = _statusState();
  if (c.isNew) {
    // A session that has not started: nothing to restart, so a pick applies at
    // once (it is only recorded on this pending session).
    const model = row.dataset.model != null ? st.strip(row.dataset.model) : st.model;
    const think = row.dataset.level != null ? row.dataset.level : (st.think || '');
    if (st.SM) st.SM.setDesired(st.sid, model, think);
    _refreshSessionModelBtn(st.sid);
    _renderStatusPanel();
    return;
  }
  if (row.dataset.model != null) c.pModel = (st.strip(row.dataset.model) === st.model) ? null : row.dataset.model;
  else if (row.dataset.level != null) c.pThink = (row.dataset.level === st.think) ? null : row.dataset.level;
  _renderStatusPanel();
}

function _loadStatusModels() {
  if (window._statusModels || window._statusModelsLoading) return;
  window._statusModelsLoading = true;
  fetch('/api/models').then(r => r.json()).then(m => { window._statusModels = Array.isArray(m) ? m : []; })
    .catch(() => { window._statusModels = []; })
    .then(() => { window._statusModelsLoading = false; _renderStatusPanel(); });
}

/** Anchor the panel above the control, left edges flush (which is also the
 *  textarea's left edge), growing upward. */
function _positionStatusPanel() {
  const p = document.getElementById('vn-status-panel');
  const trig = document.querySelector('.vn-status');
  if (!p) return;
  if (!trig) { _closeStatusPanel(); return; }
  const r = trig.getBoundingClientRect();
  p.style.left = Math.max(8, Math.min(r.left, window.innerWidth - p.offsetWidth - 8)) + 'px';
  p.style.bottom = Math.round(window.innerHeight - r.top + 10) + 'px';
}

/** THE single writer for an open panel; a no-op when it is closed.  Called by
 *  every renderer whose data the panel shows (limits, model, thinking). */
function _renderStatusPanel() {
  const p = document.getElementById('vn-status-panel');
  if (!p || !_statusPanelFor) return;
  // An apply is in flight (the shared apply path disables the button and writes
  // its progress into it).  Leave the panel alone until it settles: on success
  // the staged values equal the running ones and the footer goes away; on
  // failure the apply path re-enables the button.
  const btn = document.getElementById('sm-apply-btn');
  const st = _statusState();
  if (btn && btn.disabled && (st.dModel || st.dThink)) return;
  if (!st.dModel) _statusPanelFor.pModel = null;     // applied (or picked back): nothing staged
  if (!st.dThink) _statusPanelFor.pThink = null;
  p.innerHTML = _statusPanelHtml();
  const list = document.getElementById('vsp-models');
  if (list && list.querySelector('.msel-group-hd')) {
    _groupModelChips(list);
    // Outline what is running while something else is staged.
    if (st.dModel) list.querySelectorAll('.msel-row').forEach(r => {
      if (st.strip(r.dataset.model) === st.model && !r.classList.contains('active')) r.classList.add('was');
    });
  }
  _positionStatusPanel();
}

function _closeStatusPanel() {
  const p = document.getElementById('vn-status-panel');
  if (p) p.remove();
  _statusPanelFor = null;
  document.querySelectorAll('.vn-status.open').forEach(el => el.classList.remove('open'));
}

function _toggleStatusPanel(trig) {
  if (document.getElementById('vn-status-panel')) { _closeStatusPanel(); return; }
  _statusPanelFor = {isNew: trig.dataset.new === '1', sid: trig.dataset.sid || ''};
  const p = document.createElement('div');
  p.id = 'vn-status-panel';
  p.className = 'vn-status-panel';
  p.setAttribute('role', 'dialog');
  p.setAttribute('aria-label', 'Model, thinking and usage limits');
  p.addEventListener('click', _statusPanelClick);
  document.body.appendChild(p);
  trig.classList.add('open');
  _loadStatusModels();
  _renderStatusPanel();
}

if (!window._statusPanelBound) {
  window._statusPanelBound = true;
  document.addEventListener('click', e => {
    if (!document.getElementById('vn-status-panel')) return;
    const t = e.target;
    if (t && t.closest && (t.closest('#vn-status-panel') || t.closest('.vn-status'))) return;
    _closeStatusPanel();
  }, true);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') _closeStatusPanel(); });
  window.addEventListener('resize', _positionStatusPanel);
}

// ═══════════════════════════════════════════════════════════════════════
// FETCH LOCAL SKILLS / AGENTS
// ═══════════════════════════════════════════════════════════════════════

async function _fetchLocalWorkforce() {
  if (_localWfCache && (Date.now() - _localWfCache.ts < _LOCAL_WF_TTL)) {
    return { skills: _localWfCache.skills, agents: _localWfCache.agents };
  }
  try {
    const resp = await fetch('/api/invoke/discover?depth=2');
    const data = await resp.json();
    if (data.ok) {
      _localWfCache = {
        ts: Date.now(),
        skills: data.local_skills || [],
        agents: data.local_agents || [],
      };
      return { skills: _localWfCache.skills, agents: _localWfCache.agents };
    }
  } catch (e) {
    console.warn('Failed to fetch local workforce:', e);
  }
  return { skills: [], agents: [] };
}

// ═══════════════════════════════════════════════════════════════════════
// INVOKE MODAL
// ═══════════════════════════════════════════════════════════════════════

async function _openInvokeModal() {
  if (_invokeModalOpen) { _closeInvokeModal(); return; }

  // Create overlay
  const overlay = document.createElement('div');
  overlay.id = 'invoke-modal-overlay';
  overlay.className = 'invoke-overlay';
  overlay.onclick = (e) => { if (e.target === overlay) _closeInvokeModal(); };

  // Modal card
  const modal = document.createElement('div');
  modal.className = 'invoke-modal';
  modal.innerHTML = '<div class="invoke-modal-loading"><span class="spinner"></span> Loading workforce\u2026</div>';
  overlay.appendChild(modal);
  document.body.appendChild(overlay);
  _invokeModalOpen = true;

  requestAnimationFrame(() => overlay.classList.add('show'));

  // Fetch data
  const local = await _fetchLocalWorkforce();
  const departments = _getDeptTree();

  // Build modal content
  let h = '';
  // Header
  h += '<div class="invoke-modal-header">';
  h += '<h2 class="invoke-modal-title">Invoke Workforce</h2>';
  h += '<button class="invoke-modal-close" onclick="_closeInvokeModal()">&times;</button>';
  h += '</div>';

  // Search
  h += '<input type="text" class="invoke-search" id="invoke-search" placeholder="Search skills, agents, departments\u2026" oninput="_filterInvokeModal(this.value)">';

  // Reset registry — stores item data by index to avoid inline JSON in onclick
  _invokeRegistry = [];

  // Sections container
  h += '<div class="invoke-sections">';

  // --- Local Skills ---
  h += '<div class="invoke-section" data-section="skills">';
  h += '<div class="invoke-section-label">Local Skills</div>';
  h += '<div class="invoke-section-scroll" id="invoke-skills">';
  if (local.skills.length) {
    for (const sk of local.skills) {
      const name = sk.name || _prettifyName(sk.id);
      const idx = _invokeRegistry.length;
      _invokeRegistry.push({id:sk.id, name:name, systemPrompt:sk.systemPrompt||'', path:sk.path||'', source:'local_skill'});
      h += '<div class="invoke-item" data-search="' + escHtml(name.toLowerCase()) + '" onclick="_selectInvokeByIdx(' + idx + ')">';
      h += '<div class="invoke-item-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg></div>';
      h += '<div class="invoke-item-info"><div class="invoke-item-name">' + escHtml(name) + '</div>';
      if (sk.relativePath) h += '<div class="invoke-item-path">' + escHtml(sk.relativePath) + '</div>';
      h += '</div></div>';
    }
  } else {
    h += '<div class="invoke-empty">No local skills found. Add <code>.md</code> files to your project\'s <code>skills/</code> folder.</div>';
  }
  h += '</div></div>';

  // --- Local Agents ---
  h += '<div class="invoke-section" data-section="agents">';
  h += '<div class="invoke-section-label">Local Agents</div>';
  h += '<div class="invoke-section-scroll" id="invoke-agents">';
  if (local.agents.length) {
    for (const ag of local.agents) {
      const name = ag.name || _prettifyName(ag.id);
      const idx = _invokeRegistry.length;
      _invokeRegistry.push({id:ag.id, name:name, systemPrompt:ag.systemPrompt||'', path:ag.path||'', source:'local_agent'});
      h += '<div class="invoke-item" data-search="' + escHtml(name.toLowerCase()) + '" onclick="_selectInvokeByIdx(' + idx + ')">';
      h += '<div class="invoke-item-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="11" width="18" height="10" rx="2"/><circle cx="12" cy="5" r="2"/><line x1="12" y1="7" x2="12" y2="11"/><circle cx="8" cy="16" r="1" fill="currentColor"/><circle cx="16" cy="16" r="1" fill="currentColor"/></svg></div>';
      h += '<div class="invoke-item-info"><div class="invoke-item-name">' + escHtml(name) + '</div>';
      if (ag.relativePath) h += '<div class="invoke-item-path">' + escHtml(ag.relativePath) + '</div>';
      h += '</div></div>';
    }
  } else {
    h += '<div class="invoke-empty">No local agents found. Add <code>.md</code> files to your project\'s <code>agents/</code> folder.</div>';
  }
  h += '</div></div>';

  // --- Departments ---
  h += '<div class="invoke-section" data-section="departments">';
  h += '<div class="invoke-section-label">Departments</div>';
  h += '<div class="invoke-section-scroll" id="invoke-departments">';
  if (departments.length) {
    for (const dept of departments) {
      h += _buildInvokeDeptNode(dept, 0);
    }
  } else {
    h += '<div class="invoke-empty">No departments configured. Visit the Workforce view to set them up.</div>';
  }
  h += '</div></div>';

  h += '</div>'; // .invoke-sections

  // Footer
  h += '<div class="invoke-modal-footer">';
  h += '<button class="invoke-manage-btn" onclick="_goToWorkforce()">Manage Workforce</button>';
  h += '</div>';

  modal.innerHTML = h;

  // Focus search
  const search = document.getElementById('invoke-search');
  if (search) search.focus();

  // Escape key
  modal._escHandler = (e) => { if (e.key === 'Escape') _closeInvokeModal(); };
  document.addEventListener('keydown', modal._escHandler);
}

function _closeInvokeModal() {
  const overlay = document.getElementById('invoke-modal-overlay');
  if (overlay) {
    if (overlay.querySelector('.invoke-modal')._escHandler) {
      document.removeEventListener('keydown', overlay.querySelector('.invoke-modal')._escHandler);
    }
    overlay.classList.remove('show');
    setTimeout(() => overlay.remove(), 200);
  }
  _invokeModalOpen = false;
}

function _filterInvokeModal(query) {
  const q = query.toLowerCase();
  const items = document.querySelectorAll('#invoke-modal-overlay .invoke-item, #invoke-modal-overlay .invoke-dept-header');
  for (const item of items) {
    const s = item.dataset.search || '';
    item.style.display = (!q || s.includes(q)) ? '' : 'none';
  }
  // Show/hide sections based on whether they have visible items
  const sections = document.querySelectorAll('#invoke-modal-overlay .invoke-section');
  for (const sec of sections) {
    const visibleItems = sec.querySelectorAll('.invoke-item:not([style*="display: none"]), .invoke-dept-header:not([style*="display: none"])');
    const emptyMsg = sec.querySelector('.invoke-empty');
    // Don't hide sections, just let items filter
  }
}

// ═══════════════════════════════════════════════════════════════════════
// DEPARTMENT TREE HELPERS
// ═══════════════════════════════════════════════════════════════════════

function _getDeptTree() {
  if (typeof FOLDER_SUPERSET !== 'object' || !FOLDER_SUPERSET) return [];
  const tree = (typeof getFolderTree === 'function') ? getFolderTree() : null;
  if (!tree || !tree.rootChildren || !tree.rootChildren.length) return [];
  return tree.rootChildren.map(rc => {
    const fid = typeof rc === 'string' ? rc : rc.id;
    return _buildDeptData(tree, fid);
  }).filter(Boolean);
}

function _buildDeptData(tree, fid) {
  const folder = tree.folders[fid];
  if (!folder) return null;
  const def = FOLDER_SUPERSET[fid];
  const label = def ? (def.skill ? def.skill.label : def.name) : fid;
  const children = (folder.children || []).map(ck => {
    const cid = typeof ck === 'string' ? ck : ck.id;
    return _buildDeptData(tree, cid);
  }).filter(Boolean);
  return {
    id: fid,
    name: label,
    systemPrompt: def && def.skill ? def.skill.systemPrompt : '',
    children: children,
    isLeaf: children.length === 0,
  };
}

function _buildInvokeDeptNode(node, depth) {
  if (!node) return '';
  const indent = depth * 16;
  const searchName = (node.name || '').toLowerCase();
  let h = '';

  if (node.children && node.children.length) {
    // Department with children — collapsible
    const toggleId = 'invoke-dept-' + node.id;
    h += '<div class="invoke-dept-header" data-search="' + escHtml(searchName) + '" style="padding-left:' + indent + 'px;" onclick="_toggleInvokeDept(\'' + toggleId + '\', this)">';
    h += '<svg class="invoke-dept-chevron" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polyline points="9 18 15 12 9 6"/></svg>';
    h += '<span>' + escHtml(node.name) + '</span>';
    if (node.systemPrompt) {
      const idx = _invokeRegistry.length;
      _invokeRegistry.push({id:node.id, name:node.name, systemPrompt:node.systemPrompt, path:'', source:'department'});
      h += '<button class="invoke-dept-use" onclick="event.stopPropagation();_selectInvokeByIdx(' + idx + ')" title="Invoke this department">Use</button>';
    }
    h += '</div>';
    h += '<div class="invoke-dept-children collapsed" id="' + toggleId + '">';
    for (const child of node.children) {
      h += _buildInvokeDeptNode(child, depth + 1);
    }
    h += '</div>';
  } else {
    // Leaf — clickable
    const idx = _invokeRegistry.length;
    _invokeRegistry.push({id:node.id, name:node.name, systemPrompt:node.systemPrompt||'', path:'', source:'department'});
    h += '<div class="invoke-item" data-search="' + escHtml(searchName) + '" style="padding-left:' + indent + 'px;" onclick="_selectInvokeByIdx(' + idx + ')">';
    h += '<div class="invoke-item-icon"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="7" r="4"/><path d="M5.8 21a7 7 0 0 1 12.4 0"/></svg></div>';
    h += '<div class="invoke-item-info"><div class="invoke-item-name">' + escHtml(node.name) + '</div></div>';
    h += '</div>';
  }
  return h;
}

function _toggleInvokeDept(id, header) {
  const el = document.getElementById(id);
  if (!el) return;
  const collapsed = el.classList.toggle('collapsed');
  const chev = header.querySelector('.invoke-dept-chevron');
  if (chev) chev.style.transform = collapsed ? '' : 'rotate(90deg)';
}

// ═══════════════════════════════════════════════════════════════════════
// SELECTION + VISUAL STATE
// ═══════════════════════════════════════════════════════════════════════

function _selectInvokeByIdx(idx) {
  const item = _invokeRegistry[idx];
  if (!item) return;
  _closeInvokeModal();
  _selectInvoke(item);
}

function _selectInvokeFromModal(item) {
  _closeInvokeModal();
  _selectInvoke(item);
}

function _selectInvoke(item) {
  window._pendingInvoke = item;
  _applyInvokeVisual();
  // Focus the textarea and trigger input event so voice.js updateIcon() shows the send button
  const ta = document.getElementById('live-input-ta') || document.getElementById('live-queue-ta');
  if (ta) {
    ta.focus();
    ta.dispatchEvent(new Event('input', {bubbles: true}));
  }
}

function _applyInvokeVisual() {
  const invoke = window._pendingInvoke;
  if (!invoke) { _removeInvokeVisual(); return; }

  // Find the textarea wrapper area
  const ta = document.getElementById('live-input-ta') || document.getElementById('live-queue-ta');
  if (!ta) return;

  // Add gradient class to textarea
  ta.classList.add('invoke-active');

  // Add floating label if not present
  let label = document.getElementById('invoke-float-label');
  if (!label) {
    label = document.createElement('div');
    label.id = 'invoke-float-label';
    label.className = 'invoke-float-label';
    ta.parentElement.insertBefore(label, ta);
  }
  label.innerHTML =
    '<span class="invoke-float-name">Invoke ' + escHtml(invoke.name) + '</span>' +
    '<button class="invoke-float-cancel" onclick="_cancelInvoke()" title="Cancel invoke">&times;</button>';
}

function _removeInvokeVisual() {
  const ta = document.getElementById('live-input-ta') || document.getElementById('live-queue-ta');
  if (ta) ta.classList.remove('invoke-active');
  const label = document.getElementById('invoke-float-label');
  if (label) label.remove();
}

function _cancelInvoke() {
  window._pendingInvoke = null;
  _removeInvokeVisual();
  // Trigger input event so voice.js updateIcon() re-evaluates send button visibility
  const ta = document.getElementById('live-input-ta') || document.getElementById('live-queue-ta');
  if (ta) ta.dispatchEvent(new Event('input', {bubbles: true}));
}

// ═══════════════════════════════════════════════════════════════════════
// MESSAGE WRAPPING — wrap invoke content into [[invoke]]...[[/invoke]]
// ═══════════════════════════════════════════════════════════════════════

/**
 * If a pending invoke is set, wrap the user's text with the invoke block.
 * Returns the final text to send. Clears _pendingInvoke.
 */
function _wrapInvokeMessage(userText) {
  const invoke = window._pendingInvoke;
  if (!invoke) return userText;

  window._pendingInvoke = null;
  _removeInvokeVisual();

  const block = '[[invoke::' + invoke.name + '::path=' + (invoke.path || '') + ']]\n' +
    invoke.systemPrompt + '\n[[/invoke]]';

  return userText ? block + '\n\n' + userText : block;
}

/**
 * Build a system prompt notice for the invoked skill.
 * Returns a short string to prepend to the session system prompt.
 */
function _buildInvokeNotice() {
  const invoke = window._pendingInvoke;
  if (!invoke) return '';
  let notice = '\n\nThe user has invoked a workforce skill: "' + invoke.name + '"';
  if (invoke.path) notice += ' (from ' + invoke.path + ')';
  notice += '. The skill instructions are included in the user\'s message wrapped in [[invoke]]...[[/invoke]] tags. Follow those instructions for this request.';
  return notice;
}

// ═══════════════════════════════════════════════════════════════════════
// PILL RENDERING — detect [[invoke]] blocks in messages and render pills
// ═══════════════════════════════════════════════════════════════════════

const _INVOKE_RE = /\[\[invoke::(.+?)::path=([^\]]*)\]\][\s\S]*?\[\[\/invoke\]\]/g;

function _renderInvokePills(text) {
  // Returns { html: string, remainder: string }
  // html = pill HTML for the invoke block
  // remainder = user text after the invoke block
  const match = _INVOKE_RE.exec(text);
  _INVOKE_RE.lastIndex = 0; // reset regex state
  if (!match) return null;

  const name = match[1];
  const before = text.slice(0, match.index).trim();
  const after = text.slice(match.index + match[0].length).trim();

  return {
    pillHtml: '<span class="invoke-pill">' + escHtml(name) + '</span>',
    remainder: (before ? before + '\n' : '') + after,
  };
}

// ═══════════════════════════════════════════════════════════════════════
// NAVIGATE TO WORKFORCE
// ═══════════════════════════════════════════════════════════════════════

function _goToWorkforce() {
  _closeInvokeModal();
  if (typeof setViewMode === 'function') {
    setViewMode('workplace');
  }
}
