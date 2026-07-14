/* search.js — deep search across ALL session transcripts (FTS5-backed).
 *
 * Entry point: the "Search transcripts" button in the sidebar (which
 * replaced the old sidebar title-filter input) calls openDeepSearch().
 * This opens a modal with its own search input — the ONE place you type —
 * and searches the full message content of every session in the active
 * project via GET /api/search.
 *
 * Query syntax: plain words search message content; tokens written as
 * file:<fragment> filter to sessions that EDITED a matching file path
 * (e.g. "retry logic file:session_manager.py").
 *
 * Distinct from find.js (find-in-current-transcript).
 */

let _dsDebounceTimer = null;
let _dsSeq = 0; // fetch sequence guard — stale responses are dropped
let _dsCurrentQ = ''; // free-text of the current query, for title highlighting

function openDeepSearch(prefill) {
  // Singleton modal — refocus instead of stacking a second overlay.
  const existing = document.getElementById('deep-search-overlay');
  if (existing) { existing.querySelector('#ds-input').focus(); return; }

  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.id = 'deep-search-overlay';
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:9999;display:flex;align-items:flex-start;justify-content:center;padding-top:9vh';
  overlay.innerHTML = `
    <div style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:10px;padding:18px 20px;width:640px;max-width:92vw;max-height:74vh;display:flex;flex-direction:column;color:var(--text-primary);font-family:inherit">
      <h3 style="margin:0 0 4px;font-size:16px;color:var(--text-heading)">Search transcripts</h3>
      <p style="margin:0 0 10px;font-size:12px;color:var(--text-muted)">Searches session titles and full message content across every session in this project. Use <code>file:name.py</code> to find sessions that edited a file.</p>
      <input type="text" id="ds-input" placeholder="Search all sessions&hellip; (add file:path to filter by edited file)"
        style="padding:8px 10px;border-radius:6px;border:1px solid var(--border);background:var(--bg-tertiary);color:var(--text-primary);font-size:13px;outline:none" autocomplete="off">
      <div id="ds-status" style="font-size:11px;color:var(--text-muted);margin:6px 2px 0;min-height:14px"></div>
      <div id="ds-results" style="overflow:auto;flex:1;margin-top:8px;min-height:40px"></div>
      <div style="display:flex;justify-content:flex-end;margin-top:12px">
        <button id="ds-close" style="padding:6px 16px;border-radius:6px;border:1px solid var(--border);background:transparent;color:var(--text-primary);cursor:pointer">Close</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  const input = overlay.querySelector('#ds-input');
  overlay.querySelector('#ds-close').onclick = () => overlay.remove();
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
  overlay.addEventListener('keydown', (e) => {
    // stopPropagation: don't let document-level Escape handlers (find bar,
    // other overlays) also react to the same keypress.
    if (e.key === 'Escape') { e.stopPropagation(); overlay.remove(); }
  });

  // Debounced search-as-you-type; Enter searches immediately.
  input.addEventListener('input', () => {
    clearTimeout(_dsDebounceTimer);
    _dsDebounceTimer = setTimeout(() => _dsRun(overlay), 350);
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); clearTimeout(_dsDebounceTimer); _dsRun(overlay); }
  });

  if (prefill) { input.value = prefill; _dsRun(overlay); }
  input.focus();
}

/* Split the raw input into {q, file}: tokens starting with "file:" become
 * the touched-file filter, everything else stays free text.  Only the FIRST
 * file: token is used -- the server does a single contains-match, so joining
 * several fragments would produce a filter that can never match. */
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

async function _dsRun(overlay) {
  const input = overlay.querySelector('#ds-input');
  const status = overlay.querySelector('#ds-status');
  const results = overlay.querySelector('#ds-results');
  const { q, file } = _dsParseQuery(input.value);

  if (!q && !file) { status.textContent = ''; results.innerHTML = ''; return; }
  if (q && q.length < 2 && !file) { status.textContent = 'Keep typing…'; return; }

  const seq = ++_dsSeq;
  // First search after startup builds the index (~1-2s on big projects).
  status.textContent = 'Searching… (first search may take a moment while indexing)';

  const proj = localStorage.getItem('activeProject') || '';
  const params = new URLSearchParams();
  if (q) params.set('q', q);
  if (file) params.set('file', file);
  if (proj) params.set('project', proj);

  let data;
  try {
    const resp = await fetch('/api/search?' + params.toString());
    // Parse defensively: a crash/proxy 500 may return non-JSON -- that must
    // report as a failed search, not fall into the network-error catch.
    try { data = await resp.json(); } catch (_) { data = null; }
    if (seq !== _dsSeq) return; // a newer query superseded this one
    if (!resp.ok) {
      status.textContent = (data && data.error) ? data.error : `Search failed (HTTP ${resp.status})`;
      results.innerHTML = '';
      return;
    }
    if (!data) { status.textContent = 'Search failed — bad response'; results.innerHTML = ''; return; }
  } catch (err) {
    if (seq !== _dsSeq) return;
    status.textContent = 'Search failed — server unreachable';
    results.innerHTML = '';
    return;
  }

  // Drop the response if the user switched projects while it was in flight --
  // rendering old-project sessions would deep-link into the wrong project.
  if ((localStorage.getItem('activeProject') || '') !== proj) return;

  const sessions = data.sessions || [];
  const st = data.stats || {};

  // Session titles aren't in the server-side content index, so fold them in
  // client-side from allSessions (same active project): flag title hits
  // among the content results (so the title gets highlighted) and append
  // title-only matches that the content search missed. Skipped when a
  // file: filter is active — that's an explicit "edited this file" query
  // and title-only additions would violate it.
  const ql = (q || '').trim().toLowerCase();
  _dsCurrentQ = ql;
  let merged = sessions;
  if (ql && !file) {
    const all = (typeof allSessions !== 'undefined') ? allSessions : [];
    const present = new Set(sessions.map(s => s.session_id));
    sessions.forEach(s => {
      const meta = all.find(x => x.id === s.session_id);
      if (meta && (meta.display_title || '').toLowerCase().includes(ql)) s._titleMatch = true;
    });
    const titleOnly = all
      .filter(m => !present.has(m.id) && (m.display_title || '').toLowerCase().includes(ql))
      .map(m => ({ session_id: m.id, snippets: [], files: [], _titleMatch: true, _titleOnly: true }));
    merged = sessions.concat(titleOnly);
  }

  status.textContent = merged.length
    ? `${merged.length} session${merged.length === 1 ? '' : 's'} · ${st.messages_indexed || 0} messages indexed · ${st.took_ms || 0} ms`
    : 'No matches';
  results.innerHTML = merged.map(s => _dsRenderRow(s)).join('');

  results.querySelectorAll('.deep-search-row').forEach(row => {
    row.onclick = () => {
      const sid = row.getAttribute('data-sid');
      overlay.remove();
      // selectSession (toolbar.js) — NOT handleSessionClick, which triggers
      // inline-rename when the clicked id is already the active session.
      if (typeof selectSession === 'function') selectSession(sid);
    };
  });
}

/* Render one result session: title (from the already-loaded allSessions
 * list — the API intentionally doesn't re-read titles server-side), date,
 * up to 3 highlighted snippets, and matched file paths when file-filtering. */
function _dsRenderRow(s) {
  const meta = (typeof allSessions !== 'undefined' ? allSessions : [])
    .find(x => x.id === s.session_id);
  const title = meta ? (meta.display_title || s.session_id) : s.session_id.slice(0, 8) + '…';
  const date = meta ? (meta.last_activity || meta.date || '') : '';
  // Highlight the query inside the title when it matched there.
  const titleHtml = s._titleMatch ? _dsHighlightTitle(title, _dsCurrentQ) : escHtml(title);
  const snips = (s.snippets || []).map(sn =>
    `<div class="deep-search-snip">${_dsHighlight(sn.text)}</div>`).join('');
  // Title-only matches have no content snippet — note why they're here.
  const titleTag = (s._titleOnly)
    ? `<div class="deep-search-snip" style="color:var(--text-muted)">Matches session title</div>` : '';
  const files = (s.files || []).slice(0, 4).map(f =>
    `<span class="deep-search-file">${escHtml(f)}</span>`).join('');
  return `
    <div class="deep-search-row" data-sid="${escHtml(s.session_id)}">
      <div class="deep-search-row-head">
        <span class="deep-search-title">${titleHtml}</span>
        <span class="deep-search-date">${escHtml(date)}</span>
      </div>
      ${snips}${titleTag}${files ? `<div class="deep-search-files">${files}</div>` : ''}
    </div>`;
}

/* Highlight every occurrence of the query substring inside a plain title.
 * Splits on the RAW string then escapes each part, so escaping never breaks
 * the match and injected content can't produce markup. */
function _dsHighlightTitle(title, q) {
  if (!q) return escHtml(title);
  const lower = title.toLowerCase();
  const ql = q.toLowerCase();
  let out = '', i = 0, idx = lower.indexOf(ql, i);
  if (idx === -1) return escHtml(title);
  while (idx !== -1) {
    out += escHtml(title.slice(i, idx));
    out += '<mark class="search-hit">' + escHtml(title.slice(idx, idx + ql.length)) + '</mark>';
    i = idx + ql.length;
    idx = lower.indexOf(ql, i);
  }
  return out + escHtml(title.slice(i));
}

/* Escape the snippet FIRST, then swap the server's [[HIT]] markers for
 * <mark> — raw transcript content can never inject markup this way. */
function _dsHighlight(text) {
  return escHtml(text || '')
    .split('[[HIT]]').join('<mark class="search-hit">')
    .split('[[/HIT]]').join('</mark>');
}
