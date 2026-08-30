/* image-attach.js — paste or pick an image in the composer.

   Two entry points, because they cover different platforms:

     1. paste  — desktop (Ctrl/Cmd+V) and iOS "Paste" in the composer. Reads
                 image blobs off the clipboard.
     2. button — an image button injected next to the voice button. On iOS this
                 opens Photo Library / Take Photo / Choose File, which is the
                 only reliable way to attach a photo from a phone.

   Both upload to /api/attach-image and then insert the saved absolute path into
   whichever composer textarea is live, so Claude can Read it.

   The composer bars are rebuilt via innerHTML in four different places
   (live-panel.js x2, app.js, kanban.js). Rather than edit each render path, a
   MutationObserver re-injects the button whenever a bar row appears without it.
   That keeps this feature entirely self-contained. */

(function () {
  'use strict';

  var MAX_BYTES = 20 * 1024 * 1024;

  // Bump on every behavioural change. Diagnostic toasts carry it so a bug
  // report identifies the running build instead of guessing about caching.
  var BUILD = 'b5';

  // Ship every clipboard diagnostic to the server too (logs/client_diag.log).
  // Mobile toasts get paraphrased or missed; the log is exact and readable
  // from the PC without relaying anything through the user.
  function diag(msg) {
    try {
      fetch('/api/client-log', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({tag: 'image-attach ' + BUILD, msg: msg})
      }).catch(function () {});
    } catch (e) { /* diagnostics must never break the feature */ }
  }

  function activeTextarea() {
    return document.getElementById('live-input-ta') ||
           document.getElementById('live-queue-ta');
  }

  function insertRef(ta, path) {
    if (!ta) return;
    var ref = 'Image: ' + path;
    var cur = ta.value || '';
    // Own line, and keep whatever the user already typed.
    ta.value = cur ? (cur.replace(/\s+$/, '') + '\n' + ref + '\n') : (ref + '\n');
    ta.dispatchEvent(new Event('input', {bubbles: true}));
    try {
      ta.focus();
      ta.setSelectionRange(ta.value.length, ta.value.length);
    } catch (e) { /* focus is best-effort */ }
  }

  function setBusy(on) {
    var btn = document.getElementById('live-image-btn');
    if (btn) {
      btn.classList.toggle('is-busy', !!on);
      btn.disabled = !!on;
    }
  }

  function toast(msg) {
    if (typeof window.showToast === 'function') window.showToast(msg);
    else if (typeof window.toast === 'function') window.toast(msg);
    else console.log('[image-attach] ' + msg);
  }

  async function upload(file) {
    if (!file) return;
    if (file.size > MAX_BYTES) {
      toast('Image is too large (max 20 MB)');
      return;
    }
    var ta = activeTextarea();
    if (!ta) {
      toast('Open a session first, then attach the image');
      return;
    }

    setBusy(true);
    try {
      var fd = new FormData();
      // Clipboard blobs often have no name; give the server an extension to check.
      var name = file.name || ('pasted-image.' + ((file.type || '').split('/')[1] || 'png'));
      fd.append('file', file, name);

      var res = await fetch('/api/attach-image', {method: 'POST', body: fd});
      var data = await res.json().catch(function () { return {}; });

      if (!res.ok || !data.path) {
        toast(data.error || 'Image upload failed');
        return;
      }
      insertRef(activeTextarea() || ta, data.path);
      toast('Image attached');
    } catch (e) {
      toast('Image upload failed: ' + e.message);
    } finally {
      setBusy(false);
    }
  }

  // --- 1. paste -------------------------------------------------------------

  document.addEventListener('paste', function (ev) {
    var t = ev.target;
    var inComposer = t && t.id &&
                     (t.id === 'live-input-ta' || t.id === 'live-queue-ta');
    if (!inComposer) return;

    var items = (ev.clipboardData && ev.clipboardData.items) || [];
    for (var i = 0; i < items.length; i++) {
      if (items[i].kind === 'file' && /^image\//.test(items[i].type)) {
        var f = items[i].getAsFile();
        if (f) {
          // Only swallow the event once we know we have an image, so pasting
          // text into the composer keeps working normally.
          ev.preventDefault();
          upload(f);
          return;
        }
      }
    }
  }, true);

  // --- 2. button ------------------------------------------------------------

  // --- 3. long-press chip ---------------------------------------------------
  //
  // iOS owns the native long-press edit menu and gives web pages no way to add
  // an item to it. So this adds a chip ALONGSIDE it rather than replacing it:
  // the touch is never preventDefault'ed, so Select / Select All / Paste all
  // still appear and text paste is untouched. The chip is a second affordance
  // at the point the thumb already is.

  var _lpTimer = null, _lpStart = null, _chip = null;

  function hideChip() {
    if (_chip && _chip.parentNode) _chip.parentNode.removeChild(_chip);
    _chip = null;
  }

  var IMG_SVG =
    '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
    'stroke-linejoin="round" aria-hidden="true">' +
    '<rect x="3" y="3" width="18" height="18" rx="2"/>' +
    '<circle cx="8.5" cy="8.5" r="1.5"/>' +
    '<path d="M21 15l-5-5L5 21"/></svg>';

  var CLIP_SVG =
    '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
    'stroke-linejoin="round" aria-hidden="true">' +
    '<rect x="8" y="2" width="8" height="4" rx="1"/>' +
    '<path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/>' +
    '</svg>';

  // iOS Safari will not hand image data to a paste event on a <textarea> — it
  // filters non-text for plain text inputs, which is why text paste works and
  // image paste silently does nothing. Reading the clipboard directly is the
  // only route. It needs a secure context (this app is served over HTTPS by
  // Tailscale) and must run inside the click gesture, so no awaits before it.
  function pasteImageFromClipboard() {
    diag('paste tapped; ua=' + navigator.userAgent.slice(0, 120));
    if (!navigator.clipboard || !navigator.clipboard.read) {
      diag('clipboard.read unavailable');
      toast('This browser cannot read images from the clipboard');
      return;
    }
    navigator.clipboard.read().then(function (items) {
      var seen = [];
      try {
        diag('read ok; items=' + items.length + '; types=' +
             Array.prototype.map.call(items, function (it) {
               return (it.types || []).join('+');
             }).join(' | '));
      } catch (e) { diag('read ok; type introspection failed: ' + e.message); }

      // Pass 1 — any advertised image type. iOS sometimes advertises Apple UTIs
      // (public.png, public.heic) rather than MIME types, so match both.
      for (var i = 0; i < items.length; i++) {
        var types = items[i].types || [];
        for (var j = 0; j < types.length; j++) {
          seen.push(types[j]);
          if (/^image\//.test(types[j]) || /^public\.(png|jpeg|jpg|heic|heif|tiff|image)/.test(types[j])) {
            return grab(items[i], types[j]);
          }
        }
      }

      // Pass 2 — ask for image/png even when it was not advertised. Safari has
      // shipped builds where a pasteboard image is retrievable but unlisted.
      for (var k = 0; k < items.length; k++) {
        try {
          return items[k].getType('image/png')
            .then(function (blob) { return deliver(blob, 'image/png'); })
            .catch(function () { reportMiss(seen); });
        } catch (e) { /* fall through to the report */ }
      }

      reportMiss(seen);
    }).catch(function (e) {
      var n = e && (e.name || '');
      diag('read FAILED: ' + n + ' ' + (e && e.message || ''));
      if (n === 'NotAllowedError') {
        toast('[' + BUILD + '] Clipboard denied. Tap "Paste" when Safari asks.');
      } else {
        toast('[' + BUILD + '] Clipboard error: ' + (e.message || n || e));
      }
    });

    function grab(item, type) {
      return item.getType(type).then(function (blob) { deliver(blob, type); });
    }

    function deliver(blob, type) {
      diag('deliver: type=' + type + ' size=' + (blob && blob.size));
      if (!blob || !blob.size) { toast('Clipboard image was empty'); return; }
      var mime = blob.type || (/^image\//.test(type) ? type : 'image/png');
      var ext = (mime.split('/')[1] || 'png').replace('jpeg', 'jpg');
      var file;
      try {
        file = new File([blob], 'pasted-image.' + ext, {type: mime});
      } catch (e) {
        blob.name = 'pasted-image.' + ext;   // older Safari: no File constructor
        file = blob;
      }
      upload(file);
    }

    // Report what WAS on the clipboard. "No image" alone is undiagnosable; the
    // type list says immediately whether iOS withheld the image or the
    // clipboard genuinely held text.
    function reportMiss(seen) {
      var list = seen.filter(function (t, i) { return seen.indexOf(t) === i; }).join(', ');
      toast('[' + BUILD + '] ' + (list ? ('Clipboard has: ' + list + ' — no image')
                                       : 'Clipboard empty or unreadable'));
    }
  }

  function showChip(x, y) {
    hideChip();
    var chip = document.createElement('div');
    chip.className = 'vn-attach-chip';

    // Action 1 — paste whatever image is on the clipboard.
    var pasteBtn = document.createElement('button');
    pasteBtn.type = 'button';
    pasteBtn.className = 'vn-chip-act';
    pasteBtn.innerHTML = CLIP_SVG + '<span>Paste image</span>';
    pasteBtn.addEventListener('click', function (e) {
      e.preventDefault();
      hideChip();
      pasteImageFromClipboard();
    });

    // Action 2 — pick from the photo library / camera.
    var pickBtn = document.createElement('label');
    pickBtn.className = 'vn-chip-act';
    pickBtn.innerHTML = IMG_SVG + '<span>Photos</span>';
    var input = document.createElement('input');
    input.type = 'file';
    input.accept = 'image/*';
    input.style.display = 'none';
    input.addEventListener('change', function () {
      var f = input.files && input.files[0];
      input.value = '';
      hideChip();
      if (f) upload(f);
    });
    pickBtn.appendChild(input);

    chip.appendChild(pasteBtn);
    chip.appendChild(pickBtn);

    // Sit above the touch point, clamped into the viewport. The native menu
    // renders near the touch too, so bias upward to reduce overlap.
    chip.style.left = Math.max(8, Math.min(x - 90, window.innerWidth - 232)) + 'px';
    chip.style.top = Math.max(8, y - 96) + 'px';

    document.body.appendChild(chip);
    _chip = chip;

    setTimeout(function () { if (_chip === chip) hideChip(); }, 8000);
  }

  function lpCancel() {
    if (_lpTimer) { clearTimeout(_lpTimer); _lpTimer = null; }
    _lpStart = null;
  }

  document.addEventListener('touchstart', function (ev) {
    var t = ev.target;
    if (!t || !t.id || (t.id !== 'live-input-ta' && t.id !== 'live-queue-ta')) {
      if (_chip && !(_chip.contains && _chip.contains(t))) hideChip();
      return;
    }
    var p = ev.touches && ev.touches[0];
    if (!p) return;
    _lpStart = {x: p.clientX, y: p.clientY};
    lpCancelTimerSet(p.clientX, p.clientY);
  }, {passive: true});

  function lpCancelTimerSet(x, y) {
    if (_lpTimer) clearTimeout(_lpTimer);
    // 340ms, deliberately under iOS's ~500ms text-selection threshold, so the
    // chip is on screen before the system gesture claims the touch.
    _lpTimer = setTimeout(function () { showChip(x, y); }, 340);
  }

  document.addEventListener('touchmove', function (ev) {
    if (!_lpStart) return;
    var p = ev.touches && ev.touches[0];
    if (!p) return;
    if (Math.abs(p.clientX - _lpStart.x) > 10 || Math.abs(p.clientY - _lpStart.y) > 10) lpCancel();
  }, {passive: true});

  document.addEventListener('touchend', lpCancel, {passive: true});
  // NOT touchcancel. iOS fires touchcancel precisely when its own long-press
  // selection gesture takes over — which is the case this feature exists for.
  // Cancelling there meant racing iOS for the same gesture and losing.
  window.addEventListener('scroll', hideChip, {passive: true, capture: true});

  // Tap anywhere else to dismiss (covers the button-opened chip, which has no
  // touchstart on a composer to close it).
  document.addEventListener('click', function (ev) {
    if (!_chip) return;
    var t = ev.target;
    if (_chip.contains(t)) return;
    if (t && t.closest && t.closest('#live-image-btn')) return;
    hideChip();
  }, true);

  // The toolbar button opens the SAME two-action menu as the long-press. This
  // is the path that does not depend on iOS gesture timing, so it always works.
  function makeButton() {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'live-send-btn live-image-btn';
    btn.id = 'live-image-btn';
    btn.title = 'Attach an image';
    btn.setAttribute('aria-label', 'Attach an image');
    btn.innerHTML = IMG_SVG.replace(/width="15" height="15"/, 'width="16" height="16"');

    btn.addEventListener('click', function (e) {
      e.preventDefault();
      if (_chip) { hideChip(); return; }      // tap again to dismiss
      var r = btn.getBoundingClientRect();
      showChip(r.left + r.width / 2, r.top);
    });
    return btn;
  }

  function injectInto(row) {
    if (!row || row.querySelector('#live-image-btn')) return;
    var voice = row.querySelector('#live-voice-btn');
    var btn = makeButton();
    if (voice) row.insertBefore(btn, voice);
    else row.appendChild(btn);
  }

  function scan() {
    var rows = document.querySelectorAll('.live-bar-row');
    for (var i = 0; i < rows.length; i++) injectInto(rows[i]);
  }

  function start() {
    scan();
    var obs = new MutationObserver(function (muts) {
      for (var i = 0; i < muts.length; i++) {
        if (muts[i].addedNodes && muts[i].addedNodes.length) { scan(); return; }
      }
    });
    obs.observe(document.body, {childList: true, subtree: true});
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }

  window.vnAttachImage = upload;   // exposed for future callers / debugging
})();
