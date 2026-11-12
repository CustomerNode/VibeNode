/**
 * Sticky user message bar — a small overlay at the top of the conversation
 * showing a one-line preview of your most recent message that's been
 * scrolled out of view.  Click to expand and see the full text.
 *
 * The bar lives OUTSIDE the scroll container (sibling, absolutely positioned)
 * so it never touches message layout and cannot cause scroll jitter.
 *
 * Controlled by the "stickyUserMsgs" localStorage preference.
 */
(function() {
  'use strict';

  var _containers = new WeakMap();
  // The last prompt of each session, remembered after its bubble leaves the
  // page.  During a long run the thread drops its oldest messages (the
  // 400-message cap, _trimLiveLogIfOversized) and a reopened session only
  // draws its newest page, so the prompt that started the run is often not on
  // the page at all. Remembering it keeps the bar up in exactly that case.
  var _lastPrompt = {};       // session id -> text
  var _fetched = {};          // session id -> true once asked the server

  function _getText(msgEl) {
    var body = msgEl.querySelector('.msg-body');
    return body ? body.textContent.trim() : '';
  }

  function _sid() {
    try { return (typeof liveSessionId !== 'undefined' && liveSessionId) || ''; } catch (e) { return ''; }
  }

  // Ask the server for the session's newest prompt once, when the page holds none.
  function _fetchPrompt(sid, done) {
    if (!sid || _fetched[sid]) return;
    _fetched[sid] = true;
    var project = localStorage.getItem('activeProject') || '';
    fetch('/api/session-log/' + encodeURIComponent(sid) + '?last=2000' +
          (project ? '&project=' + encodeURIComponent(project) : ''))
      .then(function(r) { return r.ok ? r.json() : null; })
      .then(function(data) {
        var entries = (data && data.entries) || [];
        for (var i = entries.length - 1; i >= 0; i--) {
          var e = entries[i];
          if (e && e.kind === 'user' && typeof e.text === 'string' && e.text.trim()) {
            if (!_lastPrompt[sid]) _lastPrompt[sid] = e.text.trim();
            break;
          }
        }
        done();
      })
      .catch(function() { delete _fetched[sid]; });
  }

  function initStickyUserMessages(container) {
    if (!container || localStorage.getItem('stickyUserMsgs') === 'off') return;

    // Clean up previous instance
    var prev = _containers.get(container);
    if (prev) {
      container.removeEventListener('scroll', prev.handler);
      if (prev.observer) prev.observer.disconnect();
      if (prev.bar && prev.bar.parentNode) prev.bar.remove();
      container.classList.remove('has-sticky-bar');
    }

    // Build the overlay bar as a sibling before the scroll container
    var parent = container.parentNode;
    if (!parent) return;
    parent.style.position = 'relative';

    var bar = document.createElement('div');
    bar.className = 'sticky-user-bar';
    bar.innerHTML = '<span class="sticky-user-text"></span>';
    parent.insertBefore(bar, container);
    // While the bar is showing, the thread itself fades out toward its top
    // edge (a mask on the scroll container, see .has-sticky-bar in style.css),
    // so nothing scrolls visibly under the bar.  This is a fade of the CONTENT,
    // not a haze laid over it: an overlay reads as a glow.

    var textEl = bar.querySelector('.sticky-user-text');
    var currentMsg = null;

    bar.addEventListener('click', function() {
      bar.classList.toggle('expanded');
    });

    function show(text, key) {
      if (key !== currentMsg) {
        textEl.textContent = text;
        currentMsg = key;
        bar.classList.remove('expanded');
      }
      if (!bar.classList.contains('visible')) { bar.classList.add('visible'); container.classList.add('has-sticky-bar'); }
    }

    function hide() {
      if (bar.classList.contains('visible')) { bar.classList.remove('visible'); container.classList.remove('has-sticky-bar'); }
      currentMsg = null;
    }

    function update() {
      if (!container.isConnected) return;
      var userMsgs = container.querySelectorAll(':scope > .msg.user');
      var scrollTop = container.scrollTop;
      var sid = _sid();
      var pinned = null;

      // The newest prompt on the page is the one to remember.
      if (userMsgs.length && sid) {
        var newest = _getText(userMsgs[userMsgs.length - 1]);
        if (newest) _lastPrompt[sid] = newest;
      }

      // Walk newest → oldest — first one above the viewport wins
      for (var i = userMsgs.length - 1; i >= 0; i--) {
        var msg = userMsgs[i];
        if (msg.offsetTop + msg.offsetHeight <= scrollTop + 4) {
          pinned = msg;
          break;
        }
      }

      if (pinned) { show(_getText(pinned), pinned); return; }
      if (userMsgs.length) { hide(); return; }   // the prompt is on screen (or below it)

      // No prompt on the page: show the remembered one, or ask for it once.
      var text = sid && _lastPrompt[sid];
      if (text) { show(text, 'remembered:' + sid + ':' + text); return; }
      hide();
      if (sid) _fetchPrompt(sid, handler);
    }

    var rafId = null;
    function handler() {
      if (rafId) cancelAnimationFrame(rafId);
      rafId = requestAnimationFrame(update);
    }

    container.addEventListener('scroll', handler, { passive: true });
    // Messages arrive, get trimmed and get re-rendered without any scroll, so
    // the bar re-checks on those too (direct children only: cheap).
    var observer = (typeof MutationObserver === 'function') ? new MutationObserver(handler) : null;
    if (observer) observer.observe(container, { childList: true });
    _containers.set(container, { handler: handler, bar: bar, observer: observer });

    // Initial check
    update();
  }

  window.initStickyUserMessages = initStickyUserMessages;
})();
