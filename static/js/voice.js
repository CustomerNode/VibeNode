/* voice.js — Web Speech API voice input for textareas */

const _micSvg = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="9" y="1" width="6" height="12" rx="3"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="23" x2="12" y2="19"/></svg>';
const _micActiveSvg = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--result-err)" stroke-width="2" stroke-linecap="round"><rect x="9" y="1" width="6" height="12" rx="3"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="23" x2="12" y2="19"/></svg>';
const _sendSvg = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>';

let _activeRecognition = null;
let _activeSpeechNode = null;   // active SpeechNode (MediaRecorder) capture controller
let _snHiddenControls = null;   // bar buttons hidden while the premium panel owns the controls
let _snSharedAudioCtx = null;   // ONE reused AudioContext (creating/closing per capture is flaky)
// Flag set true when the most recent submit was triggered by voice transcription.
// Consumed (read + cleared) by the submit path to tag the message for AI context.
let _lastSubmitWasVoice = false;

/* ------------------------------------------------------------------ *
 * Voice transcript post-processing pipeline
 * ------------------------------------------------------------------ *
 * The raw output of the Web Speech API used to flow straight into the
 * textarea: `textarea.value = finalTranscript`. Because the submit path
 * reads `textarea.value`, the raw recognition string was ALSO exactly
 * what got sent to the backend — recognition, display, and payload were
 * all the same value with no seam to intervene at.
 *
 * This pipeline introduces that seam. The final transcript is routed
 * through an ordered chain of processor functions before it lands in the
 * textarea (and is therefore submitted). Each processor receives the
 * current text plus a context object and returns the next text (sync or
 * async). This is the landing spot for future phases — punctuation/format
 * cleanup, voice-command parsing, LLM rewrite, etc.
 *
 * Scaffolding only: the chain ships EMPTY, so the pipeline is a pure
 * identity pass today and there is no behavior change. Future phases add
 * stages via `registerVoiceTranscriptProcessor()`.
 *
 * Processor contract:
 *   fn(text: string, context: object) -> string | Promise<string>
 *   - Return the transformed text. Returning a non-string is ignored
 *     (the prior text is kept), so a processor can no-op safely.
 *   - Throwing is caught and the prior text is preserved — a broken
 *     processor must never destroy the user's dictated message.
 *   context currently carries: { textarea, onSubmit, source }.
 */
const _voiceTranscriptProcessors = [];

/**
 * Register a post-processing stage for finalized voice transcripts.
 * Stages run in registration order, each fed the previous stage's output.
 * @param {(text: string, context: object) => (string|Promise<string>)} fn
 */
function registerVoiceTranscriptProcessor(fn) {
  if (typeof fn === 'function') _voiceTranscriptProcessors.push(fn);
}

/**
 * Run a finalized transcript through the processor chain.
 * Always returns a Promise<string>. With no processors registered it
 * resolves to the input unchanged. A processor that throws or returns a
 * non-string is skipped so the dictated text is never lost.
 */
async function _processVoiceTranscript(rawTranscript, context = {}) {
  let text = rawTranscript;
  for (const processor of _voiceTranscriptProcessors) {
    try {
      const result = await processor(text, context);
      if (typeof result === 'string') text = result;
    } catch (err) {
      console.warn('[voice] transcript processor failed; keeping prior text', err);
    }
  }
  return text;
}

/** Stop any active voice capture cleanly (called on session switch, etc.) */
function _stopActiveVoice() {
  if (_activeRecognition) {
    _activeRecognition._intentionalStop = true;
    try { _activeRecognition.stop(); } catch (_) {}
    _activeRecognition = null;
  }
  if (_activeSpeechNode) {
    _activeSpeechNode._userCancelled = true;   // context change (session switch / new capture) -> discard
    try { _activeSpeechNode.cancel(); } catch (_) {}
    _activeSpeechNode = null;
  }
}

/**
 * Which voice engine to use right now.
 * SpeechNode (cross-browser, codebase-aware) wins when ready — this is what
 * lets Firefox/Safari users dictate at all. Otherwise fall back to the
 * Chromium-only Web Speech API, then to "no voice" (button acts as send).
 */
function _voiceMode() {
  if (window.SpeechNode && window.SpeechNode.isReady && window.SpeechNode.isReady()) return 'speechnode';
  if (window.SpeechRecognition || window.webkitSpeechRecognition) return 'webspeech';
  return 'none';
}

function _hasVoiceSupport() {
  return _voiceMode() !== 'none';
}

