/* All projects: one view of every project's sessions, chosen from the
 * project picker.  Each session is labelled with its project in the sidebar,
 * and the project becomes the root of the summary card's path (only in this
 * view).
 *
 * The view is a flag (localStorage.vnAllProjects = '1'); `activeProject`
 * stays a real project, the one of the session last opened, so every route
 * that takes ?project= keeps working.  Opening a session from another project
 * moves `activeProject` to it quietly (no teardown), see beforeOpen().
 *
 * Each session gets a pill: the project's initial in a small outlined tile,
 * then its name.  No project colours: colour on these cards means status.
 */
(function () {
  const FLAG = 'vnAllProjects';
  const ALL = '__all__';

  function on() { try { return localStorage.getItem(FLAG) === '1'; } catch (e) { return false; } }
  function set(v) { try { if (v) localStorage.setItem(FLAG, '1'); else localStorage.removeItem(FLAG); } catch (e) {} }

  function projects() { return (typeof _allProjects !== 'undefined' && Array.isArray(_allProjects)) ? _allProjects : []; }
  function find(enc) { return projects().find(p => p.encoded === enc) || null; }
  function name(enc) {
    const p = find(enc);
    if (p && typeof _projectShortName === 'function') return _projectShortName(p);
    return enc ? String(enc).split('-').filter(Boolean).pop() : '';
  }
  function encodeCwd(cwd) { return String(cwd || '').replace(/[\\/:_.]/g, '-'); }
  function projectOf(sid) {
    if (!sid || typeof SessionStore === 'undefined') return '';
    const s = SessionStore.get(sid);
    if (!s) return '';
    if (s._project) return s._project;
    // A row added by a live event (a new session) has no tag; its folder says.
    if (s.cwd) {
      const enc = encodeCwd(s.cwd).toLowerCase();
      const p = projects().find(x => x.encoded.toLowerCase() === enc);
      if (p) { s._project = p.encoded; return p.encoded; }
    }
    return '';
  }

  /** Make `enc` the active project at once, without the project-switch
   *  teardown: every route that takes ?project= then acts on that project. */
  function useProject(enc) {
    if (!enc || enc === localStorage.getItem('activeProject')) return null;
    localStorage.setItem('activeProject', enc);
    if (typeof socket !== 'undefined' && socket.io && socket.io.opts) socket.io.opts.query = { project: enc };
    return (typeof _syncActiveProject === 'function') ? _syncActiveProject(enc, 2).catch(() => {}) : null;
  }

  /** Any touch on a session (click, double-click, right-click, pin, open)
   *  first moves the active project to that session's project, so rename,
   *  pin, Standby, delete and the rest go to the right project. */
  function adopt(sid) {
    if (!on()) return null;
    return useProject(projectOf(sid));
  }

  /** The project a call about this session goes to: its own in this view. */
  function projectFor(sid) {
    const here = localStorage.getItem('activeProject') || '';
    return on() ? (projectOf(sid) || here) : here;
  }

  /** Key for per-project settings (session filters) while in this view. */
  function settingsKey() { return on() ? ALL : (localStorage.getItem('activeProject') || ''); }

  // ── Project chooser: the project picker, asking a question ──
  // New Session, Workflow and Subsessions work in one project, so in this
  // view they first ask which one.

  let _choice = null;
  function chooseProject(question, hint) {
    if (_choice) _choice.resolve(null);
    return new Promise(resolve => {
      _choice = { resolve, question, hint: hint || '' };
      if (typeof openProjectOverlay === 'function') openProjectOverlay();
      else { _choice = null; resolve(null); }
    });
  }
  function choosing() { return _choice; }
  /** Called by the picker: a row was picked (enc) or it was closed (null). */
  function finishChoice(enc) {
    const c = _choice;
    _choice = null;
    if (c) c.resolve(enc || null);
    return !!c;
  }

  /** New Session in this view: which project?  Returns false to cancel. */
  async function beforeNewSession() {
    if (!on()) return true;
    const enc = await chooseProject('New session in which project?', 'The session starts in that project\u2019s folder.');
    if (!enc) return false;
    const sync = useProject(enc);
    if (sync) await sync;
    return enc;
  }

  /** Workflow and Subsessions are one project's: pick it, then leave this view for it. */
  const PER_PROJECT_VIEWS = { kanban: 'Workflow', compose: 'Subsessions' };
  async function openPerProjectView(mode) {
    const enc = await chooseProject('Open ' + PER_PROJECT_VIEWS[mode] + ' for which project?',
      PER_PROJECT_VIEWS[mode] + ' shows one project at a time, so this leaves All projects.');
    if (!enc) return;
    set(false);
    if (typeof selectProjectFromOverlay === 'function') await selectProjectFromOverlay(enc);
    if (typeof setViewMode === 'function') setViewMode(mode);
  }

  // A snapshot asked for in this view covers every project (the web server
  // answers with all_projects; an older one ignores the flag, and socket.js
  // then keeps the other projects' states instead of wiping them).
  function patchSocket() {
    if (typeof socket === 'undefined' || !socket || socket._vnApPatched) return;
    const orig = socket.emit.bind(socket);
    socket.emit = function (ev, data, ...rest) {
      if (ev === 'request_state_snapshot' && on()) data = Object.assign({}, data || {}, { all_projects: true });
      return orig(ev, data, ...rest);
    };
    socket._vnApPatched = true;
  }
  window.addEventListener('DOMContentLoaded', patchSocket);

  /** A Response holding every project's sessions, each tagged with `_project`. */
  async function fetchAll(signal) {
    const list = projects();
    const parts = await Promise.all(list.map(async p => {
      for (let attempt = 0; attempt < 20; attempt++) {
        if (signal && signal.aborted) return [];
        try {
          const r = await fetch('/api/sessions?project=' + encodeURIComponent(p.encoded), { signal, cache: 'no-store' });
          if (r.ok) {
            const rows = await r.json();
            return (Array.isArray(rows) ? rows : []).map(s => Object.assign(s, { _project: p.encoded }));
          }
        } catch (e) { if (signal && signal.aborted) return []; }
        await new Promise(r => setTimeout(r, 750));
      }
      return [];
    }));
    return new Response(JSON.stringify([].concat(...parts)), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }

  /** Before a session opens: make its project the active one, without the
   *  project-switch teardown, so the thread, composer and wake use its folder. */
  async function beforeOpen(sid) {
    const sync = adopt(sid);
    if (sync) await sync;
  }

  const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  /** Put a project pill on each card and row just drawn: the project's
   *  initial in a small tile (as in the picker) and its name, in neutral grey.
   *  Runs after every render. */
  function decorate() {
    patchSocket();
    const roots = [document.getElementById('session-list'), document.getElementById('workforce-grid')].filter(Boolean);
    for (const root of roots) {
      root.classList.toggle('vn-ap', on());
      if (!on()) { root.querySelectorAll('.vn-ap-label').forEach(x => x.remove()); continue; }
      for (const row of root.querySelectorAll(':scope > .session-item[data-sid], :scope > .wf-card[data-sid]')) {
        row.querySelectorAll('.vn-ap-label').forEach(x => x.remove());
        const enc = projectOf(row.dataset.sid);
        const nameEl = row.querySelector('.session-col-name, .wf-name');
        if (!enc || !nameEl) continue;
        const nm = name(enc);
        const tag = document.createElement('span');
        tag.className = 'vn-ap-label';
        tag.title = nm;
        tag.innerHTML = '<span class="vn-ap-ic" aria-hidden="true">' + esc((nm.trim().charAt(0) || '?').toUpperCase()) + '</span><span class="vn-ap-nm">' + esc(nm) + '</span>';
        if (row.classList.contains('wf-card')) { nameEl.insertAdjacentElement('afterend', tag); continue; }
        // Wrap the bare title text so the pill keeps its place when the title is cut short.
        if (!nameEl.querySelector(':scope > .vn-ap-t')) {
          const t = Array.from(nameEl.childNodes).find(n => n.nodeType === 3 && n.textContent.trim());
          if (t) { const w = document.createElement('span'); w.className = 'vn-ap-t'; t.replaceWith(w); w.appendChild(t); }
        }
        const pin = nameEl.querySelector(':scope > .vn-pin');
        if (pin) nameEl.insertBefore(tag, pin); else nameEl.appendChild(tag);
      }
    }
  }

  /** The summary path's root in All projects: the session's project. */
  function pathRoot() {
    if (!on()) return '';
    const sid = (typeof liveSessionId !== 'undefined' && liveSessionId) || (typeof activeId !== 'undefined' && activeId) || '';
    const enc = projectOf(sid) || localStorage.getItem('activeProject') || '';
    return name(enc);
  }

  window.VNAllProjects = { ALL, on, set, name, projectOf, fetchAll, beforeOpen, decorate, pathRoot,
    adopt, useProject, projectFor, settingsKey, chooseProject, choosing, finishChoice, beforeNewSession,
    PER_PROJECT_VIEWS, openPerProjectView, patchSocket };
})();
