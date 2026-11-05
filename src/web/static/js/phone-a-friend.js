/* Phone a Friend (VibeNode Orgs): the screens.
 *
 * The working parts live in the web server (src/web/orgs/, /api/orgs/*,
 * /api/phone/*). This file draws them, in the quiet look:
 *   - "Phone a friend" in the composer's four-squares menu beside Send (and a
 *     session's right-click menu), only while this VibeNode is in an org:
 *     pick members, what they can do, how long, a note, Call.
 *   - A phone mark on the session's row and in the chat header while a call
 *     is live; the header mark lists who is on it, with Revoke / Revoke all.
 *   - A plain toast (and a notification when the tab is hidden) when a friend
 *     phones you, with Open; and one when a friend suggests a message.
 *   - A session someone phoned you about is a real session in a "<caller>
 *     phoned you" project at the top of the project list (src/web/orgs/
 *     mirror.py): the app's own list, chat and composer show it.
 *   - System → VibeNode Orgs: set up, members, Add member (a one-time invite
 *     prompt to copy), leave.
 */
(function () {
  'use strict';

  var PHONE_SVG = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1.9.4 1.8.7 2.7a2 2 0 0 1-.5 2.1L8 9.8a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.7.7a2 2 0 0 1 1.7 2z"/></svg>';
  var LEVELS = [['view', 'View'], ['suggest', 'Suggest'], ['drive', 'Drive']];
  var DURS = [['1h', '1h'], ['4h', '4h'], ['today', 'Today'], ['revoked', 'Until I revoke']];
  var LEVEL_WORDS = { view: 'can watch', suggest: 'can suggest', drive: 'can drive' };

  // activeId (app.js) and socket (socket.js) are top-level let/const: shared
  // between classic scripts, but not properties of window.
  function curSid() { return typeof activeId !== 'undefined' ? activeId : null; }
  function sock() { return typeof socket !== 'undefined' ? socket : null; }

  var st = { me: null, out: [], incoming: [], members: null, loaded: false };

  function esc(s) { return (window.escHtml ? escHtml(String(s == null ? '' : s)) : String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; })); }
  function api(path, opts) {
    opts = opts || {};
    var init = { method: opts.method || 'GET', headers: {} };
    if (opts.body !== undefined) { init.method = opts.method || 'POST'; init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.body); }
    return fetch(path, init).then(function (r) { return r.json().catch(function () { return { ok: false }; }).then(function (d) { d = d || {}; d._status = r.status; return d; }); })
      .catch(function () { return { ok: false, error: 'network', message: 'Could not reach VibeNode.' }; });
  }
  function toastText(msg, err) { if (window.showToast) showToast(msg, !!err); }
  // What a friend calls a shared session: its project first, then its title.
  function label(g) {
    var t = g.title || 'a session';
    return g.project ? g.project + ' · ' + t : t;
  }
  function left(g) {
    if (!g.expires_at) return 'until revoked';
    var ms = new Date(g.expires_at) - Date.now();
    if (ms <= 0) return 'ended';
    var m = Math.round(ms / 60000);
    return m >= 120 ? Math.round(m / 60) + 'h left' : m + 'm left';
  }

  // ── state ────────────────────────────────────────────────────────────────
  function isMember() { return !!(st.me && st.me.member); }
  function liveOut(sid) { return st.out.filter(function (g) { return g.live && g.session_id === sid; }); }

  function refresh() {
    return api('/api/orgs/me').then(function (me) {
      st.me = me && me.ok ? me : null;
      if (!isMember()) { st.out = []; st.incoming = []; st.loaded = true; paint(); return; }
      return Promise.all([api('/api/phone/out'), api('/api/phone/in?days=7')]).then(function (r) {
        if (r[0].ok) st.out = r[0].grants || [];
        if (r[1].ok) st.incoming = r[1].grants || [];
        st.loaded = true; paint();
      });
    });
  }

  // ── marks on the row and in the header ───────────────────────────────────
  function rowMark(sid) {
    var n = liveOut(sid).length;
    if (!n) return '';
    var who = liveOut(sid).map(function (g) { return g.guest + (g.note ? ': ' + g.note : ''); }).join('; ');
    return '<span class="pf-mark" title="Shared with ' + esc(who) + '">' + PHONE_SVG + '</span>';
  }

  var _paintedRows = '';
  function paint() {
    var key = JSON.stringify(st.out.filter(function (g) { return g.live; }).map(function (g) { return g.grant_id; }));
    if (key !== _paintedRows) {
      _paintedRows = key;
      document.querySelectorAll('.session-item[data-sid], .wf-card[data-sid]').forEach(function (row) {
        var sid = row.getAttribute('data-sid');
        var old = row.querySelector('.pf-mark');
        var html = rowMark(sid);
        if (old && !html) old.remove();
        else if (!old && html) {
          var name = row.querySelector('.session-col-name') || row.querySelector('.wf-card-title') || row;
          name.insertAdjacentHTML('beforeend', html);
        }
      });
    }
    paintHeader();
  }

  function paintHeader() {
    var title = document.getElementById('main-title');
    if (!title) return;
    var sid = curSid();
    var grants = sid ? liveOut(sid) : [];
    var mark = document.getElementById('pf-head-mark');
    if (!grants.length) { if (mark) mark.remove(); return; }
    if (!mark) {
      mark = document.createElement('button');
      mark.id = 'pf-head-mark';
      mark.className = 'pf-head-mark';
      mark.type = 'button';
      mark.onclick = function (e) { e.stopPropagation(); openList(curSid(), mark); };
      title.insertAdjacentElement('afterend', mark);
    }
    mark.title = 'Shared with ' + grants.map(function (g) { return g.guest + (g.note ? ': ' + g.note : ''); }).join('\n');
    mark.innerHTML = PHONE_SVG + (grants.length > 1 ? '<sup>' + grants.length + '</sup>' : '');
  }

  // ── the call's note: shown everywhere, either side edits it ──────────────
  function noteHtml(g) {
    var who = g.note_by ? '<span class="pf-nby">' + esc(g.note_by) + '</span>' : '';
    return '<div class="pf-callnote" data-grant="' + esc(g.grant_id) + '">'
      + '<div class="pf-ntext">' + (g.note ? esc(g.note) : '<span class="pf-nempty">No note</span>') + '</div>'
      + '<div class="pf-nmeta">' + (g.note ? who : '') + '<button type="button" class="pf-ghost pf-nedit">' + (g.note ? 'Edit' : 'Add note') + '</button></div></div>';
  }
  function wireNotes(root) {
    root.querySelectorAll('.pf-callnote').forEach(function (box) {
      var btn = box.querySelector('.pf-nedit');
      if (!btn || btn._wired) return;
      btn._wired = true;
      btn.onclick = function (e) {
        e.stopPropagation();
        var gid = box.getAttribute('data-grant');
        var g = findGrant(gid) || {};
        box.innerHTML = '<textarea class="pf-nin" maxlength="500" rows="2"></textarea><div class="pf-nmeta"><button type="button" class="pf-go pf-nsave">Save</button><button type="button" class="pf-ghost pf-ncancel">Cancel</button></div>';
        var ta = box.querySelector('textarea');
        ta.value = g.note || ''; ta.focus();
        var restore = function (gg) { var r = box.parentNode; box.outerHTML = noteHtml(gg || g); if (r) wireNotes(r); };
        box.querySelector('.pf-ncancel').onclick = function (ev) { ev.stopPropagation(); restore(); };
        box.querySelector('.pf-nsave').onclick = function (ev) {
          ev.stopPropagation();
          api('/api/phone/note/' + gid, { body: { note: ta.value.trim() } }).then(function (d) {
            if (!d.ok) { toastText(d.message || 'Note not saved.', true); return; }
            putGrant(d.grant); restore(d.grant); paintNotes(d.grant);
          });
        };
      };
    });
  }
  function findGrant(gid) {
    return st.out.concat(st.incoming).filter(function (g) { return g.grant_id === gid; })[0];
  }
  function putGrant(g) {
    [st.out, st.incoming].forEach(function (list) {
      for (var i = 0; i < list.length; i++) if (list[i].grant_id === g.grant_id) list[i] = Object.assign({}, list[i], g);
    });
  }
  // Every note on screen for this call shows the latest text.
  function paintNotes(g) {
    document.querySelectorAll('.pf-callnote[data-grant="' + g.grant_id + '"]').forEach(function (box) {
      if (box.querySelector('textarea')) return;
      var root = box.parentNode;
      box.outerHTML = noteHtml(g);
      if (root) wireNotes(root);
    });
    paintHeader();
  }

  // ── popovers ─────────────────────────────────────────────────────────────
  // A phone gets a bottom sheet: full width, finger-sized rows, and text
  // boxes at 16px so iOS does not zoom the page in on focus.
  function isPhone() {
    return (window.matchMedia && window.matchMedia('(max-width: 768px)').matches)
      || (typeof window._isTouchKeyboard === 'function' && window._isTouchKeyboard());
  }
  var _popCloser = null;
  function closePop() {
    var p = document.querySelector('.pf-pop'); if (p) p.remove();
    var sc = document.querySelector('.pf-scrim'); if (sc) sc.remove();
    if (_popCloser) { document.removeEventListener('pointerdown', _popCloser, true); _popCloser = null; }
  }
  function popAt(html, anchor) {
    closePop();
    var p = document.createElement('div');
    p.className = 'pf-pop';
    p.innerHTML = html;
    if (isPhone()) {
      // Drop the composer's keyboard first, so the sheet is not pushed off screen.
      if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
      var scrim = document.createElement('div');
      scrim.className = 'pf-scrim';
      scrim.addEventListener('click', closePop);
      document.body.appendChild(scrim);
      p.classList.add('pf-sheet');
      document.body.appendChild(p);
      return p;
    }
    document.body.appendChild(p);
    var r = anchor && anchor.getBoundingClientRect ? anchor.getBoundingClientRect() : null;
    if (r && !r.width && !r.height) r = null;          // anchor not on screen: centre it
    var w = p.offsetWidth, h = p.offsetHeight;
    var x = r ? Math.min(window.innerWidth - w - 8, Math.max(8, r.right - w)) : (window.innerWidth - w) / 2;
    var y = r ? Math.min(window.innerHeight - h - 8, r.bottom + 6) : Math.max(20, (window.innerHeight - h) / 3);
    p.style.left = Math.max(8, x) + 'px';
    p.style.top = Math.max(8, y) + 'px';
    setTimeout(function () {
      if (!p.isConnected) return;
      _popCloser = function (ev) { if (!p.contains(ev.target)) closePop(); };
      document.addEventListener('pointerdown', _popCloser, true);
    }, 0);
    return p;
  }
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closePop(); });

  function sessionTitle(sid) {
    var s = window.SessionStore && SessionStore.get ? SessionStore.get(sid) : null;
    return (s && (s.display_title || s.custom_title)) || (sid === curSid() && document.getElementById('main-title') ? document.getElementById('main-title').textContent : '') || 'Untitled session';
  }

  function openCall(sid, anchor) {
    if (!isMember()) { openOrgs(); return; }
    // One or more people: each gets their own call (grant), revoked on its own.
    var pick = { guests: {}, level: 'drive', dur: 'revoked' };
    var p = popAt('<h4>Phone a friend</h4><div class="pf-lab pf-lab-top">Pick one or more</div><div class="pf-members">Loading members…</div>'
      + '<div class="pf-lab">They can</div><div class="pf-seg" data-k="level">' + LEVELS.map(function (l) { return '<button type="button" data-v="' + l[0] + '"' + (l[0] === pick.level ? ' class="on"' : '') + '>' + l[1] + '</button>'; }).join('') + '</div>'
      + '<div class="pf-lab">For</div><div class="pf-seg" data-k="dur">' + DURS.map(function (d) { return '<button type="button" data-v="' + d[0] + '"' + (d[0] === pick.dur ? ' class="on"' : '') + '>' + d[1] + '</button>'; }).join('') + '</div>'
      + '<div class="pf-lab">Note</div><textarea class="pf-nin pf-callnote-in" maxlength="500" rows="2" placeholder="What you need help with"></textarea>'
      + '<div class="pf-note"></div><div class="pf-acts"><button type="button" class="pf-go" disabled>Pick someone to call</button><button type="button" class="pf-ghost pf-cancel">Cancel</button></div>', anchor);
    p.querySelector('.pf-cancel').onclick = closePop;
    p.querySelectorAll('.pf-seg').forEach(function (seg) {
      seg.onclick = function (e) {
        var b = e.target.closest('button'); if (!b) return;
        pick[seg.dataset.k] = b.dataset.v;
        seg.querySelectorAll('button').forEach(function (x) { x.classList.toggle('on', x === b); });
      };
    });
    var go = p.querySelector('.pf-go');
    api('/api/orgs/members').then(function (d) {
      var box = p.querySelector('.pf-members');
      if (!box) return;
      var others = (d.members || []).filter(function (m) { return !m.you; });
      if (!d.ok) { box.textContent = d.message || 'Could not load your org.'; return; }
      if (!others.length) { box.innerHTML = 'Nobody else is in your org yet. <a href="#" class="pf-add">Add member</a>'; box.querySelector('.pf-add').onclick = function (e) { e.preventDefault(); closePop(); openOrgs(); }; return; }
      box.innerHTML = others.map(function (m) { return '<button type="button" class="pf-opt" data-id="' + esc(m.member_id) + '" data-name="' + esc(m.username) + '"><span class="pf-av">' + esc(m.username.charAt(0).toUpperCase()) + '</span>' + esc(m.username) + '<span class="pf-tick"></span></button>'; }).join('');
      box.onclick = function (e) {
        var b = e.target.closest('.pf-opt'); if (!b) return;
        if (pick.guests[b.dataset.id]) delete pick.guests[b.dataset.id];
        else pick.guests[b.dataset.id] = b.dataset.name;
        b.classList.toggle('on', !!pick.guests[b.dataset.id]);
        b.querySelector('.pf-tick').textContent = pick.guests[b.dataset.id] ? '✓' : '';
        var names = Object.keys(pick.guests).map(function (k) { return pick.guests[k]; });
        go.disabled = !names.length;
        go.textContent = !names.length ? 'Pick someone to call' : names.length === 1 ? 'Call ' + names[0] : 'Call ' + names.length + ' people';
      };
      // Only one person to call: pick them already.
      if (others.length === 1) box.querySelector('.pf-opt').click();
    });
    go.onclick = function () {
      var ids = Object.keys(pick.guests);
      if (!ids.length) return;
      go.disabled = true; go.textContent = 'Calling…';
      var note = (p.querySelector('.pf-callnote-in').value || '').trim();
      Promise.all(ids.map(function (id) {
        return api('/api/phone/call', { body: { session_id: sid, guest_id: id, level: pick.level, duration: pick.dur, title: sessionTitle(sid), note: note } })
          .then(function (d) { d._id = id; return d; });
      })).then(function (results) {
        var ok = results.filter(function (d) { return d.ok; }), bad = results.filter(function (d) { return !d.ok; });
        refresh();
        if (!bad.length) {
          closePop();
          var g = ok[0].grant;
          toastText('Phoned ' + ok.map(function (d) { return d.grant.guest; }).join(', ') + '. They ' + LEVEL_WORDS[g.level] + ', ' + left(g) + '.');
          return;
        }
        // Keep only the people whose call failed selected, so Try again redoes just them.
        ok.forEach(function (d) { delete pick.guests[d._id]; var b = p.querySelector('.pf-opt[data-id="' + d._id + '"]'); if (b) { b.classList.remove('on'); b.querySelector('.pf-tick').textContent = ''; } });
        p.querySelector('.pf-note').textContent = (ok.length ? 'Phoned ' + ok.map(function (d) { return d.grant.guest; }).join(', ') + '. ' : '')
          + 'Not through to ' + bad.map(function (d) { return pick.guests[d._id]; }).join(', ') + ': ' + (bad[0].message || 'the call did not go through.');
        go.disabled = false; go.textContent = 'Try again';
      });
    };
  }

  function openList(sid, anchor) {
    var grants = liveOut(sid);
    var p = popAt('<h4>Shared with ' + grants.length + '</h4><div class="pf-glist">' + grants.map(function (g) {
      return '<div class="pf-gl"><span class="pf-av">' + esc(g.guest.charAt(0).toUpperCase()) + '</span><span class="pf-gn">' + esc(g.guest) + '</span><span class="pf-meta">' + esc(LEVEL_WORDS[g.level]) + ' · ' + esc(left(g)) + '</span><button type="button" class="pf-ghost" data-g="' + esc(g.grant_id) + '">Revoke</button></div>' + noteHtml(g);
    }).join('') + '</div><div class="pf-acts"><button type="button" class="pf-ghost pf-all">Revoke all</button><button type="button" class="pf-ghost pf-more">Phone someone else</button></div>', anchor);
    function revoke(ids) {
      return Promise.all(ids.map(function (id) { return api('/api/phone/revoke/' + id, { body: {} }); })).then(function () { closePop(); refresh(); toastText(ids.length > 1 ? 'Ended every call on this session.' : 'Ended that call.'); });
    }
    p.querySelectorAll('[data-g]').forEach(function (b) { b.onclick = function () { revoke([b.dataset.g]); }; });
    wireNotes(p);
    p.querySelector('.pf-all').onclick = function () { revoke(grants.map(function (g) { return g.grant_id; })); };
    p.querySelector('.pf-more').onclick = function () { openCall(sid, anchor); };
  }

  // ── toasts (the float stack, utils.js) ───────────────────────────────────
  function floatToast(html, ms) {
    var t = document.createElement('div');
    t.className = 'pf-toast';
    t.innerHTML = html + '<button type="button" class="pf-x" title="Close">×</button>';
    document.body.appendChild(t);
    var done = function () { t.remove(); if (window._layoutFloats) _layoutFloats(); };
    t.querySelector('.pf-x').onclick = done;
    if (window._updateFloatOffset) _updateFloatOffset();
    if (window._layoutFloats) _layoutFloats();
    if (ms) setTimeout(done, ms);
    return { el: t, close: done };
  }

  function onIncoming(g) {
    if (!g || !g.grant_id) return;
    var t = floatToast(PHONE_SVG + '<span><b>' + esc(g.owner) + ' phoned you</b> about “' + esc(label(g)) + '”' + (g.note ? '<br><span class="pf-tnote">' + esc(g.note) + '</span>' : '') + '</span><button type="button" class="pf-ghost pf-open">Open</button>', 60000);
    t.el.querySelector('.pf-open').onclick = function () { t.close(); openFriend(g.owner_id, g.share_id); };
    if (window.VNNotify && VNNotify.notice) VNNotify.notice({ key: 'pf:' + g.grant_id, title: g.owner + ' phoned you', body: label(g) + ' · ' + LEVEL_WORDS[g.level] + (g.note ? '\n' + g.note : ''), banner: true });
    refresh();
  }

  function onSuggestion(d) {
    if (!d || !d.key) return;
    var t = floatToast(PHONE_SVG + '<span><b>' + esc(d.from) + ' suggests</b> “' + esc((d.text || '').slice(0, 140)) + '”</span><button type="button" class="pf-ghost pf-send">Send</button><button type="button" class="pf-ghost pf-skip">Dismiss</button>', 0);
    var act = function (a) { api('/api/phone/suggestions/' + encodeURIComponent(d.key) + '/' + a, { body: {} }).then(function (r) { t.close(); if (r.ok && a === 'send') toastText('Sent ' + d.from + "'s suggestion to the session."); }); };
    t.el.querySelector('.pf-send').onclick = function () { act('send'); };
    t.el.querySelector('.pf-skip').onclick = function () { act('dismiss'); };
    if (window.VNNotify && VNNotify.notice) VNNotify.notice({ key: 'pfs:' + d.key, title: d.from + ' suggests a message', body: (d.text || '').slice(0, 120), banner: true });
  }

  function onNote(g) {
    if (!g || !g.grant_id) return;
    putGrant(g); paintNotes(g);
    if (st.me && g.note_by && g.note_by !== st.me.username)
      toastText(g.note_by + ' changed the note on “' + label(g) + '”: ' + (g.note || '(cleared)'));
  }

  function onMessage(d) {
    if (d && d.from) toastText(d.from + ' sent a message to “' + sessionTitle(d.session_id) + '”.');
  }

  // ── a phoned session is a real session ───────────────────────────────────
  //
  // The friend's VibeNode writes the shared thread into a session of its own,
  // in a "<caller> phoned you" project (src/web/orgs/mirror.py), so the app's
  // own list, chat, summary cards and composer show it. A message typed there
  // goes to the caller (the socket handlers' _phoned_session_reply). This file
  // only opens it from the call toast and adds Hang up / the note to the
  // composer's four-squares menu.
  function grantForSession(sid) {
    return st.incoming.filter(function (g) { return g.share_id === sid; })
      .sort(function (a, b) { return (b.live ? 1 : 0) - (a.live ? 1 : 0); })[0] || null;
  }
  function isPhoned(sid) { return !!grantForSession(sid); }

  function openShare(shareId) {
    var g = grantForSession(shareId);
    var go = function (enc) {
      var open = function () { if (typeof openInGUI === 'function') openInGUI(shareId); };
      var cur = localStorage.getItem('activeProject') || '';
      if (enc && cur !== enc && typeof selectProjectFromOverlay === 'function') {
        selectProjectFromOverlay(enc);
        var tries = 0;
        (function wait() {
          var ok = window.SessionStore && SessionStore.has && SessionStore.has(shareId);
          if (ok || ++tries > 40) open(); else setTimeout(wait, 250);
        })();
      } else open();
    };
    api('/api/phone/mirror/' + encodeURIComponent(shareId)).then(function (d) { go(d && d.project); });
    return g;
  }
  function openFriend(ownerId, shareId) {
    var gs = st.incoming.filter(function (g) { return g.owner_id === ownerId; });
    var g = gs.filter(function (x) { return x.share_id === shareId; })[0] || gs.filter(function (x) { return x.live; })[0] || gs[0];
    if (g) openShare(g.share_id);
  }

  // Composer menu rows for a phoned session (image-attach.js asks for them).
  function composerRows(sid) {
    var g = grantForSession(sid);
    if (!g) return [];
    var rows = [{ label: 'Call note', sub: g.note || 'Add a note for ' + g.owner, run: function (btn) {
      var p = popAt('<h4>Call with ' + esc(g.owner) + '</h4>' + noteHtml(g), btn);
      wireNotes(p);
    } }];
    if (g.live) rows.push({ label: 'Hang up', sub: 'End ' + g.owner + '’s call', run: function () {
      api('/api/phone/revoke/' + g.grant_id, { body: {} }).then(function (d) {
        if (!d.ok) { toastText(d.message || 'Could not hang up.', true); return; }
        toastText('Hung up on ' + g.owner + '’s session.');
        refresh();
      });
    } });
    return rows;
  }

  function onNotice(d) {
    if (d && d.message) toastText(d.message, d.ok === false);
  }

  // ── System → VibeNode Orgs ───────────────────────────────────────────────
  function openOrgs() {
    var overlay = document.getElementById('pm-overlay');
    if (!overlay) return;
    overlay.innerHTML = '<div class="pm-card pm-enter pf-orgs" style="width:560px;max-height:80vh;display:flex;flex-direction:column;"><h2 class="pm-title">VibeNode Orgs</h2><div class="pm-body pf-obody" style="overflow:auto">Loading…</div><div class="pm-actions"><button type="button" class="pm-btn pm-btn-secondary" onclick="_closePm()">Close</button></div></div>';
    overlay.classList.add('show');
    requestAnimationFrame(function () { var c = overlay.querySelector('.pm-card'); if (c) c.classList.remove('pm-enter'); });
    overlay.onclick = function (e) { if (e.target === overlay && window._closePm) _closePm(); };
    drawOrgs(overlay.querySelector('.pf-obody'));
  }

  function drawOrgs(body) {
    api('/api/orgs/me').then(function (me) {
      if (!body.isConnected) return;
      if (!me.ok) { body.textContent = me.message || 'Could not read VibeNode Orgs.'; return; }
      if (!me.member) return drawSetup(body, me);
      drawMembers(body, me);
    });
  }

  function drawSetup(body, me) {
    if (!me.cloud_storage) {
      body.innerHTML = '<p>An org lives in your cloud storage, so turn it on first: <a href="#" class="pf-ps">Persistent Storage</a>.</p><p>Joining someone else\'s org needs nothing: paste the invite they sent you into any session.</p>';
      body.querySelector('.pf-ps').onclick = function (e) { e.preventDefault(); if (window.openPersistentStorage) openPersistentStorage(); };
      return;
    }
    body.innerHTML = '<p>Start an org so you and the people you add can Phone a Friend: open one session to one person until it ends or you revoke it.</p>'
      + '<label class="pf-lab">Org name</label><input class="pf-in" name="org" maxlength="80" placeholder="e.g. Build crew">'
      + '<label class="pf-lab">Your username in it</label><input class="pf-in" name="user" maxlength="32">'
      + (me.publishable_key_set ? '' : '<label class="pf-lab">Your cloud storage\'s publishable key (Supabase → Project Settings → API Keys)</label><input class="pf-in" name="pk" placeholder="sb_publishable_…">')
      + '<label class="pf-lab">Supabase access token, to add the Orgs tables for you (supabase.com/dashboard/account/tokens). Leave empty if you ran the setup SQL yourself.</label><input class="pf-in" name="tok" type="password" autocomplete="off">'
      + '<p class="pf-small"><a href="/api/orgs/setup-sql" target="_blank" rel="noopener">See the setup SQL</a></p>'
      + '<div class="pf-note"></div><button type="button" class="pf-go">Create org</button>';
    var go = body.querySelector('.pf-go');
    go.onclick = function () {
      var v = function (n) { var el = body.querySelector('[name="' + n + '"]'); return el ? el.value.trim() : ''; };
      if (!v('org') || !v('user')) { body.querySelector('.pf-note').textContent = 'Give the org a name and yourself a username.'; return; }
      go.disabled = true; go.textContent = 'Creating…';
      var payload = { org_name: v('org'), username: v('user') };
      if (v('pk')) payload.publishable_key = v('pk');
      if (v('tok')) payload.access_token = v('tok');
      api('/api/orgs/setup', { body: payload }).then(function (d) {
        go.disabled = false; go.textContent = 'Create org';
        if (!d.ok) { body.querySelector('.pf-note').textContent = d.message || d.error || 'Setup failed.'; return; }
        refresh(); drawOrgs(body);
      });
    };
  }

  function drawMembers(body, me) {
    body.innerHTML = '<div class="pf-oname">' + esc(me.org_name) + '</div><div class="pf-small">You are ' + esc(me.username) + ' (' + esc(me.role) + ')</div><div class="pf-mlist">Loading members…</div>'
      + ((me.role === 'owner' || me.role === 'admin')
        ? '<div class="pf-addbox"><div class="pf-lab">Add member: the invite works once and expires in</div><div class="pf-seg" data-k="days"><button type="button" data-v="1">1 day</button><button type="button" data-v="3" class="on">3 days</button><button type="button" data-v="7">7 days</button></div>'
          + '<button type="button" class="pf-go pf-mkinv">Add member</button><div class="pf-inv"></div></div>' : '')
      + '<div class="pf-acts" style="margin-top:16px"><button type="button" class="pf-ghost pf-leave">Leave org</button></div>';
    api('/api/orgs/members').then(function (d) {
      var box = body.querySelector('.pf-mlist'); if (!box) return;
      if (!d.ok) { box.textContent = d.message || 'Could not load members.'; return; }
      box.innerHTML = d.members.map(function (m) {
        var canRemove = !m.you && m.role !== 'owner' && (me.role === 'owner' || me.role === 'admin');
        return '<div class="pf-mrow"><span class="pf-av">' + esc(m.username.charAt(0).toUpperCase()) + '</span>' + esc(m.username) + (m.you ? ' <span class="pf-small">(you)</span>' : '') + '<span class="pf-meta">' + esc(m.role) + '</span>' + (canRemove ? '<button type="button" class="pf-ghost" data-rm="' + esc(m.member_id) + '" data-name="' + esc(m.username) + '">Remove</button>' : '') + '</div>';
      }).join('');
      box.querySelectorAll('[data-rm]').forEach(function (b) {
        b.onclick = function () {
          if (!confirm('Remove ' + b.dataset.name + ' from the org? Every call they are on ends now.')) return;
          api('/api/orgs/members/' + b.dataset.rm + '/remove', { body: {} }).then(function () { drawOrgs(body); refresh(); });
        };
      });
    });
    var days = 3;
    var seg = body.querySelector('.pf-seg[data-k="days"]');
    if (seg) seg.onclick = function (e) { var b = e.target.closest('button'); if (!b) return; days = +b.dataset.v; seg.querySelectorAll('button').forEach(function (x) { x.classList.toggle('on', x === b); }); };
    var mk = body.querySelector('.pf-mkinv');
    if (mk) mk.onclick = function () {
      mk.disabled = true;
      api('/api/orgs/invite', { body: { days: days } }).then(function (d) {
        mk.disabled = false;
        var inv = body.querySelector('.pf-inv');
        if (!d.ok) { inv.textContent = d.message || 'Could not make an invite.'; return; }
        inv.innerHTML = '<pre class="pf-prompt"></pre><button type="button" class="pf-go pf-copy">Copy invite</button> <span class="pf-small pf-copied"></span>';
        inv.querySelector('.pf-prompt').textContent = d.prompt;
        inv.querySelector('.pf-copy').onclick = function () {
          var done = function () { inv.querySelector('.pf-copied').textContent = 'Copied. Paste it in an email to your friend.'; };
          if (window.vnCopyText) Promise.resolve(vnCopyText(d.prompt)).then(done, done);
          else if (navigator.clipboard) navigator.clipboard.writeText(d.prompt).then(done);
        };
      });
    };
    body.querySelector('.pf-leave').onclick = function () {
      if (!confirm('Leave ' + me.org_name + '? Every call you gave or hold ends now.')) return;
      api('/api/orgs/leave', { body: {} }).then(function () { refresh(); drawOrgs(body); });
    };
  }

  // ── wiring ───────────────────────────────────────────────────────────────
  function bindSocket() {
    var s = sock();
    if (!s || !s.on || bindSocket.done) return;
    bindSocket.done = true;
    s.on('pf_incoming', onIncoming);
    s.on('pf_ended', function () { refresh(); });
    s.on('pf_suggestion', onSuggestion);
    s.on('pf_message', onMessage);
    s.on('pf_note', onNote);
    s.on('pf_notice', onNotice);
  }

  var _lastActive = null;
  setInterval(function () {
    bindSocket();
    if (curSid() !== _lastActive) { _lastActive = curSid(); paintHeader(); }
    if (isMember() && document.querySelector('.session-item[data-sid]')) { _paintedRows = ''; paint(); }
  }, 1500);
  setInterval(function () { if (!document.hidden) refresh(); }, 30000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) refresh(); });
  bindSocket();
  refresh();

  window.PF = {
    isMember: isMember, rowMark: rowMark, openCall: openCall, openList: openList, openOrgs: openOrgs,
    openFriend: openFriend, openShare: openShare, isPhoned: isPhoned, composerRows: composerRows, refresh: refresh,
    state: function () { return st; },
  };
  window.openVibeNodeOrgs = openOrgs;
})();
