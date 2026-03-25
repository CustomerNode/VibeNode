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

  function _getText(msgEl) {
    var body = msgEl.querySelector('.msg-body');
    return body ? body.textContent.trim() : '';
  }

  function initStickyUserMessages(container) {
    if (!container || localStorage.getItem('stickyUserMsgs') === 'off') return;

    // Clean up previous instance
    var prev = _containers.get(container);
    if (prev) {
      container.removeEventListener('scroll', prev.handler);
      if (prev.bar && prev.bar.parentNode) prev.bar.remove();
    }

    // Build the overlay bar as a sibling before the scroll container
    var parent = container.parentNode;
    if (!parent) return;
    parent.style.position = 'relative';

    var bar = document.createElement('div');
    bar.className = 'sticky-user-bar';
    bar.innerHTML = '<span class="sticky-user-text"></span>';
    parent.insertBefore(bar, container);

    var textEl = bar.querySelector('.sticky-user-text');
    var currentMsg = null;

    bar.addEventListener('click', function() {
      bar.classList.toggle('expanded');
    });

    function update() {
      var userMsgs = container.querySelectorAll(':scope > .msg.user');
      var scrollTop = container.scrollTop;
      var pinned = null;

      // Walk newest → oldest — first one above the viewport wins
      for (var i = userMsgs.length - 1; i >= 0; i--) {
        var msg = userMsgs[i];
        if (msg.offsetTop + msg.offsetHeight <= scrollTop + 4) {
          pinned = msg;
          break;
        }
      }

      if (pinned) {
        if (pinned !== currentMsg) {
          textEl.textContent = _getText(pinned);
          currentMsg = pinned;
          bar.classList.remove('expanded');
        }
        if (!bar.classList.contains('visible')) bar.classList.add('visible');
      } else {
        if (bar.classList.contains('visible')) bar.classList.remove('visible');
        currentMsg = null;
      }
    }

    var rafId = null;
    function handler() {
      if (rafId) cancelAnimationFrame(rafId);
      rafId = requestAnimationFrame(update);
    }

    container.addEventListener('scroll', handler, { passive: true });
    _containers.set(container, { handler: handler, bar: bar });

    // Initial check
    update();
  }

  window.initStickyUserMessages = initStickyUserMessages;
})();
