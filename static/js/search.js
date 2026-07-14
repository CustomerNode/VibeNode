/* search.js — deep search IS the sidebar search.
 *
 * Typing in the sidebar #search box runs a full-content search over every
 * session transcript in the active project (GET /api/search, FTS5-backed).
 * The matching sessions render as rich result cards — title, date,
 * highlighted content snippets, and matched file paths — INLINE in the
 * normal list area (#session-list). There is no modal and no second search
 * box; the sidebar search box is the only entry point.
 *
 * filterSessions() (app.js) calls renderDeepSearchInto() whenever the box
 * is non-empty and falls back to the normal session list when it's cleared.
 *
 * Query syntax: plain words match message content; a file:<fragment> token
 * additionally restricts to sessions that EDITED a matching file path
 * (e.g. "retry logic file:session_manager.py").
 *
 * Distinct from find.js (find-in-current-transcript).
 */

let _dsFilterTimer = null;
let _dsFilterSeq = 0; // fetch sequence guard — stale responses are dropped

/* Split raw input into {q, file}: tokens starting with "file:" become the
 * touched-file filter, everything else stays free text.  Only the FIRST
 * file: token is used (the server does a single contains-match, so joining
 * several fragments would produce a filter that can never match). */
function _dsParseQuery(raw) {
  const words = (raw || '').trim().split(/\s+/).filter(Boolean);
  const q = [], file = [];
  for (const w of words) {
    if (w.toLowerCase().startsWith('file:')) {
      const frag = w.slice(5);
      if (frag) file.push(frag);
    } else {
      q.push(w);
    }
  }
  return { q: q.join(' '), file: file[0] || '' };
}

/* Entry point from filterSessions(): render deep-search results for the
 * current query into the list area.  If we already have results for this
 * exact query, show them; otherwise keep showing the last results (or a
 * "Searching…" placeholder) while a debounced fetch runs. */
function renderDeepSearchInto(rawQuery, q) {
  if (window._deepFilterQuery === q && window._deepFilterSessions) {
    _dsRenderCards(window._deepFilterSessions, window._deepFilterStats, false);
    return;
  }
  if (window._deepFilterSessions) {
    // Keep the previous results visible (marked "searching…") to avoid a
    // blank flash on every keystroke.
    _dsRenderCards(window._deepFilterSessions, window._deepFilterStats, true);
  } else {
    _dsRenderStatus('Searching all transcripts…');
  }
  scheduleDeepFilter(rawQuery);
}

/* Debounce the /api/search fetch so we don't hit the server on every
 * keystroke. */
function scheduleDeepFilter(rawQuery) {
  clearTimeout(_dsFilterTimer);
  _dsFilterTimer = setTimeout(() => runDeepFilter(rawQuery), 300);
}

/* Run the deep search and, if it's still the current query, stash the
 * results on window and re-render via filterSessions().  On any failure we
 * leave whatever is currently shown in place rather than blanking it. */
async function runDeepFilter(rawQuery) {
  const seq = ++_dsFilterSeq;
  const { q, file } = _dsParseQuery(rawQuery);
  if (!q && !file) return;
  if (q && q.length < 2 && !file) return; // too short to index-search meaningfully

  const proj = localStorage.getItem('activeProject') || '';
  const params = new URLSearchParams();
  if (q) params.set('q', q);
  if (file) params.set('file', file);
  if (proj) params.set('project', proj);

  let data;
  try {
    const resp = await fetch('/api/search?' + params.toString());
    // Parse defensively: a 500/proxy error may return non-JSON.
    try { data = await resp.json(); } catch (_) { data = null; }
    if (seq !== _dsFilterSeq) return;   // superseded by a newer query
    if (!resp.ok || !data) { _dsRenderStatus((data && data.error) || 'Search failed'); return; }
  } catch (err) {
    if (seq !== _dsFilterSeq) return;
    _dsRenderStatus('Search failed — server unreachable');
    return;
  }

  // Drop the response if the user switched projects or changed the query
  // while it was in flight.
  if ((localStorage.getItem('activeProject') || '') !== proj) return;
  const el = document.getElementById('search');
  if (!el || el.value.trim() !== rawQuery) return;

  window._deepFilterSessions = data.sessions || [];
  window._deepFilterStats = data.stats || {};
  window._deepFilterQuery = rawQuery.toLowerCase();
  _dsRenderCards(window._deepFilterSessions, window._deepFilterStats, false);
}

/* Render the result cards into #session-list. */
function _dsRenderCards(sessions, stats, searching) {
  const el = document.getElementById('session-list');
  if (!el) return;
  const st = stats || {};
  let head;
  if (sessions.length) {
    head = `${sessions.length} session${sessions.length === 1 ? '' : 's'}`
      + ` · ${st.messages_indexed || 0} messages · ${st.took_ms || 0} ms`
      + (searching ? ' · searching…' : '');
  } else {
    head = searching ? 'Searching all transcripts…' : 'No matches';
  }
  el.innerHTML =
    `<div class="deep-search-results">`
    + `<div class="deep-search-head">${escHtml(head)}</div>`
    + sessions.map(s => _dsRenderRow(s)).join('')
    + `</div>`;

  el.querySelectorAll('.deep-search-row').forEach(row => {
    row.onclick = () => {
      const sid = row.getAttribute('data-sid');
      // selectSession (toolbar.js) — NOT handleSessionClick, which triggers
      // inline-rename when the clicked id is already the active session.
      if (typeof selectSession === 'function') selectSession(sid);
    };
  });
}

/* Render a plain status message (searching / error) into #session-list. */
function _dsRenderStatus(msg) {
  const el = document.getElementById('session-list');
  if (!el) return;
  el.innerHTML =
    `<div class="deep-search-results"><div class="deep-search-head">${escHtml(msg)}</div></div>`;
}

/* Render one result session: title (from the already-loaded allSessions
 * list — the API intentionally doesn't re-read titles server-side), date,
 * up to 3 highlighted snippets, and matched file paths when file-filtering. */
function _dsRenderRow(s) {
  const meta = (typeof allSessions !== 'undefined' ? allSessions : [])
    .find(x => x.id === s.session_id);
  const title = meta ? (meta.display_title || s.session_id) : s.session_id.slice(0, 8) + '…';
  const date = meta ? (meta.last_activity || meta.date || '') : '';
  const snips = (s.snippets || []).map(sn =>
    `<div class="deep-search-snip">${_dsHighlight(sn.text)}</div>`).join('');
  const files = (s.files || []).slice(0, 4).map(f =>
    `<span class="deep-search-file">${escHtml(f)}</span>`).join('');
  return `
    <div class="deep-search-row" data-sid="${escHtml(s.session_id)}">
      <div class="deep-search-row-head">
        <span class="deep-search-title">${escHtml(title)}</span>
        <span class="deep-search-date">${escHtml(date)}</span>
      </div>
      ${snips}${files ? `<div class="deep-search-files">${files}</div>` : ''}
    </div>`;
}

/* Escape the snippet FIRST, then swap the server's [[HIT]] markers for
 * <mark> — raw transcript content can never inject markup this way. */
function _dsHighlight(text) {
  return escHtml(text || '')
    .split('[[HIT]]').join('<mark class="search-hit">')
    .split('[[/HIT]]').join('</mark>');
}

/* Enter forces the deep search immediately, skipping the debounce. */
(function () {
  const el = document.getElementById('search');
  if (!el) return;
  el.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      clearTimeout(_dsFilterTimer);
      runDeepFilter(el.value.trim());
    }
  });
})();