function setupVoiceButton(textarea, button, onSubmit) {
  if (!textarea || !button) return;

  // Create a separate send button next to the voice button
  let sendBtn = button.nextElementSibling;
  if (!sendBtn || !sendBtn.classList.contains('live-send-btn-send')) {
    sendBtn = document.createElement('button');
    sendBtn.className = 'live-send-btn live-send-btn-send';
    // Carry over the waiting class if the voice button has it
    if (button.classList.contains('waiting')) sendBtn.classList.add('waiting');
    sendBtn.innerHTML = _sendSvg;
    sendBtn.title = 'Send (' + _MOD + '+Enter)';
    sendBtn.style.display = 'none';
    button.parentNode.insertBefore(sendBtn, button.nextSibling);
  }

  // Create a cancel button (X) that appears to the LEFT of the mic button during recording
  let cancelBtn = button.previousElementSibling;
  if (!cancelBtn || !cancelBtn.classList.contains('voice-cancel-btn')) {
    cancelBtn = document.createElement('button');
    cancelBtn.className = 'voice-cancel-btn';
    cancelBtn.innerHTML = '&times;';
    cancelBtn.title = 'Cancel recording & discard';
    cancelBtn.style.display = 'none';
    button.parentNode.insertBefore(cancelBtn, button);
  }

  cancelBtn.onclick = (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (_activeRecognition && _activeRecognition._target === textarea) {
      _activeRecognition._intentionalStop = true;
      _activeRecognition._discarded = true;   // flag so onend knows to discard
      try { _activeRecognition.stop(); } catch (_) {}
      _activeRecognition = null;
    }
    if (_activeSpeechNode && _activeSpeechNode._target === textarea) {
      _activeSpeechNode._userCancelled = true;
      try { _activeSpeechNode.cancel(); } catch (_) {}
      _activeSpeechNode = null;
    }
    // Clear the textarea entirely (discard composed message)
    textarea.value = '';
    textarea.dispatchEvent(new Event('input'));
    textarea.focus();
    updateIcon();
  };

  sendBtn.onclick = () => {
    if (onSubmit) onSubmit();
  };

  const updateIcon = () => {
    const hasText = textarea.value.trim().length > 0 || !!window._pendingInvoke;
    const isRecording = (_activeRecognition && _activeRecognition._target === textarea)
      || (_activeSpeechNode && _activeSpeechNode._target === textarea);

    if (isRecording) {
      button.innerHTML = _micActiveSvg;
      button.title = 'Stop recording';
      button.classList.add('recording');
      sendBtn.style.display = 'none';
      cancelBtn.style.display = '';
    } else if (_hasVoiceSupport()) {
      button.innerHTML = _micSvg;
      button.title = 'Voice input';
      button.classList.remove('recording');
      sendBtn.style.display = hasText ? '' : 'none';
      cancelBtn.style.display = 'none';
    } else {
      // No voice support — button acts as send
      button.innerHTML = _sendSvg;
      button.title = 'Send (' + _MOD + '+Enter)';
      button.classList.remove('recording');
      sendBtn.style.display = 'none';
      cancelBtn.style.display = 'none';
    }
  };

  textarea.addEventListener('input', updateIcon);
  updateIcon();

  button.onclick = () => {
    // Stop an active SpeechNode capture (manual click while recording)
    if (_activeSpeechNode && _activeSpeechNode._target === textarea) {
      try { _activeSpeechNode.stop(); } catch (_) {}
      return;
    }
    if (_activeRecognition && _activeRecognition._target === textarea) {
      // Stop recording (manual click)
      _activeRecognition._intentionalStop = true;
      _activeRecognition.stop();
      _activeRecognition = null;
      updateIcon();
      return;
    }

    const _mode = _voiceMode();

    // No voice support fallback — act as send button
    if (_mode === 'none') {
      if (textarea.value.trim().length > 0 && onSubmit) onSubmit();
      return;
    }

    // Start voice input — kill any stale capture from a previous textarea (e.g. bar rebuild)
    _stopActiveVoice();

    // SpeechNode (cross-browser, codebase-aware) path — works in every browser.
    if (_mode === 'speechnode') {
      _startSpeechNodeCapture(textarea, button, onSubmit, updateIcon);
      return;
    }

    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    const recognition = new SR();
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = 'en-US';
    recognition._target = textarea;
    recognition._intentionalStop = false;
    _activeRecognition = recognition;

    // Preserve any existing text so voice appends to it
    const existingText = textarea.value;
    let finalTranscript = existingText ? existingText + ' ' : '';
    let silenceTimer = null;
    let restartCount = 0;
    const MAX_RESTARTS = 5;

    const resetSilenceTimer = () => {
      if (silenceTimer) clearTimeout(silenceTimer);
      silenceTimer = setTimeout(() => {
        recognition._intentionalStop = true;
        recognition.stop();
      }, 3000);  // 3s silence timeout — do NOT increase, causes premature cutoff feel
    };

    recognition.onresult = (e) => {
      restartCount = 0;
      let interim = '';
      for (let i = e.resultIndex; i < e.results.length; i++) {
        if (e.results[i].isFinal) {
          finalTranscript += e.results[i][0].transcript;
        } else {
          interim += e.results[i][0].transcript;
        }
      }
      // Live, in-progress feedback shows the RAW transcript intentionally —
      // post-processing runs once on the finalized transcript in onend (see
      // the _processVoiceTranscript pipeline), not on every interim tick.
      textarea.value = finalTranscript + interim;
      textarea.dispatchEvent(new Event('input'));
      resetSilenceTimer();
    };

    recognition.onend = () => {
      if (silenceTimer) clearTimeout(silenceTimer);

      // If the browser killed recognition unexpectedly (not silence / not manual),
      // try to seamlessly restart and keep recording.
      if (!recognition._intentionalStop && restartCount < MAX_RESTARTS && _activeRecognition === recognition) {
        restartCount++;
        try { recognition.start(); return; } catch (_) { /* fall through to normal end */ }
      }

      if (_activeRecognition === recognition) _activeRecognition = null;
      // If the user hit the cancel (X) button, discard everything
      if (recognition._discarded) {
        textarea.value = '';
        textarea.dispatchEvent(new Event('input'));
        updateIcon();
        textarea.focus();
        return;
      }
      // Commit the (post-processed) transcript into the textarea and, if
      // non-empty, submit it. Factored out so it can run either inline
      // (no processors) or as the pipeline's continuation (processors
      // registered). `text` is the value AFTER post-processing.
      const commitTranscript = (text) => {
        textarea.value = text;
        textarea.dispatchEvent(new Event('input'));
        updateIcon();
        if (text.trim() && onSubmit) {
          _lastSubmitWasVoice = true;
          onSubmit();
        } else {
          textarea.focus();
        }
        // Apply any bar updates that were deferred while voice was active.
        // Force liveBarState=null so the re-render isn't skipped by the
        // stateKey===liveBarState guard — the bar HTML is stale because
        // updateLiveInputBar returned early while we were recording.
        if (typeof updateLiveInputBar === 'function') {
          if (typeof liveBarState !== 'undefined') liveBarState = null;
          setTimeout(updateLiveInputBar, 0);
        }
      };

      // Decouple raw recognition from the committed/submitted value: the
      // final transcript passes through the post-processing pipeline first.
      // Fast path — with no processors registered, commit synchronously so
      // behavior is byte-for-byte identical to before this seam existed.
      if (_voiceTranscriptProcessors.length === 0) {
        commitTranscript(finalTranscript);
      } else {
        _processVoiceTranscript(finalTranscript, { textarea, onSubmit, source: 'voice' })
          .then(commitTranscript);
      }
    };

    recognition.onerror = (e) => {
      // Transient errors — let onend handle restart
      const transient = ['network', 'aborted', 'audio-capture'];
      if (transient.includes(e.error)) return;
      if (e.error !== 'no-speech') showToast('Voice error: ' + e.error, true);
      recognition._intentionalStop = true;
      if (_activeRecognition === recognition) _activeRecognition = null;
      updateIcon();
    };

    recognition.start();
    updateIcon();
    showToast('Listening...');
  };
}

