/*
 * VoiceLocal: speech-to-text inside the browser, on the device itself.
 *
 * iPhone dictation cuts off mid-sentence: iOS Safari ends its speech
 * recognizer at a breath and refuses a silent
 * restart. SpeechNode (record on the phone, transcribe on the PC) avoids that,
 * but the PC may have no transcriber at all (no build for Windows on ARM, or
 * never installed). This engine fills that gap: the phone records, and Whisper
 * runs in the browser itself. Nothing leaves the device.
 *
 * Interface (the same shape voice.js uses for SpeechNode):
 *   VoiceLocal.canRun()                  this browser could run it (opt-out: localStorage vnVoiceLocal = 'off')
 *   VoiceLocal.isReady()                 the model is loaded
 *   VoiceLocal.load(onProgress)          loads the library and the model once; resolves when ready
 *   VoiceLocal.transcribeBlob(blob)      -> Promise<{ok, text, gap}>; gap is 0 (no timestamps), so
 *                                           the capture's own mic meter decides end of speech
 *   VoiceLocal.noPartials === true       voice.js skips the live-caption partial loop: a whole
 *                                           pass per partial is too slow on a phone
 *
 * The library (Hugging Face transformers.js) and the model (Whisper base,
 * English, 8-bit on the processor or fp32 on the graphics chip) come from the
 * public CDN and model hub on first use and are kept by the browser's cache,
 * about 80 MB once per device. Pin the versions: a moving CDN tag would change
 * what the phone runs with no change here.
 */
(function () {
  'use strict';
  const LIB_URL = 'https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.5.2';
  const MODEL_ID = 'onnx-community/whisper-base.en';

  let _pipe = null;        // the loaded pipeline
  let _loading = null;     // the in-flight load (so two taps share one download)
  let _device = '';        // 'webgpu' or 'wasm' once loaded

  function canRun() {
    try {
      if (typeof localStorage !== 'undefined' && localStorage.getItem('vnVoiceLocal') === 'off') return false;
      if (typeof WebAssembly === 'undefined' || typeof MediaRecorder === 'undefined') return false;
      if (!(window.AudioContext || window.webkitAudioContext)) return false;
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return false;
      return true;
    } catch (_) { return false; }
  }

  function isReady() { return !!_pipe; }

  async function _pickDevice() {
    try {
      if (!navigator.gpu) return 'wasm';
      const adapter = await navigator.gpu.requestAdapter();
      return adapter ? 'webgpu' : 'wasm';
    } catch (_) { return 'wasm'; }
  }

  function load(onProgress) {
    if (_pipe) return Promise.resolve(_pipe);
    if (_loading) return _loading;
    const report = (msg, frac) => { try { if (onProgress) onProgress(msg, frac); } catch (_) {} };
    _loading = (async () => {
      report('Loading the speech library…', 0.02);
      const lib = await import(/* webpackIgnore: true */ LIB_URL);
      const device = await _pickDevice();
      const opts = device === 'webgpu' ? { device: 'webgpu', dtype: 'fp32' } : { dtype: 'q8' };
      const seen = {};
      opts.progress_callback = (p) => {
        if (!p || p.status !== 'progress' || !p.file) return;
        seen[p.file] = [p.loaded || 0, p.total || 0];
        let l = 0, t = 0;
        Object.keys(seen).forEach((k) => { l += seen[k][0]; t += seen[k][1]; });
        if (t > 0) report('Downloading the speech model… ' + Math.round(100 * l / t) + '%', 0.05 + 0.85 * l / t);
      };
      const pipe = await lib.pipeline('automatic-speech-recognition', MODEL_ID, opts);
      _device = device;
      _pipe = pipe;
      report('Speech model ready.', 1);
      return pipe;
    })().catch((e) => { _loading = null; throw e; });
    return _loading;
  }

  // Decode whatever the recorder produced (mp4 on iOS, webm elsewhere) to 16 kHz mono floats.
  async function _toPcm16k(blob) {
    const AC = window.AudioContext || window.webkitAudioContext;
    const ctx = new AC();
    let buf;
    try {
      buf = await new Promise((res, rej) => {
        // Promise form is missing on older Safari; the callback form works everywhere.
        blob.arrayBuffer().then((ab) => { try { ctx.decodeAudioData(ab, res, rej); } catch (e) { rej(e); } }, rej);
      });
    } finally { try { ctx.close(); } catch (_) {} }
    const mono = buf.getChannelData(0);
    if (buf.sampleRate === 16000) return { audio: mono, duration: buf.duration };
    const off = new OfflineAudioContext(1, Math.ceil(buf.duration * 16000), 16000);
    const src = off.createBufferSource();
    src.buffer = buf; src.connect(off.destination); src.start();
    const out = await off.startRendering();
    return { audio: out.getChannelData(0), duration: buf.duration };
  }

  // One inference at a time. The model is a single session: a second call while
  // the first is still running throws "Session already started", and the session
  // can stay unusable afterwards ("Session mismatch"). A tap-stop-tap while the
  // model was still warming up did exactly that. Calls queue here instead.
  let _busy = Promise.resolve();

  async function _runOnce(blob) {
    const pipe = await load();
    const { audio, duration } = await _toPcm16k(blob);
    if (!audio || !audio.length) return { ok: true, text: '', gap: 0, duration: 0 };
    const r = await pipe(audio, { chunk_length_s: 30, stride_length_s: 5 });
    const text = (r && typeof r.text === 'string') ? r.text.trim() : '';
    return { ok: true, text, gap: 0, duration };
  }

  function transcribeBlob(blob) {
    const run = _busy.then(() => _runOnce(blob)).catch((e) => {
      const msg = String((e && e.message) || e);
      // A broken session never recovers on its own: drop it so the next
      // capture rebuilds the pipeline (the files come from the browser cache).
      if (/session/i.test(msg)) { _pipe = null; _loading = null; }
      return { ok: false, error: msg, text: '', gap: 0 };
    });
    _busy = run.then(() => undefined, () => undefined);
    return run;
  }

  window.VoiceLocal = { canRun, isReady, load, transcribeBlob, noPartials: true,
    get device() { return _device; }, MODEL_ID, LIB_URL };
})();
