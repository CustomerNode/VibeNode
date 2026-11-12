/**
 * Work trail: the steps between two messages, as one line on the thread's rail.
 *
 * With Smart termination on, a run of tool rows in the
 * thread is drawn as one line: icons that spin in as steps happen, what it is
 * doing now, a step count, and a written sentence once the run is over. Tapping
 * the line opens the steps in plain words; changes and failures open by
 * themselves, and any step still shows its original row.
 *
 * NOTHING MOVES. The rows renderLiveEntry draws stay exactly where they are in
 * #live-log, as siblings, and are only hidden. Everything that walks the thread
 * (the sent-back card, collapse, trim, ThreadScroll, the output shelf, Load
 * older) keeps seeing the DOM it always saw. The trail is an extra element in
 * front of each run. A run's rows are never parents of anything here.
 *
 * Words come from plain rules over the tool name and input. A command's words
 * come from the short description the session wrote for it, read from the
 * transcript by /api/session-steps; so do real changes with line numbers. If
 * that is unavailable the line falls back to a shortened command.
 */
(function () {
  'use strict';

  const MAX_ICONS = 6;
  const FETCH_DEBOUNCE_MS = 500;
  const FETCH_ATTEMPTS = 4;        // the transcript can lag the live row slightly
  const DETAIL_LINES = 40;

  let _log = null;
  let _mo = null;
  let _trails = new Map();         // run key -> trail
  let _sid = null;
  const _steps = new Map();        // tool_use id -> what the transcript knows
  const _tries = new Map();        // tool_use id -> fetch attempts
  let _noRoute = false;            // a web server not yet restarted onto the route
  let _fetchTimer = null;

  const _on = () => !!window.SessionVerb;
  // A session started under a temp id takes the CLI's id mid-turn; the panel's
  // own id is the one that stays right.
  const _cur = () => { try { return (typeof liveSessionId !== 'undefined' && liveSessionId) || _sid; } catch (_e) { return _sid; } };
  const _esc = s => (typeof escHtml === 'function' ? escHtml(String(s == null ? '' : s))
    : String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));

  /* ---------- what a step is, in plain words ---------- */
  const PICS = {
    look: '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="2.5"/>',
    search: '<circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.5" y2="16.5"/>',
    change: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
    test: '<path d="M9 3h6"/><path d="M10 3v6l-5 9a2 2 0 0 0 2 3h10a2 2 0 0 0 2-3l-5-9V3"/>',
    run: '<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>',
    agent: '<rect x="3" y="11" width="18" height="10" rx="2"/><circle cx="12" cy="5" r="2"/><line x1="12" y1="7" x2="12" y2="11"/>',
    other: '<circle cx="12" cy="12" r="3"/><circle cx="12" cy="12" r="9"/>',
  };
  const pic = k => '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + (PICS[k] || PICS.other) + '</svg>';

  const _TEST_CMD = /\b(pytest|jest|vitest|mocha|phpunit|rspec|unittest|go test|cargo test|npm (run )?test|yarn test|pnpm test|node tests?\/)/i;
  // A count a test tool printed. Anything else stays words.
  function testCount(text) {
    if (!text) return null;
    const tail = String(text).slice(-2000);
    const f = /(\d+) failed/.exec(tail), p = /(\d+) passed/.exec(tail);
    if (!f && !p) return null;
    const failed = f ? +f[1] : 0, passed = p ? +p[1] : 0;
    return { failed, passed, total: failed + passed };
  }
  const _base = s => { const m = /([^/\\]+)[/\\]?$/.exec(String(s || '')); return m ? m[1] : String(s || ''); };
  function _pathOf(desc) {
    return String(desc || '').replace(/^(file_path|notebook_path|path):\s*/, '').replace(/\s*\(write \d+ chars\)$/, '').trim();
  }
  function _short(cmd) {
    const c = String(cmd || '').replace(/\s+/g, ' ').trim().replace(/^cd \S+ && /, '');
    return c.length > 60 ? c.slice(0, 59) + '…' : c;
  }
  const _lower = s => s ? s.charAt(0).toLowerCase() + s.slice(1) : s;
  // The session writes a command's description as an order ("Run the tests").
  // Said back afterwards it reads "Ran the tests"; while running,
  // "Running the tests". Only for verbs known here: a description that starts
  // with anything else ("Git status") is left as written, never mangled.
  const _PAST = { run: 'ran', show: 'showed', take: 'took', find: 'found', build: 'built', read: 'read', get: 'got', write: 'wrote', make: 'made', set: 'set', put: 'put', send: 'sent', drive: 'drove', rerun: 'reran', reset: 'reset', rebuild: 'rebuilt', rewrite: 'rewrote', grab: 'grabbed', scan: 'scanned' };
  const _REGULAR = new Set(['add', 'apply', 'check', 'clean', 'commit', 'compare', 'copy', 'count', 'create', 'delete', 'download', 'fetch', 'fix', 'generate', 'inspect', 'install', 'kill', 'launch', 'list', 'load', 'look', 'measure', 'move', 'open', 'print', 'pull', 'push', 'remove', 'rename', 'render', 'replay', 'restart', 'restore', 'search', 'start', 'stage', 'stop', 'test', 'update', 'verify', 'view', 'watch',
    'save', 'capture', 'serve', 'wait', 'edit', 'close', 'reload', 'confirm', 'query', 'probe', 'simulate', 'retry', 'try', 'diff', 'record', 'replace', 'extract', 'collect', 'import', 'export', 'upload', 'clear', 'scroll', 'click', 'type']);
  function _verbForm(label, form) {
    const m = /^(\w+)(.*)$/s.exec(label || '');
    if (!m) return label;
    const w = m[1].toLowerCase();
    if (!_PAST[w] && !_REGULAR.has(w)) return label;
    let out;
    if (form === 'past') {
      if (_PAST[w]) out = _PAST[w];
      else if (w === 'stop') out = 'stopped';
      else if (w.endsWith('e')) out = w + 'd';
      else if (/[^aeiou]y$/.test(w)) out = w.slice(0, -1) + 'ied';
      else out = w + 'ed';
    } else {
      if (/^(run|rerun|get|set|reset|put|stop|grab|scan)$/.test(w)) out = w + w.slice(-1) + 'ing';
      else if (w.endsWith('e')) out = w.slice(0, -1) + 'ing';
      else out = w + 'ing';
    }
    // "Run the tests and drive the demo": every order in the line takes the
    // same form, not only the first ("Running the tests and drive the demo").
    const rest = m[2].replace(/((?:\b(?:and|then)|,) )(\w+)/g, (all, lead, v) => {
      const lw = v.toLowerCase();
      return (_PAST[lw] || _REGULAR.has(lw)) ? lead + _lower(_verbForm(lw, form)) : all;
    });
    return out.charAt(0).toUpperCase() + out.slice(1) + rest;
  }
  const _SEARCH = new Set(['Grep', 'Glob', 'WebSearch', 'WebFetch', 'ToolSearch']);
  const _CHANGE = new Set(['Edit', 'Write', 'MultiEdit', 'NotebookEdit']);
  const _LOOK = new Set(['Read', 'NotebookRead']);
  // A command, whatever shell ran it. On Windows sessions run PowerShell, and a
  // Bash-only check drew every one of their commands as "Used PowerShell".
  const _SHELL = new Set(['Bash', 'PowerShell']);
  // [verb, object, while doing]
  const _OTHER = {
    ListAgents: ['Checked', 'its helpers', 'Checking its helpers'],
    SendMessage: ['Sent', 'a helper a message', 'Messaging a helper'],
    TaskStop: ['Stopped', 'a background task', 'Stopping a background task'],
    TaskOutput: ['Read', 'a background task’s output', 'Reading a background task’s output'],
    BashOutput: ['Read', 'a background command’s output', 'Reading a background command’s output'],
    KillShell: ['Stopped', 'a background command', 'Stopping a background command'],
    Monitor: ['Watched', 'a background job', 'Watching a background job'],
    ScheduleWakeup: ['Set', 'a time to check back', 'Setting a time to check back'],
    Skill: ['Loaded', 'a skill', 'Loading a skill'],
    AskUserQuestion: ['Asked', 'you a question', 'Asking you a question'],
    ExitPlanMode: ['Finished', 'its plan', 'Finishing its plan'],
    EnterPlanMode: ['Started', 'a plan', 'Planning'],
  };

  // tool: { use: entry, result: entry|null }
  function describe(t) {
    const n = t.use.name || '', d = t.use.desc || '', info = _steps.get(t.use.id) || {};
    const err = !!(t.result && t.result.is_error);
    if (t.use.orphan) return { kind: 'other', verb: 'Finished', obj: 'a step from earlier', doing: 'Finishing a step from earlier', err };
    if (_LOOK.has(n)) { const f = _base(_pathOf(d)); return { kind: 'look', verb: 'Read', obj: f, doing: 'Reading ' + f, err }; }
    if (_SEARCH.has(n)) {
      const web = n.startsWith('Web');
      return { kind: 'search', verb: web ? 'Searched the web for' : 'Searched for', obj: _short(d), doing: web ? 'Searching the web' : 'Searching the code', searched: true, err };
    }
    if (_CHANGE.has(n)) { const f = _base(_pathOf(info.file || d)); return { kind: 'change', verb: n === 'Write' ? 'Wrote' : 'Changed', obj: f, doing: (n === 'Write' ? 'Writing ' : 'Changing ') + f, file: f, err }; }
    if (_SHELL.has(n)) {
      const label = info.label || '', count = testCount(t.result && t.result.text);
      const isTest = _TEST_CMD.test(d) || !!count;
      const words = label ? _verbForm(label, 'past') : _short(d);
      return { kind: isTest ? 'test' : 'run', verb: label ? '' : 'Ran', obj: words, doing: label ? _verbForm(label, 'doing') : 'Running ' + _short(d), cmd: d, label: words, count, err };
    }
    if (n === 'Agent' || n === 'Task') return { kind: 'agent', verb: 'Asked a helper:', obj: _short(d), doing: 'A helper is working', err };
    if (n === 'TodoWrite') return { kind: 'other', verb: 'Updated', obj: 'its to-do list', doing: 'Updating its to-do list', err };
    // Other tools the CLI has, in words; an unknown one keeps its name.
    const said = _OTHER[n];
    if (said) return { kind: 'other', verb: said[0], obj: said[1], doing: said[2], err };
    const mcp = /^mcp__(.+?)__(.+)$/.exec(n);
    if (mcp) { const what = mcp[2].replace(/_/g, ' ') + ' (' + mcp[1].replace(/^claude_ai_/, '').replace(/_/g, ' ') + ')'; return { kind: 'other', verb: 'Used', obj: what, doing: 'Using ' + what, err }; }
    return { kind: 'other', verb: 'Used', obj: n || 'a tool', doing: 'Using ' + (n || 'a tool'), err };
  }
  function changeCount(t) {
    const i = _steps.get(t.use.id);
    if (!i) return null;
    if (i.patch) {
      let a = 0, d = 0;
      i.patch.forEach(h => (h.lines || []).forEach(l => { if (l[0] === '+') a++; else if (l[0] === '-') d++; }));
      return { a, d };
    }
    if (i.added != null) return { a: i.added, d: 0 };
    return null;
  }
  function stepMeta(t, w) {
    if (w.kind === 'change') { const c = changeCount(t); return c ? '<span class="vt-num">+' + c.a + ' −' + c.d + '</span>' : ''; }
    if (w.count && w.count.total) {
      let dots = '';
      const nDots = Math.min(w.count.total, 20), nFail = Math.min(w.count.failed, nDots);
      for (let i = 0; i < nDots - nFail; i++) dots += '<i></i>';
      for (let i = 0; i < nFail; i++) dots += '<i class="f"></i>';
      const tx = w.count.failed ? w.count.failed + ' of ' + w.count.total + ' failed' : 'all ' + w.count.total + ' passed';
      return '<span class="vt-dots">' + dots + '</span><span class="vt-tx vt-num' + (w.count.failed ? '' : ' vt-good') + '">' + tx + '</span>';
    }
    if (w.err) return '<span class="vt-tx">failed</span>';
    return '';
  }

  /* ---------- what a run comes to ---------- */
  // A failed command counts as fixed when the same command later ran clean.
  function unresolved(tools) {
    const bad = new Map();
    tools.forEach(t => {
      if (!t.result) return;
      const key = t.use.name + '\u0000' + t.use.desc;
      if (t.result.is_error || (testCount(t.result.text) || {}).failed) bad.set(key, t);
      else bad.delete(key);
    });
    return [...bad.values()];
  }
  const _list = a => a.length < 2 ? (a[0] || '') : a.slice(0, -1).join(', ') + ' and ' + a[a.length - 1];
  const _uniq = a => [...new Set(a)];
  function sentence(tools) {
    const ws = tools.map(t => ({ t, w: describe(t) }));
    const acted = ws.some(x => x.w.kind !== 'look' && !x.w.searched);
    const parts = [];
    if (!acted) {
      const reads = _uniq(ws.filter(x => x.w.kind === 'look').map(x => x.w.obj));
      if (reads.length > 3) parts.push('read ' + reads.length + ' files');
      else if (reads.length) parts.push('read ' + _esc(_list(reads)));
      if (ws.some(x => x.w.searched)) parts.push('searched');
    }
    const files = new Map();
    ws.filter(x => x.w.kind === 'change').forEach(x => {
      const f = files.get(x.w.file) || { a: 0, d: 0, known: false };
      const c = changeCount(x.t); if (c) { f.a += c.a; f.d += c.d; f.known = true; }
      files.set(x.w.file, f);
    });
    if (files.size > 3) parts.push('changed ' + files.size + ' files');
    else if (files.size) parts.push('changed ' + _list([...files].map(([f, n]) => _esc(f) + (n.known ? ' <span class="vt-faint">+' + n.a + ' −' + n.d + '</span>' : ''))));

    const cmds = new Map();
    ws.filter(x => _SHELL.has(x.t.use.name)).forEach(x => { const k = x.w.cmd; if (!cmds.has(k)) cmds.set(k, []); cmds.get(k).push(x); });
    if (cmds.size > 3) {
      const bad = unresolved(ws.filter(x => _SHELL.has(x.t.use.name)).map(x => x.t)).length;
      parts.push('ran ' + cmds.size + ' commands' + (bad ? ': <span class="vt-warn">' + bad + ' failed</span>' : ''));
    } else {
      cmds.forEach(r => {
        const last = r[r.length - 1], firstBad = r.find(x => x.w.err || (x.w.count && x.w.count.failed));
        const name = _esc(r[0].w.verb ? 'ran ' + r[0].w.obj : _lower(r[0].w.label));
        const res = x => x.w.count ? (x.w.count.failed ? x.w.count.failed + ' of ' + x.w.count.total + ' failed' : 'all ' + x.w.count.total + ' passed') : (x.w.err ? 'failed' : 'passed');
        const lastBad = last.w.err || (last.w.count && last.w.count.failed);
        if (lastBad) parts.push(name + ': <span class="vt-warn">' + _esc(res(last)) + '</span>');
        else if (firstBad) parts.push(name + ': ' + _esc(res(firstBad)) + ', then <span class="vt-good">' + _esc(res(last)) + '</span>');
        else if (last.w.count) parts.push(name + ': ' + _esc(res(last)));
        else parts.push(name);
      });
    }
    const others = ws.filter(x => x.w.kind === 'agent' || x.w.kind === 'other');
    // Named while they fit ("watched a background job and stopped a background
    // task"); a count only past three different ones.
    const otherWords = _uniq(others.map(x => _lower(x.w.verb + ' ' + x.w.obj)));
    if (otherWords.length && otherWords.length <= 3) otherWords.forEach(s => parts.push(_esc(s)));
    else if (otherWords.length) parts.push('took ' + others.length + ' other steps');
    if (!parts.length) parts.push('searched');
    const s = _list(parts);
    return s.charAt(0).toUpperCase() + s.slice(1);
  }
  function nowDoing(tools) {
    for (let i = tools.length - 1; i >= 0; i--) if (!tools[i].result) return describe(tools[i]).doing + '…';
    return 'Thinking…';
  }

  /* ---------- what opens under a step ---------- */
  function diffHtml(info) {
    const row = (cls, n, sign, code) => '<div class="' + cls + '"><span class="n">' + n + '</span><span class="s">' + sign + '</span><span>' + _esc(code) + '</span></div>';
    let h = '';
    if (info.patch && info.patch.length) {
      info.patch.forEach((hunk, i) => {
        if (i) h += '<div class="gap"></div>';
        let n = hunk.newStart || 1;
        (hunk.lines || []).forEach(l => {
          if (l[0] === '+') h += row('add', n++, '+', l.slice(1));
          else if (l[0] === '-') h += row('del', '', '−', l.slice(1));
          else h += row('', n++, '', l.slice(1));
        });
      });
    } else if (info.old != null || info.new != null) {
      String(info.old || '').split('\n').forEach(l => { if (info.old) h += row('del', '', '−', l); });
      String(info.new || '').split('\n').forEach(l => { h += row('add', '', '+', l); });
    }
    return h ? '<div class="vt-diff">' + h + '</div>' : '';
  }
  function _clip(text) {
    const lines = String(text || '').split('\n');
    return lines.length > DETAIL_LINES ? lines.slice(0, DETAIL_LINES).join('\n') + '\n…' : lines.join('\n');
  }
  function detailHtml(t, w, rawOn) {
    const id = t.use.id;
    const link = '<button class="vt-rawlink" data-vt-raw="' + _esc(id) + '">' + (rawOn ? 'Hide the raw step' : 'Raw step') + '</button>';
    const info = _steps.get(id) || {};
    let body = w.kind === 'change' ? diffHtml(info) : '';
    let capt = '';
    if (!body) {
      const text = t.result ? t.result.text : '';
      if (w.cmd) capt = _short(w.cmd);
      else if (t.result && t.result.is_error) capt = 'What went wrong';
      body = t.result
        ? (text ? '<pre class="vt-plain">' + _esc(_clip(text)) + '</pre>' : '<div class="vt-plain vt-faint">Nothing came back.</div>')
        : '<div class="vt-plain vt-faint">Still going…</div>';
    }
    return '<div class="vt-det"><div class="vt-capt"><span>' + _esc(capt) + '</span>' + link + '</div>' + body +
      (rawOn ? '<div class="vt-raw" data-vt-rawbox="' + _esc(id) + '"></div>' : '') + '</div>';
  }

  /* ---------- one trail ---------- */
  const _isAuto = (t, w) => w.kind === 'change' || w.err || !!(w.count && w.count.failed);

  function makeTrail(key) {
    const el = document.createElement('div');
    el.className = 'vn-trail';
    el.innerHTML = '<button class="vt-bar" type="button"><span class="vt-stack"></span><span class="vt-roll vt-lab"><span></span></span>' +
      '<span class="vt-count"><span class="vt-roll vt-cnt"><span>0</span></span><span class="vt-cw">steps</span><span class="vt-ok"></span></span><span class="vt-chev">›</span></button>' +
      '<div class="vt-wrap"><div class="vt-in"><div class="vt-body"></div></div></div>';
    const tr = { key, el, tools: [], open: false, flipped: new Set(), raw: new Set(), live: false, count: 0, label: '', iconsFor: 0, wasOk: false };
    el.querySelector('.vt-bar').addEventListener('click', () => {
      tr.open = !tr.open;
      el.classList.toggle('vt-open', tr.open);
      if (tr.open) { paintBody(tr); _queueFetch(tr, true); }
      if (typeof _noteDetailToggle === 'function') _noteDetailToggle();
    });
    el.querySelector('.vt-body').addEventListener('click', ev => {
      const raw = ev.target.closest('[data-vt-raw]');
      if (raw) { const k = raw.dataset.vtRaw; tr.raw.has(k) ? tr.raw.delete(k) : tr.raw.add(k); paintBody(tr); return; }
      const row = ev.target.closest('[data-vt-step]');
      if (!row) return;
      const k = row.dataset.vtStep;
      tr.flipped.has(k) ? tr.flipped.delete(k) : tr.flipped.add(k);
      paintBody(tr);
    });
    return tr;
  }

  function roll(box, html, anim) {
    if (!anim) { box.innerHTML = '<span>' + html + '</span>'; return; }
    [...box.children].forEach(c => { c.className = 'vt-out'; setTimeout(() => c.remove(), 300); });
    const n = document.createElement('span'); n.className = 'vt-in-roll'; n.innerHTML = html; box.appendChild(n);
  }

  function paintBody(tr) {
    if (!tr.open) return;
    const body = tr.el.querySelector('.vt-body');
    let h = '';
    tr.tools.forEach(t => {
      const w = describe(t), id = t.use.id, auto = _isAuto(t, w);
      const isOpen = auto ? !tr.flipped.has(id) : tr.flipped.has(id);
      h += '<button class="vt-step' + (w.kind === 'change' ? ' change' : '') + (w.err || (w.count && w.count.failed) ? ' err' : '') + (isOpen ? ' open' : '') + '" type="button" data-vt-step="' + _esc(id) + '">' +
        '<span class="ic">' + pic(w.kind) + '</span><span class="tx"><span class="v">' + _esc(w.verb) + '</span> <span class="o">' + _esc(w.obj) + '</span></span><span class="mt">' + stepMeta(t, w) + '</span><span class="vt-chev">›</span></button>' +
        (isOpen ? detailHtml(t, w, tr.raw.has(id)) : '');
    });
    body.innerHTML = h;
    // The files the run wrote, as the same chips (their Open / Save buttons
    // are inline handlers, so a copy works like the original).
    if (tr.files && tr.files.length) {
      const box = document.createElement('div');
      box.className = 'vt-files';
      tr.files.forEach(f => { const c = f.cloneNode(true); c.classList.remove('vt-hidden'); box.appendChild(c); });
      body.appendChild(box);
    }
    // Anything else the run held (a CLI note, a system line), as a line of its
    // words; a failure is drawn as one.
    if (tr.notes && tr.notes.length) {
      const box = document.createElement('div');
      box.className = 'vt-notes';
      box.innerHTML = tr.notes.map(n => '<div class="vt-note' + (_noteErr(n) ? ' err' : '') + '">' + _esc(_noteText(n)) + '</div>').join('');
      body.appendChild(box);
    }
    // The step's own original rows, drawn by the same code as ever.
    if (typeof renderLiveEntry === 'function') {
      body.querySelectorAll('[data-vt-rawbox]').forEach(box => {
        const t = tr.tools.find(x => x.use.id === box.dataset.vtRawbox);
        if (!t) return;
        box.appendChild(renderLiveEntry(t.use));
        if (t.result) box.appendChild(renderLiveEntry(t.result));
      });
    }
  }

  function update(tr, tools, live, anim) {
    tr.tools = tools;
    const el = tr.el, trouble = unresolved(tools);
    el.classList.toggle('vt-trouble', trouble.length > 0 || (tr.notes || []).some(_noteErr));
    el.classList.toggle('vt-live', live);
    el.classList.toggle('vt-settled', !live);
    const stack = el.querySelector('.vt-stack');
    if (tools.length < tr.iconsFor) { stack.innerHTML = ''; tr.iconsFor = 0; }   // the page was cut shorter
    if (!anim) stack.querySelectorAll('.new').forEach(c => c.classList.remove('new'));   // a redraw must not replay them
    if (tools.length !== tr.iconsFor) {
      for (let i = Math.max(tr.iconsFor, tools.length - MAX_ICONS); i < tools.length; i++) {
        const w = describe(tools[i]), c = document.createElement('span');
        c.className = 'vt-chip ' + w.kind + (anim ? ' new' : '');
        c.innerHTML = pic(w.kind);
        stack.appendChild(c);
      }
      while (stack.children.length > MAX_ICONS) stack.firstChild.remove();
      tr.iconsFor = tools.length;
    }
    // A step that came back failed turns its icon amber, in place.
    const chips = [...stack.children], recent = tools.slice(-chips.length);
    chips.forEach((c, i) => {
      const w = describe(recent[i]);
      c.classList.toggle('err', !!(w.err || (w.count && w.count.failed)));
    });
    if (tools.length !== tr.count) {
      roll(el.querySelector('.vt-cnt'), String(tools.length), anim);
      el.querySelector('.vt-cw').textContent = tools.length === 1 ? 'step' : 'steps';
      tr.count = tools.length;
    }
    let label;
    if (live) {
      const warn = trouble.length ? '<span class="vt-warn">' + _esc(describe(trouble[trouble.length - 1]).obj) + ' failed</span> · ' : '';
      label = warn + _esc(nowDoing(tools));
    } else {
      label = sentence(tools);
    }
    if (label !== tr.label) {
      roll(el.querySelector('.vt-lab'), label, anim && live);
      tr.label = label;
    }
    const ok = !live && !trouble.length;
    if (ok !== tr.wasOk) {
      el.querySelector('.vt-ok').innerHTML = ok ? '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><polyline points="20 6 9 17 4 12"/></svg>' : '';
      el.querySelector('.vt-ok').classList.toggle('pop', ok && !!anim);
      tr.wasOk = ok;
    }
    paintBody(tr);
  }

  /* ---------- transcript lookups (plain words for commands, real changes) ---------- */
  function _needs(t) { return !_steps.has(t.use.id) && (_tries.get(t.use.id) || 0) < FETCH_ATTEMPTS; }
  let _pending = new Set();
  function _queueFetch(tr, opened) {
    if (_noRoute || !_sid) return;
    tr.tools.forEach(t => {
      const worth = opened || _SHELL.has(t.use.name) || _CHANGE.has(t.use.name);
      if (worth && _needs(t) && t.use.id) _pending.add(t.use.id);
    });
    if (_pending.size && !_fetchTimer) _fetchTimer = setTimeout(_flush, FETCH_DEBOUNCE_MS);
  }
  function _flush() {
    _fetchTimer = null;
    const ids = [..._pending].slice(0, 60), sid = _cur();
    if (!ids.length) return;
    ids.forEach(id => { _pending.delete(id); _tries.set(id, (_tries.get(id) || 0) + 1); });
    const proj = localStorage.getItem('activeProject') || '';
    fetch('/api/session-steps/' + encodeURIComponent(sid) + '?ids=' + encodeURIComponent(ids.join(',')) + (proj ? '&project=' + encodeURIComponent(proj) : ''))
      .then(r => {
        if (r.status === 404) { _noRoute = true; return null; }
        return r.ok ? r.json() : null;
      })
      .then(d => {
        if (!d || !d.steps || sid !== _cur()) return;
        Object.keys(d.steps).forEach(id => _steps.set(id, d.steps[id]));
        _trails.forEach(tr => update(tr, tr.tools, tr.live, false));
      })
      .catch(() => { /* words fall back to the shortened command */ })
      .finally(() => { if (_pending.size && !_fetchTimer) _fetchTimer = setTimeout(_flush, FETCH_DEBOUNCE_MS); });
  }

  /* ---------- the thread ---------- */
  function _clear() {
    if (!_log) return;
    _log.classList.remove('vn-trail-on');
    _log.querySelectorAll(':scope > .vn-trail').forEach(n => n.remove());
    _log.querySelectorAll(':scope > .vt-hidden').forEach(n => n.classList.remove('vt-hidden'));
    _trails = new Map();
  }
  function _working() {
    try { return typeof sessionKinds !== 'undefined' && sessionKinds[_cur()] === 'working'; } catch (_e) { return false; }
  }
  // What stays on screen between runs. Said: something said to or by the person; it
  // ends a run and settles it. Frame: the thread's own furniture; it ends a run.
  const _SAID = '.msg, .vn-sent-back, .vn-sb-group, .vn-auto-msg, .live-directive-conflict';
  const _FRAME = '.live-load-more, .vn-jump-wrap, .vn-thread-sentinel, .vs-verdict-strip, .session-role-banner, .subsession-inbox-strip, .subsession-actions-bar';
  // A stop pill is said; the CLI's note on a picture ("Image: original 780x2888 ...")
  // uses the same pill and is a step's output.
  const _said = n => n.matches(_SAID) || (n.classList.contains('live-interrupted') && !/^\s*Image:/.test(n.textContent || ''));
  const _frame = n => n.matches(_FRAME);
  // A folded row that is not a step, in words: its first line.
  const _noteText = n => { const l = n.querySelector('.live-result-line') || n; return (l.textContent || '').replace(/\s+/g, ' ').trim(); };
  const _noteErr = n => !!n.querySelector('.live-result-err') || n.classList.contains('live-result-err');

  /* ---------- several send-backs in a row: one line that adds them up ---------- */
  // Send-back rows with nothing between them but hidden rows and the thread's furniture get one
  // line in front ("Invalid termination response ×3 · too long, ..."); the
  // rows themselves stay where they are, hidden, and tapping the line shows
  // them in place, each still opening its own details. A single one is left
  // as it is.
  let _sbGroups = new Map();       // first row (the element) -> { el, open, members }
  const _sbOf = n => n.classList.contains('vn-sent-back');
  const _sbWhy = n => ((n.querySelector('.vn-sb-why') || {}).textContent || '').replace(/^\s*·\s*/, '').trim();
  function groupSentBacks() {
    if (!_log) return;
    const groups = [];
    let cur = null;
    for (let n = _log.firstElementChild; n; n = n.nextElementSibling) {
      if (n.classList.contains('vn-sb-group')) continue;
      if (_sbOf(n)) {
        if (!cur) { cur = []; groups.push(cur); }
        cur.push(n);
      } else if (!(n.classList.contains('vt-hidden') || _frame(n))) {
        cur = null;
      }
    }
    const want = groups.filter(g => g.length > 1);
    const firsts = new Set(want.map(g => g[0]));
    // Let go of groups that ended first, so a row moving to a new group keeps
    // the new group's marks.
    _sbGroups.forEach((grp, first) => {
      if (firsts.has(first) && first.isConnected) return;
      (grp.members || []).forEach(m => m.classList.remove('vsb-member', 'vsb-shut'));
      grp.el.remove(); _sbGroups.delete(first);
    });
    want.forEach(g => {
      let grp = _sbGroups.get(g[0]);
      if (!grp) {
        const el = document.createElement('div');
        el.className = 'vn-sb-group';
        grp = { el, open: false, members: [] };
        el.addEventListener('click', ev => {
          if (!ev.target.closest('.vn-sbg-head')) return;
          grp.open = !grp.open;
          paintGroup(grp);
          if (typeof _noteDetailToggle === 'function') _noteDetailToggle();
        });
        _sbGroups.set(g[0], grp);
      }
      (grp.members || []).forEach(m => { if (!g.includes(m)) m.classList.remove('vsb-member', 'vsb-shut'); });
      grp.members = g;
      if (grp.el.nextElementSibling !== g[0]) _log.insertBefore(grp.el, g[0]);
      paintGroup(grp);
    });
  }
  function paintGroup(grp) {
    const g = grp.members, n = g.length;
    const whys = _uniq(g.map(_sbWhy).filter(Boolean));
    const why = whys.slice(0, 2).join(', ') + (whys.length > 2 ? ' and ' + (whys.length - 2) + ' more' : '');
    const kept = g.filter(m => /kept working/.test((m.querySelector('.vn-sb-after') || {}).textContent || '')).length;
    const after = kept === n ? 'kept working' : 'rewrote it ' + (n - kept === 2 ? 'twice' : (n - kept) + ' times');
    const icon = (g[0].querySelector('.vn-sb-icon') || {}).outerHTML || '';
    grp.el.classList.toggle('open', grp.open);
    grp.el.innerHTML = '<button type="button" class="vn-sb-head vn-sbg-head" aria-expanded="' + grp.open + '">' +
      '<span class="vn-sbg-icon">' + icon + '<span class="vn-sbg-n">' + n + '</span></span>' +
      '<span class="vn-sb-main">Invalid termination response <span class="vn-sbg-x">×' + n + '</span> <span class="vn-sb-why">· ' + _esc(why) + '</span></span>' +
      '<span class="vn-sb-after">' + _esc(after) + '</span><span class="vn-sb-chev">›</span></button>';
    g.forEach(m => { m.classList.add('vsb-member'); m.classList.toggle('vsb-shut', !grp.open); });
  }

  function sync(anim) {
    if (!_log) return;
    groupSentBacks();
    if (!_on()) { if (_trails.size || _log.classList.contains('vn-trail-on')) _clear(); return; }
    _log.classList.add('vn-trail-on');
    // FOLD BY DEFAULT, so a new row type never slips through unfolded. Once a
    // run has started, EVERY row up to the next thing meant for the person is part of it and hidden, whatever
    // it is: a file chip, a CLI note, a system line, a row type added next
    // year. Only _said() and _frame() rows stay on screen. A new kind of row
    // that must stay visible is added to one of those lists; nothing else
    // needs to know about it. Guards: test_every_kind_the_renderer_draws_is_classified and the fold tests in tests/test_work_trail.py.
    const runs = [];
    let run = null;
    for (let n = _log.firstElementChild; n; n = n.nextElementSibling) {
      const e = n._vnEntry, c = n.classList;
      if (c.contains('vn-trail')) continue;
      if (_said(n) || _frame(n)) { run = null; continue; }
      if (!run) {
        // A system line that says nothing ("System message", the CLI's own
        // reminders) carries nothing for the person, in a run or not.
        if (!e && c.contains('live-entry-result') && _noteText(n) === 'System message') { n.classList.add('vt-hidden'); continue; }
        if (!e && !c.contains('live-entry-permission')) continue;   // before any step: not part of a run
        run = { first: n, members: [], uses: [], byId: new Map(), files: [], notes: [] }; runs.push(run);
      }
      run.members.push(n);
      if (e && e.kind === 'tool_use') { const t = { use: e, result: null }; run.uses.push(t); if (e.id) run.byId.set(e.id, t); }
      else if (e && e.kind === 'tool_result') {
        const t = run.byId.get(e.tool_use_id);
        if (t) t.result = e;
        else {
          // Its step is on an earlier page (Load older): still a step.
          const o = { use: { kind: 'tool_use', id: e.tool_use_id || '', name: '', desc: '', orphan: true }, result: e };
          run.uses.push(o); if (e.tool_use_id) run.byId.set(e.tool_use_id, o);
        }
      }
      else if (c.contains('live-output-card-inline')) run.files.push(n);
      else if (!c.contains('live-entry-permission')) run.notes.push(n);
    }
    const seen = new Set();
    runs.forEach((r, i) => {
      if (!r.uses.length) return;     // a stray result at the top of a page, or audit lines alone
      r.members.forEach(m => m.classList.add('vt-hidden'));
      const key = r.uses[0].use.id || ('i' + i);
      seen.add(key);
      let tr = _trails.get(key);
      if (!tr) { tr = makeTrail(key); _trails.set(key, tr); }
      if (tr.el.nextElementSibling !== r.first) _log.insertBefore(tr.el, r.first);
      // Live: the newest run, nothing said after it, and the session working.
      let after = r.members[r.members.length - 1].nextElementSibling, closed = false;
      for (; after; after = after.nextElementSibling) {
        if (_said(after)) { closed = true; break; }
      }
      const live = i === runs.length - 1 && !closed && _working();
      tr.live = live;
      tr.files = r.files;
      tr.notes = r.notes;
      update(tr, r.uses, live, !!anim);
      // Every run on the page asks for its commands' words, not only the last
      // few, so older lines never read as raw commands ("Ran curl.exe -s …").
      // Only commands and changes are asked for, 60 at a time, each once.
      _queueFetch(tr, false);
    });
    _trails.forEach((tr, key) => { if (!seen.has(key)) { tr.el.remove(); _trails.delete(key); } });
  }

  function attach(logEl, sid) {
    if (_mo) _mo.disconnect();
    if (sid !== _sid) { _steps.clear(); _tries.clear(); _pending = new Set(); }
    _log = logEl; _sid = sid; _trails = new Map(); _sbGroups = new Map();
    if (!logEl) return;
    _mo = new MutationObserver(records => {
      let added = 0, relevant = false;
      for (const r of records) {
        for (const n of r.addedNodes) {
          if (n.nodeType !== 1 || n.classList.contains('vn-trail') || n.classList.contains('vn-sb-group')) continue;
          relevant = true;
          if (n._vnEntry) added++;
        }
        for (const n of r.removedNodes) {
          if (n.nodeType === 1 && !n.classList.contains('vn-trail') && !n.classList.contains('vn-sb-group')) relevant = true;
        }
      }
      // A full render adds many in one go: that is the thread, not news.
      if (relevant) sync(added > 0 && added <= 3);
    });
    _mo.observe(logEl, { childList: true });
    sync(false);
  }

  window.WorkTrail = {
    attach,
    /** A state change: the newest trail goes live or settles. */
    refresh() { sync(true); },
    _sync: sync,
    _describe: describe,
    _steps,
    _testCount: testCount,
  };
})();