/**
 * SpeechNode capture path — used when SpeechNode is enabled & ready.
 * Records via MediaRecorder (works in EVERY browser, so Firefox/Safari users
 * finally get voice) and transcribes server-side with codebase-biased Whisper.
 * In Chromium it ALSO shows live Web Speech interim text for feedback, but the
 * authoritative transcript always comes from SpeechNode.
 */
function _startSpeechNodeCapture(textarea, button, onSubmit, updateIcon) {
  const existingText = textarea.value ? textarea.value.replace(/\s+$/, '') : '';
  // Bias SpeechNode toward the ACTIVE project's vocabulary: capture the current
  // project dir once (so partials + the final all learn from the same codebase).
  const snCwd = (typeof _currentProjectDir === 'function') ? (_currentProjectDir() || '') : '';
  let interimRecog = null;
  let stopped = false;

  const controller = {
    _target: textarea,
    _discarded: false,
    _recorder: null,
    _stream: null,
    stop() { this._finish(); },
    cancel() { this._discarded = true; this._finish(); },
    _finish() {
      if (stopped) return;
      stopped = true;
      if (this._silenceTimer) { clearTimeout(this._silenceTimer); this._silenceTimer = null; }
      if (this._finishFallback) { clearTimeout(this._finishFallback); this._finishFallback = null; }
      if (this._maxTimer) { clearTimeout(this._maxTimer); this._maxTimer = null; }
      if (this._levelTimer) { clearInterval(this._levelTimer); this._levelTimer = null; }
      if (this._streamKick) { clearTimeout(this._streamKick); this._streamKick = null; }
      try { if (this._audioSrc) this._audioSrc.disconnect(); } catch (_) {}
      try { button.style.boxShadow = ''; } catch (_) {}
      // Cancel -> discard the panel. Normal stop -> KEEP it through finalize so the
      // message never appears to vanish; onstop fades it out after the handoff.
      if (this._discarded) { try { _snCaptionHide(); } catch (_) {} }
      else {
        try { _snCaptionFinalize(); } catch (_) {}
        // Backstop: guarantee a send even if recorder.onstop NEVER fires (recorder error
        // / already-inactive). commitSend is idempotent, so this can't double-send.
        if (!this._sendBackstop && this._commitSend) {
          this._sendBackstop = setTimeout(() => { try { this._commitSend(''); } catch (_) {} }, 7000);
        }
      }
      if (interimRecog) { try { interimRecog._intentionalStop = true; interimRecog.stop(); } catch (_) {} }
      try { if (this._recorder && this._recorder.state !== 'inactive') this._recorder.stop(); } catch (_) {}
      try { if (this._stream) this._stream.getTracks().forEach((t) => t.stop()); } catch (_) {}
    },
  };

  navigator.mediaDevices.getUserMedia({ audio: true }).then((stream) => {
    if (stopped || controller._discarded) {
      try { stream.getTracks().forEach((t) => t.stop()); } catch (_) {}
      return;
    }
    controller._stream = stream;
    let recorder;
    try {
      recorder = new MediaRecorder(stream);
    } catch (e) {
      try { stream.getTracks().forEach((t) => t.stop()); } catch (_) {}
      _speechNodeFail(updateIcon, 'Recording is not supported in this browser.');
      return;
    }
    controller._recorder = recorder;

    // --- Silence-based auto-stop (cross-browser via Web Audio) ---
    // When you finish speaking, a short pause ends the turn: stop -> transcribe
    // -> auto-send. Works in EVERY browser (Firefox has no Web Speech VAD), and
    // mirrors the old Web Speech 3s silence behavior. Manual click still stops too.
    const SILENCE_SHORT = 2500;   // pause-to-send in a quiet room
    const SILENCE_LONG = 5000;    // pause-to-send when background noise/music is present
    const MAX_MS = 60000;         // hard cap on one recording
    const RMS_THRESHOLD = 0.015;  // absolute speech floor (a fast path for QUIET rooms)
    const QUIET_RMS = 0.02;       // background above this = "noisy" -> use the long window
    const SPEECH_FACTOR = 2.2;    // (reserved)
    const STABLE_MS = 3500;       // committed words unchanged this long -> end of speech (client fallback)
    const GAP_S = 2.5;            // Whisper-VAD trailing silence (real, noise-immune) -> end of speech
    let hasSpoken = false;
    let noiseFloor = 1;           // tracks background (min rms); starts high, drops to real floor
    let streamBusy = false;       // a partial transcription is in flight
    let streamCooldown = 0;
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (AudioCtx) {
      try {
        if (!_snSharedAudioCtx) _snSharedAudioCtx = new AudioCtx();
        const ctx = _snSharedAudioCtx;           // reuse one context (avoids per-capture churn)
        try { if (ctx.state === 'suspended') ctx.resume(); } catch (_) {}
        const src = ctx.createMediaStreamSource(stream);
        controller._audioSrc = src;              // disconnect on finish; never close the shared ctx
        const analyser = ctx.createAnalyser();
        analyser.fftSize = 1024;
        src.connect(analyser);                   // not to destination — no echo
        const buf = new Uint8Array(analyser.fftSize);
        // Finalize-and-send, but only once the model has caught up (compute gate):
        // if a partial is still transcribing, defer until it returns so a stall on
        // a long utterance can't trigger a premature send.
        const maybeFinish = () => {
          controller._silenceTimer = null;
          if (stopped || controller._discarded) return;
          controller._finishing = true;          // stop issuing new partials
          if (!streamBusy) { controller.stop(); return; }   // model idle -> send now
          // model busy: the in-flight partial's handler stops us when it returns,
          // but never hang on it — hard fallback so a stuck request can't trap us.
          controller._finishFallback = setTimeout(() => { try { controller.stop(); } catch (_) {} }, 1500);
        };
        controller._levelTimer = setInterval(() => {
          if (stopped) return;
          analyser.getByteTimeDomainData(buf);
          let sum = 0;
          for (let i = 0; i < buf.length; i++) { const v = (buf[i] - 128) / 128; sum += v * v; }
          const rms = Math.sqrt(sum / buf.length);
          // Honest live feedback: glow the mic with the actual mic level.
          try { button.style.boxShadow = '0 0 0 ' + (2 + Math.min(10, rms * 55)).toFixed(1) + 'px rgba(239,68,68,0.30)'; } catch (_) {}
          // Drive the premium caption's audio-reactive glow from the same level.
          try { const _cap = document.getElementById('sn-live-caption'); if (_cap) _cap.style.setProperty('--sn-level', Math.min(1, rms * 9).toFixed(2)); } catch (_) {}
          // Track the background-noise floor (quietest recent level) — used ONLY to
          // pick the pause window, NEVER to gate speech. Speech uses a fixed floor so
          // it can't mis-calibrate and refuse to ever end.
          if (rms < noiseFloor) noiseFloor = rms;
          else noiseFloor = noiseFloor * 0.995 + rms * 0.005;
          const silenceMs = (noiseFloor > QUIET_RMS) ? SILENCE_LONG : SILENCE_SHORT;
          if (rms > RMS_THRESHOLD) {
            hasSpoken = true;
            if (controller._silenceTimer) { clearTimeout(controller._silenceTimer); controller._silenceTimer = null; }
          } else if (hasSpoken && !controller._finishing && !controller._silenceTimer) {
            controller._silenceTimer = setTimeout(maybeFinish, silenceMs);
          }
        }, 100);
      } catch (_) { /* no silence detection available — manual stop still works */ }
    }
    controller._maxTimer = setTimeout(() => { controller.stop(); }, MAX_MS);

    const chunks = [];
    recorder.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };

    // --- Near-real-time streaming (chunked Whisper) ---
    // Re-transcribe the audio-so-far continuously and show the REAL SpeechNode
    // text building up as you talk. Single request in flight; it re-chains the
    // moment it returns, so it runs as fast as the machine allows. The ONLY
    // throttle is ADAPTIVE — it stays at full speed and backs off (and drops
    // partials to fast/greedy decode) only if a partial actually comes back slow.
    // The final pause-snap always uses the full-quality pass, so committed text
    // is unchanged.
    const _now = () => ((window.performance && performance.now) ? performance.now() : Date.now());

    // LocalAgreement stabilization: words that two consecutive partials agree on
    // get "committed" and never rewrite again — this is what kills the jumpiness.
    // Only the unstable tail keeps updating; the final snap replaces everything.
    let committedWords = [];
    let prevWords = [];
    const _normW = (w) => w.toLowerCase().replace(/[^a-z0-9']/g, '');
    const _commonPrefix = (a, b) => {
      let i = 0; const n = Math.min(a.length, b.length);
      while (i < n && _normW(a[i]) === _normW(b[i])) i++;
      return i;
    };
    const applyPartial = (text) => {
      const cur = text.trim().split(/\s+/).filter(Boolean);
      const agree = _commonPrefix(prevWords, cur);
      if (agree > committedWords.length) committedWords = cur.slice(0, agree);  // grow committed, never shrink
      prevWords = cur;
      controller._lastFullText = cur.join(' ');   // remember last good transcript for failure fallback
      // Premium live surface: render committed (solid) + settling tail (shimmer)
      // into the floating caption, NOT the textarea — so the input never looks janky.
      _snCaptionUpdate(committedWords, cur.slice(committedWords.length));
    };

    const _reschedule = (ms) => {
      if (stopped || controller._discarded || controller._finishing) return;
      controller._streamKick = setTimeout(pumpPartial, ms);
    };
    function pumpPartial() {
      if (stopped || controller._discarded) return;
      // Not ready yet (no audio captured, or a request still in flight): keep the
      // loop ALIVE by retrying soon, instead of dying on the first early bail.
      if (streamBusy || !chunks.length) { _reschedule(250); return; }
      streamBusy = true;
      const t0 = _now();
      // Partials are ALWAYS fast/greedy (beam=1) so they return in well under a second
      // and the live preview keeps up while you talk. If they still come back slow we
      // additionally back OFF the rate (below). The final pause-snap uses full quality,
      // so the committed message is never lower-quality. Full-quality partials were the
      // bug: the first one was too slow to land before a short utterance finalized.
      const useFast = true;
      const partial = new Blob(chunks.slice(), { type: (chunks[0] && chunks[0].type) || 'audio/webm' });
      window.SpeechNode.transcribeBlob(partial, { fast: useFast, cwd: snCwd }).then((res) => {
        streamBusy = false;
        const dur = _now() - t0;
        if (dur > 1200) streamCooldown = Math.min(1500, streamCooldown + 300);
        else streamCooldown = Math.max(0, streamCooldown - 200);
        if (stopped || controller._discarded) return;
        if (res && res.ok && typeof res.text === 'string') {
          if (res.text.trim()) applyPartial(res.text);
          const haveText = !!(controller._lastFullText || '').trim();
          // END-OF-SPEECH — noise-immune (mic level AND raw text both fail in loud rooms):
          // (1) Whisper-VAD trailing-silence gap from the backend = real silence since you
          //     last spoke; VAD ignores noise so it's solid. (needs the web restart)
          if (haveText && typeof res.gap === 'number' && res.gap >= GAP_S) controller._finishing = true;
          // (2) committed-words stability — the confidently-recognized prefix stops growing
          //     when you stop talking. Works client-side (just a hard refresh).
          if (committedWords.length === controller._lastCommitLen) {
            if (haveText && committedWords.length > 0 &&
                _now() - (controller._lastCommitTs || _now()) >= STABLE_MS) controller._finishing = true;
          } else {
            controller._lastCommitLen = committedWords.length;
            controller._lastCommitTs = _now();
          }
        } else if (res && !res.ok) {
          console.warn('[SpeechNode] partial transcribe not ok:', res.error);
        }
        if (controller._finishing) { controller.stop(); return; }  // end of speech -> finalize
        _reschedule(streamCooldown);
      }).catch((e) => {
        streamBusy = false;
        streamCooldown = Math.min(1500, streamCooldown + 300);
        console.warn('[SpeechNode] partial transcribe failed:', e);
        if (controller._finishing) { controller.stop(); return; }
        _reschedule(streamCooldown);
      });
    }
    controller._streamKick = setTimeout(pumpPartial, 400);

    // Bulletproof send: NEVER drop a message. Idempotent (sends exactly once), and
    // gated ONLY by an explicit ✕ cancel. Empty/failed/slow transcribes fall back to
    // the best live transcript. (Pre-emptively sending is fine; dropping is not.)
    const commitSend = (text) => {
      if (controller._sent) return;
      controller._sent = true;
      if (controller._sendBackstop) { clearTimeout(controller._sendBackstop); controller._sendBackstop = null; }
      if (_activeSpeechNode === controller) _activeSpeechNode = null;
      try { button.classList.remove('processing'); } catch (_) {}
      if (controller._userCancelled) {            // the ONLY thing that discards a message
        try { _snCaptionHide(); } catch (_) {}
        updateIcon();
        _refreshBarSoon();
        return;
      }
      let t = (text || '').trim();
      if (!t) t = (controller._lastFullText || '').trim();   // best available transcript
      const joined = (existingText ? (existingText + ' ' + t) : t).trim();
      if (joined) {
        try { _snCaptionSetFinal(t || joined); } catch (_) {}
        try { _snCaptionSend(); } catch (_) {}    // lift + fade as the message lands
        // The input bar can re-render mid-capture (e.g. the session you're dictating to
        // keeps streaming), swapping the textarea node. onSubmit re-queries the input by
        // id, so write the message into the CURRENT live element — not the stale node we
        // captured at start. (Writing to the detached node = "send motion plays, then it
        // vanishes" — the message was put somewhere onSubmit never reads.)
        const liveTa = (textarea.id && document.getElementById(textarea.id)) || textarea;
        liveTa.value = joined;
        try { liveTa.dispatchEvent(new Event('input')); } catch (_) {}
        if (liveTa !== textarea) { try { textarea.value = joined; } catch (_) {} }
        updateIcon();
        _lastSubmitWasVoice = true;
        // If onSubmit throws, the text is already sitting in the live input -> recoverable,
        // not lost. Tell the user so a failed auto-send never silently swallows a message.
        try {
          if (onSubmit) onSubmit();
        } catch (e) {
          console.warn('[SpeechNode] onSubmit threw; text left in the input box', e);
          try { if (typeof showToast === 'function') showToast('Your dictation is ready — press Enter to send.', true); } catch (_) {}
        }
      } else {                                     // model recognized nothing -> say so, don't vanish silently
        try { _snCaptionHide(); } catch (_) {}
        updateIcon();
        try { if (typeof showToast === 'function') showToast("Didn't catch that — try again.", true); } catch (_) {}
        textarea.focus();
      }
      setTimeout(() => { try { _snCaptionHide(); } catch (_) {} }, 340);
      _refreshBarSoon();
    };
    controller._commitSend = commitSend;   // let the finalize-time backstop reach it

    recorder.onstop = () => {
      try { if (controller._stream) controller._stream.getTracks().forEach((t) => t.stop()); } catch (_) {}
      if (controller._userCancelled) { commitSend(''); return; }   // ✕ -> discard (inside commitSend)
      const blob = new Blob(chunks, { type: (chunks[0] && chunks[0].type) || 'audio/webm' });
      try { button.classList.add('processing'); button.title = 'Transcribing…'; } catch (_) {}
      // Watchdog: if the final transcribe is slow or dies, still send the best live
      // transcript. commitSend is idempotent, so this can never double-send.
      const watchdog = setTimeout(() => { commitSend(''); }, 6000);
      window.SpeechNode.transcribeBlob(blob, { cwd: snCwd }).then((res) => {
        clearTimeout(watchdog);
        if (res && res.ok && typeof res.text === 'string' && res.text.trim()) {
          _processVoiceTranscript(res.text, { textarea, onSubmit, source: 'speechnode' })
            .then((t) => commitSend(t || res.text))
            .catch(() => commitSend(res.text));
        } else {
          commitSend('');     // empty/failed final -> fall back to the live transcript
        }
      }).catch((err) => {
        clearTimeout(watchdog);
        console.warn('[SpeechNode] final transcribe failed:', err);
        commitSend('');       // salvage from the live transcript -> never drop
      });
    };

    // NO fake live preview. We deliberately do NOT paint Web Speech (Google's
    // recognizer) text while recording — it's a DIFFERENT, worse engine than
    // SpeechNode, so it looked broken mid-speech and then snapped to the real
    // result. Honest feedback only: the mic glows with your live audio level
    // (above), then the real SpeechNode transcript appears on pause.
    interimRecog = null;

    recorder.start(400);   // emit a chunk every 400ms so streaming has fresh audio
    _activeSpeechNode = controller;
    updateIcon();
    try { _snCaptionShow(textarea); } catch (_) {}
    // Hand the controls to the premium panel: hide the bar's mic/cancel buttons
    // (visibility, not display — so the bar doesn't reflow and shift the panel).
    try {
      _snHiddenControls = [];
      _snHiddenControls.push([button, button.style.visibility || '']);
      button.style.visibility = 'hidden';
      const _xb = button.parentElement && button.parentElement.querySelector('.voice-cancel-btn');
      if (_xb) { _snHiddenControls.push([_xb, _xb.style.visibility || '']); _xb.style.visibility = 'hidden'; }
    } catch (_) {}
    if (typeof showToast === 'function') showToast('Listening…');
  }).catch((err) => {
    _speechNodeFail(updateIcon,
      (err && err.name === 'NotAllowedError') ? 'Microphone permission denied.' : 'Could not access the microphone.');
  });
}

function _speechNodeFail(updateIcon, msg) {
  _activeSpeechNode = null;
  if (typeof showToast === 'function') showToast('SpeechNode: ' + msg, true);
  if (typeof updateIcon === 'function') { try { updateIcon(); } catch (_) {} }
}

function _refreshBarSoon() {
  if (typeof updateLiveInputBar === 'function') {
    if (typeof liveBarState !== 'undefined') liveBarState = null;
    setTimeout(updateLiveInputBar, 0);
  }
}

/* ------------------------------------------------------------------ *
 * SpeechNode Live caption — the premium streaming surface.
 * A floating glassy pill anchored above the active input. Committed words
 * (locked by LocalAgreement) render solid; the settling tail shimmers, so the
 * chunked stream reads as an intentional "resolving" effect instead of a janky
 * textarea. The textarea is left untouched until the final transcript lands.
 * ------------------------------------------------------------------ */
function _snEsc(s) {
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function _snCaptionEl() {
  let el = document.getElementById('sn-live-caption');
  if (!el) {
    el = document.createElement('div');
    el.id = 'sn-live-caption';
    el.className = 'sn-live-caption';
    el.innerHTML =
      '<span class="sn-cap-eq"><i></i><i></i><i></i><i></i></span>' +
      '<span class="sn-cap-spinner" aria-hidden="true"></span>' +
      '<span class="sn-cap-text"></span>' +
      '<button class="sn-cap-btn sn-cap-cancel" type="button" title="Cancel (discard)" aria-label="Cancel">' +
        '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/></svg>' +
      '</button>' +
      '<button class="sn-cap-btn sn-cap-stop" type="button" title="Stop & send" aria-label="Stop and send">' +
        '<svg width="13" height="13" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="3" fill="currentColor"/></svg>' +
      '</button>';
    el.querySelector('.sn-cap-stop').onclick = (e) => {
      e.preventDefault(); e.stopPropagation();
      if (_activeSpeechNode) { try { _activeSpeechNode.stop(); } catch (_) {} }
    };
    el.querySelector('.sn-cap-cancel').onclick = (e) => {
      e.preventDefault(); e.stopPropagation();
      const c = _activeSpeechNode;
      if (c) {
        c._userCancelled = true;        // explicit cancel — the ONLY thing that drops a message
        c._discarded = true;
        try { c.cancel(); } catch (_) {}
      }
      _activeSpeechNode = null;
      _snCaptionHide();
    };
    document.body.appendChild(el);
  }
  return el;
}
// Animated "Listening…" placeholder using the SAME shimmer gradient as the settling
// tail, so the panel ALWAYS reads as alive — even when partials are empty/slow and no
// text has streamed yet. Without it, "no live text" looked like a dead/broken capture.
const _SN_LISTENING_HTML =
  '<span class="sn-cap-word sn-cap-tail sn-cap-listening" style="font-style:italic">Listening…</span>';
function _snCaptionShow(textarea) {
  const el = _snCaptionEl();
  el.classList.remove('finalizing', 'finalized', 'sn-cap-sending');
  el.querySelector('.sn-cap-text').innerHTML = _SN_LISTENING_HTML;
  try {
    const r = textarea.getBoundingClientRect();
    // Cover the textarea's box, but anchor to its BOTTOM so the panel grows UPWARD
    // as the transcript gets longer — instead of expanding off the bottom of the screen.
    el.style.left = Math.round(r.left) + 'px';
    el.style.width = Math.round(r.width) + 'px';
    el.style.minHeight = Math.round(r.height) + 'px';
    el.style.top = 'auto';
    el.style.bottom = Math.round(window.innerHeight - r.bottom) + 'px';
  } catch (_) {}
  document.body.classList.add('sn-live');   // premium recording controls
  requestAnimationFrame(() => el.classList.add('show'));
}
function _snCaptionUpdate(committed, tail) {
  const el = document.getElementById('sn-live-caption');
  if (!el || !el.classList.contains('show')) return;
  const textEl = el.querySelector('.sn-cap-text');
  const committedN = (committed || []).length;
  const words = (committed || []).concat(tail || []);
  if (words.length === 0) {   // nothing recognized yet -> keep the shimmering "Listening…"
    if (!textEl.querySelector('.sn-cap-listening')) textEl.innerHTML = _SN_LISTENING_HTML;
    return;
  }
  if (textEl.firstElementChild && textEl.firstElementChild.classList.contains('sn-cap-listening')) {
    textEl.innerHTML = '';    // leaving the placeholder -> render real words clean
  }
  // Reconcile per word: only re-render words that actually changed, and glow-
  // highlight those so a retroactive correction reads as a deliberate refinement.
  for (let i = 0; i < words.length; i++) {
    let span = textEl.children[i];
    if (!span) {
      span = document.createElement('span');
      span.className = 'sn-cap-word';
      textEl.appendChild(span);
    }
    if (span.dataset.w !== words[i]) {
      const wasShown = span.dataset.w !== undefined;   // an existing word being corrected?
      span.dataset.w = words[i];
      span.textContent = words[i] + ' ';
      if (wasShown) {   // glow only TRUE retroactive changes, not freshly-appended words
        span.classList.remove('sn-word-anim');
        void span.offsetWidth;               // restart the highlight animation
        span.classList.add('sn-word-anim');
      }
    }
    const isCommitted = i < committedN;
    span.classList.toggle('sn-cap-committed', isCommitted);
    span.classList.toggle('sn-cap-tail', !isCommitted);
  }
  while (textEl.children.length > words.length) textEl.removeChild(textEl.lastChild);
}
function _snCaptionFinalize() {
  const el = document.getElementById('sn-live-caption');
  if (!el) return;
  el.classList.add('finalizing');   // collapses the equalizer; keeps the text visible
}
function _snCaptionSetFinal(text) {
  const el = document.getElementById('sn-live-caption');
  if (!el || !el.classList.contains('show')) return;
  el.classList.remove('finalizing');
  const textEl = el.querySelector('.sn-cap-text');
  if (textEl) textEl.innerHTML = '<span class="sn-cap-word sn-cap-committed">' + _snEsc(text) + '</span>';
  el.classList.add('finalized');    // brief polish pop, then the caller fades it out
}
function _snCaptionSend() {
  const el = document.getElementById('sn-live-caption');
  if (el) el.classList.add('sn-cap-sending');   // panel lifts + fades as the message lands in chat
}
function _snCaptionHide() {
  const el = document.getElementById('sn-live-caption');
  if (el) el.classList.remove('show', 'finalizing', 'finalized', 'sn-cap-sending');
  document.body.classList.remove('sn-live');
  // Restore the bar controls — but let the bar re-render replace them FIRST, so the
  // old voice button never flashes back in. Only restore if they're somehow still in
  // the DOM after a beat (a context that didn't re-render).
  if (_snHiddenControls) {
    const hidden = _snHiddenControls;
    _snHiddenControls = null;
    setTimeout(() => {
      hidden.forEach((p) => { try { if (document.body.contains(p[0])) p[0].style.visibility = p[1]; } catch (_) {} });
    }, 700);
  }
}
