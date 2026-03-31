/* voice.js — Web Speech API voice input for textareas */

const _micSvg = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="9" y="1" width="6" height="12" rx="3"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="23" x2="12" y2="19"/></svg>';
const _micActiveSvg = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--result-err)" stroke-width="2" stroke-linecap="round"><rect x="9" y="1" width="6" height="12" rx="3"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="23" x2="12" y2="19"/></svg>';
const _sendSvg = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>';

let _activeRecognition = null;

/** Stop any active voice recognition cleanly (called on session switch, etc.) */
function _stopActiveVoice() {
  if (_activeRecognition) {
    _activeRecognition._intentionalStop = true;
    try { _activeRecognition.stop(); } catch (_) {}
    _activeRecognition = null;
  }
}

function _hasVoiceSupport() {
  return !!(window.SpeechRecognition || window.webkitSpeechRecognition);
}

function setupVoiceButton(textarea, button, onSubmit) {
  if (!textarea || !button) return;

  const updateIcon = () => {
    const hasText = textarea.value.trim().length > 0;
    if (_activeRecognition && _activeRecognition._target === textarea) {
      button.innerHTML = _micActiveSvg;
      button.title = 'Stop recording';
      button.classList.add('recording');
    } else if (hasText) {
      button.innerHTML = _sendSvg;
      button.title = 'Send (Ctrl+Enter)';
      button.classList.remove('recording');
    } else if (_hasVoiceSupport()) {
      button.innerHTML = _micSvg;
      button.title = 'Voice input';
      button.classList.remove('recording');
    } else {
      button.innerHTML = _sendSvg;
      button.title = 'Send (Ctrl+Enter)';
      button.classList.remove('recording');
    }
  };

  textarea.addEventListener('input', updateIcon);
  updateIcon();

  button.onclick = () => {
    const hasText = textarea.value.trim().length > 0;

    if (_activeRecognition && _activeRecognition._target === textarea) {
      // Stop recording (manual click)
      _activeRecognition._intentionalStop = true;
      _activeRecognition.stop();
      _activeRecognition = null;
      updateIcon();
      return;
    }

    if (hasText) {
      // Send
      if (onSubmit) onSubmit();
      return;
    }

    // Start voice input
    if (!_hasVoiceSupport()) {
      showToast('Voice input not supported in this browser', true);
      return;
    }

    // Kill any stale recognition from a previous textarea (e.g. bar rebuild)
    _stopActiveVoice();

    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    const recognition = new SR();
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = 'en-US';
    recognition._target = textarea;
    recognition._intentionalStop = false;
    _activeRecognition = recognition;

    let finalTranscript = '';
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
      textarea.value = finalTranscript;
      textarea.dispatchEvent(new Event('input'));
      updateIcon();
      if (finalTranscript.trim() && onSubmit) {
        onSubmit();
      } else {
        textarea.focus();
      }
      // Apply any bar updates that were deferred while voice was active
      if (typeof updateLiveInputBar === 'function') setTimeout(updateLiveInputBar, 0);
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
