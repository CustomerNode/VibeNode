/* last-card.js — the approved last-message card ("Report" layout, 2026-10-27).
 *
 * A session ends its turn with a fenced ```handoff block (JSON, schema in
 * docs/plans/last-message-cards-spec.md; enforced by Smart termination,
 * daemon/handoff_rules.py).  markdown.js turns the block into a
 * `.vn-card-ph` placeholder holding the raw JSON; renderIn() replaces each
 * placeholder with the card.  Invalid JSON stays visible as code, never blank.
 *
 * The render functions are ported from the approved design page
 * (docs/plans/last-message-cards.reference.html, renderSkim) unchanged in
 * output, so the chat shows exactly what was approved.  Styles:
 * static/css/last-card.css.  No .replace() with a computed string anywhere.
 */
(function () {
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const inl = s => esc(s).replace(/\*\*(.+?)\*\*/g, (_, a) => '<b>' + a + '</b>').replace(/`([^`]+)`/g, (_, a) => '<code>' + a + '</code>');
  // Long text (a deliverable's body, an explain answer) is markdown: headings,
  // bullet and numbered lists, **bold**, *italic*, `code`.  Plain text draws
  // as paragraphs, as before (owner, 2026-10-28: "## The idea" and the list
  // markers showed raw in a notes deliverable).
  const inlx = s => inl(s).replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?!\*)/g, (_, a, b) => a + '<i>' + b + '</i>');
  // Tables (`| a | b |` + a `|---|---|` line), `---` rules and ``` fences
  // too (owner, 2026-10-28: a proposal's status table showed as raw pipes).
  const _tblSep = /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$/;
  const _cells = ln => {
    let t = ln.trim();
    if (t.startsWith('|')) t = t.slice(1);
    if (t.endsWith('|') && !t.endsWith('\\|')) t = t.slice(0, -1);
    return t.split(/(?<!\\)\|/).map(c => c.trim().replace(/\\\|/g, () => '|'));
  };
  function paras(s) {
    const out = [];
    let para = [], list = null;
    const endP = () => { if (para.length) out.push('<p>' + para.map(inlx).join('<br>') + '</p>'); para = []; };
    const endL = () => { if (list) out.push('<' + list.t + '>' + list.items.map(x => '<li>' + inlx(x) + '</li>').join('') + '</' + list.t + '>'); list = null; };
    const lines = String(s || '').replace(/\r\n?/g, () => '\n').split('\n');
    for (let i = 0; i < lines.length; i++) {
      const ln = lines[i];
      let m;
      if (!ln.trim()) { endP(); endL(); continue; }
      if ((m = /^\s{0,3}(`{3,}|~{3,})/.exec(ln))) {
        endP(); endL();
        const fence = m[1], code = [];
        for (i++; i < lines.length && !lines[i].trim().startsWith(fence); i++) code.push(lines[i]);
        out.push('<pre><code>' + esc(code.join('\n')) + '</code></pre>');
        continue;
      }
      if (ln.includes('|') && i + 1 < lines.length && lines[i + 1].includes('|') && lines[i + 1].includes('-') && _tblSep.test(lines[i + 1])) {
        endP(); endL();
        const head = _cells(ln);
        const align = _cells(lines[i + 1]).map(c => /^:-+:$/.test(c) ? 'center' : /-:$/.test(c) ? 'right' : '');
        const td = (tag, c, k) => '<' + tag + (align[k] ? ' style="text-align:' + align[k] + '"' : '') + '>' + inlx(c) + '</' + tag + '>';
        let h = '<div class="md-tbl"><table><thead><tr>' + head.map((c, k) => td('th', c, k)).join('') + '</tr></thead><tbody>';
        for (i += 2; i < lines.length && lines[i].trim() && lines[i].includes('|'); i++) {
          const cs = _cells(lines[i]);
          h += '<tr>' + head.map((_, k) => td('td', cs[k] || '', k)).join('') + '</tr>';
        }
        i--;
        out.push(h + '</tbody></table></div>');
        continue;
      }
      if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(ln)) { endP(); endL(); out.push('<hr>'); continue; }
      if ((m = /^\s{0,3}(#{1,4})\s+(.+)$/.exec(ln))) { endP(); endL(); const n = m[1].length + 2; out.push('<h' + n + '>' + inlx(m[2].replace(/\s+#+\s*$/, () => '')) + '</h' + n + '>'); continue; }
      const ul = /^\s*[-*•]\s+(.+)$/.exec(ln), ol = !ul && /^\s*\d{1,3}[.)]\s+(.+)$/.exec(ln);
      if (ul || ol) {
        endP();
        const t = ul ? 'ul' : 'ol';
        if (!list || list.t !== t) { endL(); list = { t, items: [] }; }
        list.items.push((ul || ol)[1]);
        continue;
      }
      if (list && /^\s{2,}\S/.test(ln)) { list.items[list.items.length - 1] += ' ' + ln.trim(); continue; }
      endL(); para.push(ln);
    }
    endP(); endL();
    return out.join('');
  }

  const I = {
    check: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>',
    x: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>',
    circ: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><circle cx="12" cy="12" r="8"/></svg>',
    dot: '<svg width="8" height="8" viewBox="0 0 8 8"><circle cx="4" cy="4" r="3" fill="currentColor"/></svg>',
    hand: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v6"/><path d="M12 16.5v.5"/></svg>',
    spin: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M21 12a9 9 0 1 1-6.2-8.56"/></svg>',
    block: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M5.6 5.6l12.8 12.8"/></svg>',
    doc: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/></svg>',
    chat: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
    alert: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><path d="M12 7v6M12 16.5v.1"/></svg>',
    info: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/></svg>',
    link: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 14a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1"/><path d="M14 10a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"/></svg>',
    file: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/></svg>',
    commit: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="12" cy="12" r="3.5"/><path d="M3 12h5.5M15.5 12H21"/></svg>',
    arrow: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M7 17 17 7M8 7h9v9"/></svg>',
    copy: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>',
    chev: '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><polyline points="9 6 15 12 9 18"/></svg>',
    mail: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/></svg>',
    eye: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>',
    expand: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/></svg>',
    pause: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><circle cx="12" cy="12" r="9.5"/><path d="M10 9v6"/><path d="M14 9v6"/></svg>',
    back: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/></svg>',
    close: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>',
    fwd: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg>',
    dl: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11M7 10l5 5 5-5"/><path d="M5 20h14"/></svg>',
    pen: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20h4L19 9l-4-4L4 16z"/></svg>',
  };

  /** The URL to show a review page in a frame, or '' when it can't be framed
   *  here: same-origin paths as they are; this machine's other ports as
   *  themselves when this page is open on this machine, else (the phone, which
   *  cannot reach them) through the preview proxy.
   *
   *  Framed as itself, another app on this machine keeps the person's sign-in
   *  and runs its own scripts. Through the proxy it can do neither: the proxy
   *  sends no cookies, and the page's script requests go to VibeNode. So a
   *  signed-in app (CustomerNode's live design trials, 2026-10-28: "live pages
   *  plug into the vibenode stuff") showed only its login.  The address is
   *  framed as given, never renamed: a server listening only on ::1 (Vite
   *  under "localhost") refuses 127.0.0.1, and a sign-in belongs to the host
   *  name it was made at.  127.0.0.1 and localhost are different sites to the
   *  browser, so a signed-in app is named the way VibeNode is opened
   *  (127.0.0.1: CustomerNode's trials). VibeNode itself under its other
   *  name is this page's own path. */
  const _LOOPBACK = /^(localhost|127\.0\.0\.1|\[::1\])$/;
  function _reviewSrc(url) {
    url = String(url || '').trim();
    if (/^\/(?!\/)/.test(url)) return url;
    let u;
    try { u = new URL(url); } catch (_e) { return ''; }
    if (!/^https?:$/.test(u.protocol)) return '';
    if (typeof location !== 'undefined' && u.host === location.host) return u.pathname + u.search + u.hash;
    if (_LOOPBACK.test(u.hostname)) {
      if (typeof location !== 'undefined' && _LOOPBACK.test(location.hostname || '')) {
        return u.port === location.port ? u.pathname + u.search + u.hash : url;
      }
      return '/api/preview/proxy?u=' + encodeURIComponent(url);
    }
    return '';
  }

  const TYPES = {
    done:        { cls: 't-done',        label: 'Done',        icon: I.check },
    needs_you:   { cls: 't-needs',       label: 'Needs you',   icon: I.hand },
    working:     { cls: 't-working',     label: 'Working',     icon: I.spin },
    blocked:     { cls: 't-blocked',     label: 'Blocked',     icon: I.block },
    deliverable: { cls: 't-deliverable', label: 'Deliverable', icon: I.doc },
    explain:     { cls: 't-explain',     label: 'Answer',      icon: I.info },
  };

  function fmtDur(ms) {
    ms = Math.max(0, Math.round(ms / 1000));
    const h = Math.floor(ms / 3600), m = Math.floor(ms % 3600 / 60), sec = ms % 60;
    return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(sec).padStart(2, '0');
  }

  function row(o) {
    const tag = o.href ? 'a' : 'button';
    return '<' + tag + ' class="lm-row' + (o.cls ? ' ' + o.cls : '') + '"' + (o.href ? ' href="' + esc(o.href) + '" target="_blank" rel="noopener"' : ' type="button"') + (o.attrs || '') + '>' +
      '<span class="tile">' + o.tile + '</span><span class="tx"><span class="tt">' + o.title + (o.rec ? '<span class="rec">Recommended</span>' : '') + '</span>' +
      (o.sub ? '<span class="ts' + (o.mono ? ' mono' : '') + '">' + o.sub + '</span>' : '') + '</span>' +
      '<span class="vb">' + (o.verb || '') + '</span></' + tag + '>';
  }
  // Every link that can be shown here opens FULL SCREEN over the chat, on the
  // phone too (a localhost page goes through the preview proxy): owner,
  // 2026-10-28, "what we really need is a click to fullscreen type thing".
  // A screenshot is the same row with its picture as the tile.
  // A file to download (same list as handoff_rules._DOWNLOAD_FILE): framing
  // it full screen showed nothing (2026-10-28, a presentation's .zip behind
  // "Try it"), so an older card that still points at one gets a Download link.
  const _DL_FILE = /\.(?:zip|tar|tgz|gz|bz2|xz|7z|rar|pdf|pptx?|key|odp|docx?|odt|rtf|xlsx?|ods|csv|tsv|epub|dmg|pkg|exe|msi|apk|deb|rpm|appimage|whl|iso|mp4|mov|m4v|webm|mkv|avi|mp3|m4a|wav|flac)(?:[?#].*)?$/i;
  const _isDownload = u => /^(\/(?!\/)|https?:\/\/)/.test(String(u || '').trim()) && _DL_FILE.test(String(u || '').trim());
  function dlRow(u, label, sub, cls) {
    u = String(u).trim();
    const name = decodeURIComponent((u.split(/[?#]/)[0].split('/').pop()) || 'file');
    return '<a class="lm-row lm-dlfile' + (cls ? ' ' + cls : '') + '" href="' + esc(u) + '" download="' + esc(name) + '">' +
      '<span class="tile">' + I.dl + '</span><span class="tx"><span class="tt">' + inl(label || name) + '</span>' +
      '<span class="ts mono">' + (sub == null ? esc(name) : sub) + '</span></span><span class="vb">' + I.dl + ' Download</span></a>';
  }
  function linkRow(l) {
    l = l || {};
    const title = l.label || l.url || '';
    if (!isImg(l) && _isDownload(l.url)) return dlRow(l.url, title);
    if (isImg(l)) {
      const src = _imgSrc(l.url);
      return row({ cls: 'shot', tile: '<img class="thumb" src="' + esc(src) + '" alt="" loading="lazy">', title: inl(title || 'Screenshot'),
        sub: 'Full screen', verb: 'View', attrs: ' data-view="' + esc(src) + '" data-img="1" data-title="' + esc(title || 'Screenshot') + '"' });
    }
    const ic = l.kind === 'file' ? I.file : l.kind === 'commit' ? I.commit : I.link;
    const view = _reviewSrc(l.url);
    if (view) {
      return row({ tile: ic, title: inl(title), sub: esc(l.url || ''), mono: true, verb: 'View',
        attrs: ' data-view="' + esc(view) + '" data-href="' + esc(l.url || '') + '" data-title="' + esc(title) + '"' });
    }
    return row({ tile: ic, title: inl(title), sub: esc(l.url || ''), mono: true, verb: 'Open ' + I.arrow, attrs: ' data-href="' + esc(l.url || '') + '"' });
  }
  // A screenshot or other picture in the card (links/look entry with kind
  // "image").  Owner, 2026-10-28: "any screenshots, links etc should go inside
  // the approved cards".  Only a page-relative or http(s) address is drawn.
  const isImg = l => !!(l && l.kind === 'image' && /^(\/(?!\/)|https?:\/\/|~\/|file:\/\/\/|[A-Za-z]:[\\/])/.test(String(l.url || '')));
  // Where the browser loads a card's picture from (2026-10-28: screenshots
  // given as a file path, "/tmp/shot.png", drew as broken images on every
  // device).  This app's own pages and API stay as they are; a file on this
  // machine goes through /api/card-image; another localhost port through the
  // preview proxy, so the phone can see it too.
  function _imgSrc(url) {
    url = String(url || '').trim();
    if (/^\/(api|static)\//.test(url)) return url;
    if (/^(\/(?!\/)|~\/|file:\/\/\/|[A-Za-z]:[\\/])/.test(url)) return '/api/card-image?path=' + encodeURIComponent(url);
    return _loopbackPic(url) || _reviewSrc(url) || url;
  }
  // A picture on another port of this machine comes through the proxy, on this
  // computer too (a page is framed as itself, see _reviewSrc): a picture needs
  // no sign-in, and a server that keeps its files to its own site
  // (Cross-Origin-Resource-Policy: same-origin, as CustomerNode's dev server
  // sends) refuses an <img> from VibeNode's port.
  function _loopbackPic(url) {
    let u;
    try { u = new URL(String(url || '').trim()); } catch (_e) { return ''; }
    if (!/^https?:$/.test(u.protocol) || !_LOOPBACK.test(u.hostname)) return '';
    if (typeof location !== 'undefined' && u.host === location.host) return '';
    return '/api/preview/proxy?u=' + encodeURIComponent(String(url).trim());
  }
  // Replies are real buttons in the card's colour, inside the "Your move"
  // band (design A, picked by the owner 2026-10-28 from
  // static/scratch/card-buttons.html): the recommended one (else the first)
  // is filled, the others outlined.  The hidden .tile keeps the key and .tt
  // the label for the Review overlay, which reads them from these buttons.
  function replyButtons(replies) {
    const rs = (replies || []).map(r => r || {});
    const primary = Math.max(0, rs.findIndex(r => r.recommended));
    const wide = rs.some(r => r.detail);
    return '<div class="lm-rows lm-ctas' + (wide ? ' wide' : '') + '">' + rs.map((r, i) =>
      '<button type="button" class="lm-row reply cta' + (i === primary ? ' primary' : '') + '" data-reply="' + esc(r.reply || r.label) + '">' +
        '<span class="tile">' + esc(r.key || String.fromCharCode(65 + i)) + '</span>' +
        '<span class="tx"><span class="tt"><span class="ck">' + I.check + '</span>' + inl(r.label) +
          (r.recommended && rs.length > 1 ? '<span class="rec">Recommended</span>' : '') + '</span>' +
          (r.detail ? '<span class="ts">' + inl(r.detail) + '</span>' : '') + '</span></button>').join('') + '</div>';
  }
  function copyRow(c) {
    return row({ tile: c.icon || I.copy, title: c.mono ? '<span style="font:600 12.5px var(--font-mono,monospace)">' + esc(c.title) + '</span>' : inl(c.title), sub: c.sub ? inl(c.sub) : '',
      verb: c.verb || (I.copy + ' Copy'), href: c.href, attrs: c.text != null ? ' data-copy="' + esc(c.text) + '"' : '' });
  }
  function list(items, icon) {
    return '<ul class="lm-list">' + items.map(it => {
      const o = typeof it === 'string' ? { what: it } : (it || {});
      const st = o.ok === false ? 'bad' : (icon || 'ok');
      const ic = st === 'bad' ? I.x : st === 'todo' ? I.circ : st === 'dot' ? I.dot : I.check;
      return '<li><span class="ic ' + st + '">' + ic + '</span><span class="w">' + inl(o.what) + (o.why ? '<span class="y">' + inl(o.why) + '</span>' : '') + '</span></li>';
    }).join('') + '</ul>';
  }
  function compareBlock(c) {
    if (!c) return '';
    if (c.rows) {
      return '<div class="lm-cmp-rows"><div class="r hd"><div class="k"></div><div class="b">' + esc(c.before_label || 'Before') + '</div><div class="a">' + esc(c.after_label || 'After') + '</div></div>' +
        c.rows.map(r => '<div class="r"><div class="k">' + inl(r.topic) + '</div><div class="b">' + inl(r.before) + '</div><div class="a">' + inl(r.after) + '</div></div>').join('') + '</div>';
    }
    const b = c.before || [], a = c.after || [];
    return '<div class="lm-cmp"><div class="side"><div class="sl">' + esc(c.before_label || 'Before') + '</div><ul>' + b.map(x => '<li>' + inl(x) + '</li>').join('') + '</ul></div>' +
      '<div class="side"><div class="sl">' + esc(c.after_label || 'After') + '</div><ul>' + a.map(x => '<li>' + inl(x) + '</li>').join('') + '</ul></div></div>';
  }
  function deliverableBlock(d) {
    if (!d) return '';
    // meta is {"To": …, "Subject": …}; a plain line or a list of lines is
    // drawn as such (a string used to be split into one row per character).
    const mrows = !d.meta ? [] : typeof d.meta === 'string' ? [['', d.meta]] : Array.isArray(d.meta) ? d.meta.map(v => ['', v])
      : typeof d.meta === 'object' ? Object.entries(d.meta) : [['', d.meta]];
    const mval = v => esc(v && typeof v === 'object' ? JSON.stringify(v) : v);
    const meta = mrows.filter(([, v]) => v != null && String(v).trim()).map(([k, v]) => k
      ? '<span class="mk">' + esc(k) + '</span><span class="mv' + (k.toLowerCase() === 'subject' ? ' subj' : '') + '">' + mval(v) + '</span>'
      : '<span class="ml">' + mval(v) + '</span>').join('');
    const metaHtml = meta ? '<div class="dv-meta">' + meta + '</div>' : '';
    if (d.meta && (typeof d.meta !== 'object' || Array.isArray(d.meta))) d = Object.assign({}, d, { meta: null });
    const mailto = d.kind === 'email' && d.meta ? 'mailto:' + encodeURIComponent(d.meta.To || '') + '?subject=' + encodeURIComponent(d.meta.Subject || '') + '&body=' + encodeURIComponent(d.body || '') : '';
    const fmt = dvFormat(d);
    // Files to download: one Download button per file, a real link with the
    // download attribute (2026-10-28: a presentation's zip handed over as a
    // Validate row framed the archive full screen and showed nothing).
    const files = (Array.isArray(d.files) ? d.files : []).filter(f => f && typeof f === 'object' && /^(\/(?!\/)|https?:\/\/)/.test(String(f.url || '').trim()));
    const fileRows = files.map(f => {
      const u = String(f.url).trim(), name = decodeURIComponent((u.split(/[?#]/)[0].split('/').pop()) || 'file');
      if (f.copy === true && /^\/static\/scratch\/[^?#]+\.(?:txt|md|json|py|js|css|html?|csv|log)$/i.test(u)) {
        return row({ cls: 'loading', tile: I.copy, title: inl(f.label || name), sub: [f.size, name].filter(x => x != null && String(x).trim()).map(x => esc(String(x))).join(' · '),
          verb: (_shareFiles() ? I.copy + ' Copy .txt' : I.dl + ' Save .txt'), attrs: ' data-copy-src="' + esc(u) + '" aria-disabled="true"' });
      }
      return dlRow(u, f.label || name, [f.size, name, f.note].filter(x => x != null && String(x).trim()).map(x => esc(String(x))).join(' · '));
    }).join('');
    const filesHtml = fileRows ? '<div class="lm-rows lm-dlfiles">' + fileRows + '</div>' : '';
    if (!String(d.body || '').trim()) return filesHtml + metaHtml;
    // The first copy row holds the body; the full-screen view reads it there.
    const acts = [copyRow({ title: fmt === 'html' ? 'Copy the HTML' : 'Copy the text', sub: fmt === 'html' ? 'The page’s source, ready to paste' : 'The body only, ready to paste', text: d.body || '' })];
    if (d.meta && d.meta.Subject) acts.push(copyRow({ title: 'Copy with the subject line', sub: 'Subject first, then the body', text: 'Subject: ' + d.meta.Subject + '\n\n' + (d.body || '') }));
    if (mailto) acts.push(copyRow({ icon: I.mail, title: 'Open it in Mail', sub: 'A new email with To, Subject and body filled in', href: mailto, verb: 'Open ' + I.arrow }));
    // A capped preview of the rendered deliverable IS the way in: tapping it
    // opens the full-screen view (owner, 2026-10-28: "a max heighted preview
    // of the rendered content that is effectively then the same click to
    // expand mechanism", and no separate Open button beside it: "idk why
    // youd have the click button in addition to the preview").  HTML is
    // previewed in a frame with an empty sandbox: no scripts, no same-origin.
    const ttl = d.title || d.kind || 'Deliverable';
    const inner = fmt === 'html'
      ? '<iframe class="dv-pvframe" sandbox="" tabindex="-1" aria-hidden="true" srcdoc="' + esc(d.body || '') + '"></iframe>'
      : '<div class="dv-body">' + dvBodyHtml(fmt, d.body || '') + '</div>';
    const open = '<div class="dv dv-pv lm-dvopen dv-' + fmt + '" role="button" tabindex="0" aria-label="' + esc('Open ' + ttl + ' full screen') + '"' +
      ' data-dv="' + fmt + '" data-title="' + esc(ttl) + '">' +
      '<div class="dv-top"><span class="k">' + _DV_LABEL[fmt] + '</span><span class="n">' + inl(d.title || '') + '</span><span class="fx">' + I.expand + '</span></div>' +
      '<div class="dv-pvbox">' + inner + '</div></div>';
    return filesHtml + open + metaHtml + '<div class="lm-rows">' + acts.join('') + '</div>';
  }
  /** A deliverable's body drawn as its format, for the full-screen view.
   *  HTML is not drawn here: it goes in a sandboxed frame. */
  function dvBodyHtml(fmt, body) {
    return fmt === 'text' ? '<div class="dv-plain">' + esc(body) + '</div>' : paras(body);
  }
  /** "markdown" (the default), "html" or "text". */
  function dvFormat(d) {
    const f = String((d && d.format) || '').trim().toLowerCase();
    return f === 'html' ? 'html' : (f === 'text' || f === 'plaintext' || f === 'plain') ? 'text' : 'markdown';
  }
  const _DV_LABEL = { markdown: 'Formatted text', html: 'Web page', text: 'Plain text' };

  // A Review card's Next band ends with three answers, each saying what it
  // means (owner, 2026-10-28: "a cta to mark session done", then three
  // options, then "put it into next ... more clearly"; picked B of
  // static/scratch/card-verdict-next.html).  Hidden until _syncDone() finds
  // it on the open session's newest card while that session waits for a
  // verdict.
  // Read (an answer, a report) or Send (a text to send) for a card with
  // nothing asked; the sidebar's own rule when session-verb.js is loaded.
  function _cardVerb(m) {
    const core = window.SessionVerbCore;
    if (core && typeof core.verbFromCard === 'function') return core.verbFromCard(m) || '';
    if (m.type === 'explain') return 'read';
    if (m.type === 'deliverable' && Array.isArray((m.deliverable || {}).files) && m.deliverable.files.length) return 'read';
    if (m.type === 'deliverable') return /report|doc|summary|notes|analysis|audit|findings|plan|research/i.test(String((m.deliverable || {}).kind || '')) ? 'read' : 'send';
    return '';
  }
  function verdictRows(offer) {
    // title: the meaning line, for the Your verdict chips that hide it.
    const r = (act, ic, t, s, attrs) => '<button type="button" class="v-' + act + '" data-verdict="' + act + '"' + (attrs || '') + ' title="' + esc(String(s).replace(/<[^>]*>/g, '')) + '"><span class="ic">' + ic + '</span>' +
      '<span><span class="tt">' + t + '</span><span class="ts">' + s + '</span></span><span class="go">' + I.chev + '</span></button>';
    // An answer (Read) and a text to send (Send) get their own rows, not the
    // finished-fix ones (owner's pick A, 2026-10-28,
    // static/scratch/card-next-cta.html: "Problem: not done, needs
    // remediation" made no sense under an answer or an email).  Read: Got it
    // / Do what it suggested (only with the card's "offer") / Ask more /
    // Standby.  Send: Sent it / Change it / Standby.
    offer = typeof offer === 'string' ? offer.trim() : '';
    // One rule for every set (owner, 2026-10-28): the answer the card expects
    // first, other ways to keep going next, Standby last.  An offer is the
    // expected answer, so: Do what it suggested · Ask more · Got it · Standby.
    // Without one, Got it leads.  Got it is drawn exactly once.
    const gotit = r('gotit', I.check, 'Got it', 'Answered. Put the session to sleep');
    const own = (offer ? r('offer', I.fwd, 'Do what it suggested', inl(offer), ' data-offer="' + esc(offer) + '"') : gotit) +
      r('sent', I.check, 'Sent it', 'Put the session to sleep') +
      r('ask', I.chat, 'Ask more', 'A follow-up question in the chat box') +
      (offer ? gotit : '') +
      r('change', I.pen, 'Change it', 'Tell it what to change');
    // Which rows show depends on the session's status (_syncDone sets
    // data-state): "verdict" (Review / Read / Send), "question" (Decide /
    // Unblock: "Not answering now?"), "standby".  Standby, owner 2026-10-28,
    // static/scratch/standby-status.html option A.  Standby comes after the
    // verdicts, never first (owner, 2026-10-28: "i dont like standby as the
    // first option").
    return '<div class="lm-finish" hidden><div class="lm-fq">Not answering now?</div><div class="lm-verdict">' + own +
      r('done', I.check, 'Done', 'Put the session to sleep') +
      r('next', I.chat, 'Something else', 'Done, but keep chatting') +
      r('bad', I.alert, 'Problem', 'Not done, needs remediation') +
      r('standby', I.pause, 'Standby', 'Not now: keep it on my list. Chatting with it wakes it') +
      r('unpark', I.hand, 'Back to Needs you', 'Take it off Standby') + '</div>' +
      '<div class="lm-vdone">' + I.check + '<span>Session done</span><button type="button" class="u" data-verdict="undo">Undo</button></div></div>';
  }

  // "Left waiting": earlier work in the session that this card did not
  // finish, with one button that sends the session back to it (owner's pick
  // C, 2026-10-28, static/scratch/card-back-to.html: "accept this and return
  // to preceding unfinished work").  Card field:
  // "back_to": {"label", "status", "reply"}.  Shown on the newest card only
  // (_syncDone).
  function backToStrip(b) {
    if (!b || typeof b !== 'object' || typeof b.label !== 'string' || !b.label.trim()) return '';
    const reply = (typeof b.reply === 'string' && b.reply.trim()) ? b.reply.trim() : 'That’s done. Back to ' + b.label.trim() + '.';
    return '<div class="lm-backto"><span class="ic">' + I.back + '</span><span class="tx"><span class="tt">Left waiting: ' + inl(b.label) + '</span>' +
      (b.status ? '<span class="ts">' + inl(b.status) + '</span>' : '') + '</span>' +
      '<button type="button" class="go" data-back="' + esc(reply) + '">Back to it</button></div>';
  }

  // "Also found": something the session noticed outside the user's ask that
  // it may not, or should not, act on alone (a permission rule it would have
  // to change, a risk in code it was not asked to touch).  Neither unfinished
  // work nor a decision, so it never has to be dressed up as either to pass
  // the end-of-turn check (2026-10-29: a side finding about Windows delete
  // commands became an Approve card in jargon because the rules had no other
  // place for it).  Card field: "finding": {"what", "risk", "recommend",
  // "reply"}.  One quiet band after Next, one button, "Deal with it", that
  // sends finding.reply (or a default naming the finding) as the user's
  // message.  The button acts only on the newest card (_syncDone), like the
  // verdict chips; the text stays on older cards.
  function findingBand(f) {
    if (!f || typeof f !== 'object' || typeof f.what !== 'string' || !f.what.trim()) return '';
    const what = f.what.trim();
    const reply = (typeof f.reply === 'string' && f.reply.trim()) ? f.reply.trim() : 'Deal with what you found: ' + what;
    // Same shape as render()'s band(): a .lm-dl with its dt and dd.
    return '<div class="lm-dl lm-quiet lm-findband"><div class="dt">Also found</div><div class="dd">' +
      '<div class="lm-find"><span class="ic">' + I.info + '</span><span class="tx"><span class="tt">' + inl(what) + '</span>' +
      (typeof f.risk === 'string' && f.risk.trim() ? '<span class="ts"><b>Risk:</b> ' + inl(f.risk.trim()) + '</span>' : '') +
      (typeof f.recommend === 'string' && f.recommend.trim() ? '<span class="ts"><b>Recommends:</b> ' + inl(f.recommend.trim()) + '</span>' : '') +
      '</span><button type="button" class="go" data-find="' + esc(reply) + '">Deal with it</button></div></div></div>';
  }

  /** The approved Report card.  opts.clock: epoch ms the card was written. */
  function render(m, opts) {
    opts = opts || {};
    // A Decide card shows its choices once: a picture labelled with a choice
    // ("A: …", "Option B", or the choice's own label) goes inside Decide with
    // that choice, not into a Screens band beside it (owner, 2026-10-28:
    // "screens and decide, both with the same choices, confused the fuck out of me").
    const optImg = _optionImages(m);
    const t = Object.assign({}, TYPES[m.type] || TYPES.explain);
    if (m.late) { t.cls = 'tc-late'; t.label = 'Working · late'; }
    const band = (label, body, cls) => '<div class="lm-dl' + (cls ? ' ' + cls : '') + '"><div class="dt">' + label + '</div><div class="dd">' + body + '</div></div>';
    // A short title (at most 32 characters) drawn big, the headline under it
    // as the subtitle, the eyebrow only the status icon, label and time, and
    // no "Working on" line; scrolled past, a slim bar keeps the title and a
    // jump to the card's Next / Decide band (owner, 2026-10-28: option C of
    // static/scratch/card-title.html without the colour rail or the project
    // name).  A card without a title draws as before.
    const ti = typeof m.title === 'string' && m.title.trim() ? m.title.trim() : '';
    let h = '<div class="lm skim ' + t.cls + (ti ? ' lm-titled' : '') + '">';
    // The bar's jump goes to what the card asks of you, so only a card that
    // asks has one; a "Next" jump on a Done card led to "Nothing needed"
    // (owner, 2026-10-28: "that makes no sense").
    const nk = (m.needs || {}).kind;
    const jl = nk && nk !== 'none' ? (({ decision: 'Decide', approve: 'Approve', action: 'To do' })[nk] || 'Needed from you') : '';
    // A card that asks nothing shows its status there instead (owner: "its
    // missing the status on the done sticky").
    if (ti) h += '<div class="lm-stick" aria-hidden="true"><div class="in"><b>' + inl(ti) + '</b>' + (jl ? '<button type="button" class="jump" tabindex="-1">' + esc(jl) +
      ' <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.8" stroke-linecap="round"><path d="M12 5v14M6 13l6 6 6-6"/></svg></button>'
      : '<span class="st">' + t.icon + esc(m.pill || t.label) + '</span>') + '</div></div>';
    // "Explain more": the same card written out in plain words with no length
    // limit, for when the short card is not enough (owner's pick B of
    // static/scratch/card-lost.html, 2026-10-28): under the This task / Just
    // now lines, the card's quiet grey on every status, no icon.
    const ex = typeof m.explained === 'string' && m.explained.trim() ? m.explained.trim() : '';
    const exH = ex ? '<div class="lm-xpl"><button type="button" class="lm-xpl-tg" aria-expanded="false">Explain more <span class="tg">▾</span></button>' +
      '<div class="lm-xpl-body"><div><div class="lm-xpl-in">' + dvBodyHtml('markdown', ex) + '</div></div></div></div>' : '';
    // The way to the thing the title names (owner, 2026-10-28,
    // static/scratch/card-title-crumb.html): the title is the last step of
    // the path, so the small line above it holds the steps before it.
    const pth = Array.isArray(m.path) ? m.path.filter(x => typeof x === 'string' && x.trim()).map(x => x.trim()) : [];
    const pathH = pth.length ? '<div class="lm-path">' + pth.map(x => '<span class="s">' + inl(x) + '</span><span class="sep">›</span>').join('') + '</div>' : '';
    h += '<div class="lm-head"><div class="lm-pillrow"><span class="lm-pill">' + t.icon + esc(m.pill || t.label) + '</span>' + (opts.when ? '<span class="lm-when">' + esc(opts.when) + '</span>' : '') + '</div>' +
      (ti ? pathH + '<h3 class="lm-headline lm-title' + (/^[BDEFHIKLMNPRbhiklmnpr]/.test(ti) ? ' stem' : '') + '">' + inl(ti) + '</h3><p class="lm-subtitle">' + inl(m.headline) + '</p>'
          : '<h3 class="lm-headline">' + inl(m.headline) + '</h3>' + (m.context ? '<p class="lm-context"><b>Working on</b> ' + inl(m.context) + '</p>' : '')) +
      // Explain more belongs to the top block, with no rule above it (owner,
      // 2026-10-28: "should be integrated into the crumbs title subtitle block").
      exH + '</div>';
    // Big picture / this task / just now: three lines under the title (the
    // owner's pick, 2026-10-28, static/scratch/card-zoom.html option A), drawn
    // as a nested dial ladder with the label on the same line
    // (static/scratch/zoom-icons.html option J).  The dial's filled part says
    // how far zoomed in the line is: outer ring, inner ring, centre dot.
    const zm = (m.zoom && typeof m.zoom === 'object') ? m.zoom : null;
    let histH = '';
    if (zm && (zm.big || zm.task || zm.now)) {
      const dial = lvl => '<svg class="zd" width="13" height="13" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="10"' + (lvl === 0 ? ' class="on"' : '') +
        '/><circle cx="12" cy="12" r="5.5"' + (lvl === 1 ? ' class="on"' : '') + '/><circle class="dot' + (lvl === 2 ? ' on' : '') + '" cx="12" cy="12" r="2.2"/></svg>';
      const zr = (k, label, lvl) => zm[k] ? '<div class="r z' + lvl + '"><div class="nst">' + dial(lvl) + '<span class="lbl">' + label + '</span><span class="t">' + inl(zm[k]) + '</span></div></div>' : '';
      // This task / Just now (with the earlier tasks and the queued-messages
      // line that attach to them) are drawn at the bottom of the card in a
      // Session history section (owner, 2026-10-28, card-title-crumb.html);
      // Explain more stays at the top, under the sentence.
      histH = band('History', '<div class="lm-zoom">' + zr('big', 'Big picture', 0) + zr('task', 'This task', 1) + zr('now', 'Just now', 2) + '</div>', 'lm-hist');
    }

    if (m.late) {
      h += band('Late', '<p class="lm-apol">' + inl(m.late.apology) + '</p><dl class="lm-kv">' +
        '<dt>Why</dt><dd>' + inl(m.late.why) + '</dd>' +
        '<dt>New time</dt><dd><b>' + esc(m.late.new_eta_minutes) + ' more minutes</b>' + (m.late.confidence ? ' · ' + inl(m.late.confidence) : '') + '</dd></dl>', 'lm-sec lm-late');
    }
    const nn = m.needs || {};
    if (nn.eta_minutes && opts.clock) {
      const eta = Number(nn.eta_minutes) * 60000;
      h += band('Timing', '<div class="tm" data-clock="' + esc(opts.clock) + '" data-eta="' + eta + '">' +
        '<div class="tm-figs"><div class="f"><span class="n" data-f="since">0:00</span><span class="l">since it stopped</span></div>' +
        '<div class="f"><span class="n">' + fmtDur(eta) + '</span><span class="l">it said it would take</span></div>' +
        '<div class="f dl"><span class="n" data-f="delta">' + fmtDur(eta) + '</span><span class="l" data-f="dlabel">left</span></div></div>' +
        '<div class="tm-bar"><i class="fill"></i><i class="over"></i><i class="mark"></i></div></div>', 'lm-sec lm-timing');
    }

    const st = Object.assign({}, m.status || {});
    if (m.did) st.done = (st.done || []).concat(m.did);
    const D = (st.done || []).length, L = (st.remaining || []).length, B = (st.broken || []).length;
    if (D + L + B) {
      const item = (o, kind) => { o = typeof o === 'string' ? { what: o } : (o || {});
        const ic = kind === 'b' ? I.x : kind === 'l' ? I.circ : I.check;
        return '<li class="' + kind + '"><span class="ic">' + ic + '</span><span><span class="w">' + inl(o.what) + '</span>' + (o.why ? '<span class="y">' + inl(o.why) + '</span>' : '') + '</span></li>'; };
      const col = (label, arr, kind) => '<div class="col"><div class="ch">' + label + '</div><ul>' + arr.map(o => item(o, kind)).join('') + '</ul></div>';
      const cols = (D ? col('Done', st.done, 'd') : '') + (B ? col('Broken', st.broken, 'b') : '') + (L ? col('Not done yet', st.remaining, 'l') : '');
      // A Done card with nothing left or broken: "What changed", the list with
      // its checks and nothing else (no meter, no counts, no column header).
      // Owner's pick D, 2026-10-28, static/scratch/done-status.html.
      if (m.type === 'done' && !L && !B) h += band('What changed', '<div class="lm-statcols one"><div class="col"><ul>' + st.done.map(o => item(o, 'd')).join('') + '</ul></div></div>', 'lm-sec lm-status lm-changed');
      // No meter / "2 done · 1 left" row above the columns: the columns already
      // say it (owner, 2026-10-28).
      else h += band('Status', '<div class="lm-statcols' + ((D ? 1 : 0) + (B ? 1 : 0) + (L ? 1 : 0) === 1 ? ' one' : '') + '">' + cols + '</div>', 'lm-sec lm-status');
    }

    // "Validate": how the owner checks the work himself, as a row he taps
    // (owner, 2026-10-28: "we need to force a validate row so i have a way to
    // check").  A page this app can frame opens full screen; another web
    // address opens a tab; with no address it is a line of what to do.
    const vl = (m.validate && typeof m.validate === 'object') ? m.validate : null;
    const shots = [].concat(m.links || [], (m.needs && m.needs.look) || []).filter(isImg).filter(l => !optImg.has(l));
    // A card with a Validate row draws its screenshots INSIDE that band, as
    // rows under "Try it", never as a Screens band beside it (owner's pick A,
    // 2026-10-28, static/scratch/validate-screens.html: "the screenshots are
    // ok for validate i just dont want it presented as screen + validate").
    const shotsInValidate = !!(vl && vl.how);
    if (vl && vl.how) {
      const vu = String(vl.url || '').trim(), vs = vu ? _reviewSrc(vu) : '';
      const vr = _isDownload(vu) ? dlRow(vu, inl(vl.how), null, 'lm-validate') : vs ? row({ cls: 'lm-validate', tile: I.eye, title: inl(vl.how), sub: esc(vu), mono: true, verb: 'Try it',
                            attrs: ' data-view="' + esc(vs) + '" data-href="' + esc(vu) + '" data-title="' + esc(vl.how) + '"' })
        : /^https?:\/\//.test(vu) ? row({ cls: 'lm-validate', tile: I.eye, title: inl(vl.how), sub: esc(vu), mono: true, verb: 'Open ' + I.arrow, attrs: ' data-href="' + esc(vu) + '"' })
        : row({ cls: 'lm-validate static', tile: I.eye, title: inl(vl.how), sub: vu ? esc(vu) : '', mono: !!vu });
      // When the real check comes later, the row opens a demo of it and the
      // real moment is a quiet line under it (owner's pick B, 2026-10-28,
      // static/scratch/validate-later.html).
      const later = typeof vl.later === 'string' && vl.later.trim()
        ? '<div class="lm-vlater"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg><span>' + inl(vl.later) + '</span></div>' : '';
      // A page to try AND a screenshot: two tiles side by side, so it reads as
      // a choice, not two steps (owner's pick B, 2026-10-28,
      // static/scratch/validate-or.html: "its not obvious to me that that is a
      // or ... i need that clearer").  The OR is in the tile names, "Try it
      // yourself" / "OR just look", with no mark between them (owner's pick B,
      // static/scratch/validate-or-word.html: "more pronounced without a
      // circle").  Stacks on a phone.
      const tryOpens = !_isDownload(vu) && (vs || /^https?:\/\//.test(vu));
      if (shots.length && tryOpens) {
        const play = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M7 4.5v15l13-7.5z"/></svg>';
        const pic = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="m21 16-5-5L5 20"/></svg>';
        const shotAttrs = l => { const src = _imgSrc(l.url), t = l.label || 'Screenshot';
          return ' data-view="' + esc(src) + '" data-img="1" data-title="' + esc(t) + '"'; };
        const tryTile = '<button type="button" class="lm-row lm-validate vt vt-try"' +
          (vs ? ' data-view="' + esc(vs) + '" data-href="' + esc(vu) + '" data-title="' + esc(vl.how) + '"' : ' data-href="' + esc(vu) + '"') + '>' +
          '<span class="vt-h">' + play + ' Try it yourself</span><span class="vt-d">' + inl(vl.how) + '</span>' +
          '<span class="vt-act">' + (vs ? 'Open the demo' : 'Open it') + ' ' + I.arrow + '</span></button>';
        const s0 = shots[0], more = shots.slice(1), seeName = ' <span class="vt-orw">OR</span> just look';
        // One screenshot: the whole tile opens it.  Several: each picture does.
        const seeTile = !more.length
          ? '<button type="button" class="lm-row shot vt vt-see"' + shotAttrs(s0) + '><span class="vt-h">' + pic + seeName + '</span>' +
            '<span class="vt-pic"><img src="' + esc(_imgSrc(s0.url)) + '" alt="" loading="lazy"></span>' +
            '<span class="vt-act">View the screenshot</span></button>'
          : '<div class="vt vt-see"><span class="vt-h">' + pic + seeName + '</span>' +
          '<button type="button" class="lm-row shot vt-pic"' + shotAttrs(s0) + ' aria-label="' + esc('View ' + (s0.label || 'screenshot')) + '">' +
            '<img src="' + esc(_imgSrc(s0.url)) + '" alt="" loading="lazy"></button>' +
          (more.length ? '<span class="vt-more">' + more.map(l => '<button type="button" class="lm-row shot vt-thumb"' + shotAttrs(l) + ' title="' + esc(l.label || 'Screenshot') + '">' +
            '<img src="' + esc(_imgSrc(l.url)) + '" alt="" loading="lazy"></button>').join('') + '</span>' : '') +
          '<span class="vt-act">Tap a screenshot</span></div>';
        h += band('Validate', '<div class="lm-vpair">' + tryTile + seeTile + '</div>' + later, 'lm-sec');
      } else
      h += band('Validate', '<div class="lm-rows">' + vr + shots.map(linkRow).join('') + '</div>' + later, 'lm-sec');
    }
    // "Full answer": the detailed answer in markdown, folded under the short
    // one (owner's pick A, 2026-10-28, static/scratch/answer-detail.html:
    // "shouldn't there be a more detailed answer too, perhaps like expandable
    // markdown").  The same toggle as Explain more, which stays up top for
    // when the card itself is confusing.
    const fa = typeof m.full_answer === 'string' && m.full_answer.trim() ? m.full_answer.trim() : '';
    if (m.answer) h += band('In short', paras(m.answer) + (fa ? '<div class="lm-xpl lm-fullans"><button type="button" class="lm-xpl-tg" aria-expanded="false">Full answer <span class="tg">▾</span></button>' +
      '<div class="lm-xpl-body"><div><div class="lm-xpl-in">' + dvBodyHtml('markdown', fa) + '</div></div></div></div>' : ''), 'lm-sec');
    if (m.compare) h += band(esc(m.compare.label || 'Change'), compareBlock(m.compare), 'lm-sec');
    if (m.deliverable) h += band('Deliverable', deliverableBlock(m.deliverable), 'lm-sec');
    if (shots.length && !shotsInValidate) h += band('Screens', '<div class="lm-rows">' + shots.map(linkRow).join('') + '</div>', 'lm-sec');

    const n = m.needs || { kind: 'none' };
    if (n.kind === 'action' && n.tried && n.tried.length) {
      h += band('Tried', '<ul class="lm-tried">' + n.tried.map(x => '<li><span class="ic">' + I.x + '</span><span><span class="w">' + inl(x.what) + '</span><span class="y">' + inl(x.result) + '</span></span></li>').join('') + '</ul>' +
        (n.only_you ? '<p class="lm-onlyyou"><b>Why only you:</b> ' + inl(n.only_you) + '</p>' : ''), 'lm-sec');
    }
    // History sits above the action band (Next, Decide, To do, Read it…),
    // which sits above More (owner, 2026-10-28: "history should go before
    // next which goes before more").
    h += histH;
    // An answer or a text to send: the band is named for what he does and
    // holds only its own buttons, no "Nothing needed" (owner's pick A,
    // 2026-10-28, static/scratch/card-next-cta.html).  A finished fix keeps
    // its Next band as it was.
    const vb = (!n.kind || n.kind === 'none') ? _cardVerb(m) : '';
    if (vb === 'read' || vb === 'send') {
      h += band(vb === 'read' ? 'Read it' : 'Send it', backToStrip(m.back_to) + (!m.late ? verdictRows(m.offer) : ''), 'lm-quiet lm-act');
    } else if (!n.kind || n.kind === 'none') {
      // Still running: "Nothing needed until 11:40" when the time is a clock time.
      const clock = m.type === 'working' && typeof n.eta === 'string' && !n.eta_minutes &&
        /^~?\s*(\d{1,2}:\d{2}(\s*[ap]\.?m\.?)?|\d{1,2}\s*[ap]\.?m\.?)\s*$/i.test(n.eta.trim()) ? n.eta.trim().replace(/^~\s*/, '') : '';
      // Next says only what the session does next; the verdict chips get their
      // own "Your verdict" band under it, with no line above them (owner's
      // picks B and A, 2026-10-28, static/scratch/next-verdict-words.html and
      // next-verdict-line.html: "connecting the done nothing needs me to the
      // actions ... its like it does need me").  "Nothing needed." stays only
      // while the chips are not shown (an older card, a working one); CSS
      // hides it and the empty band otherwise.
      h += band('Next', (clock ? '<b>Nothing needed until ' + esc(clock) + '.</b> ' : '<b class="lm-nn">Nothing needed.</b> ') +
        '<span class="lm-nx">' + inl(n.next || n.why || '') + '</span>' +
        (n.eta && !n.eta_minutes && !clock ? '<span class="lm-eta">' + esc(n.eta) + '</span>' : '') +
        // The verdict chips sit right under the Next line, in the same band
        // (owner's pick A, 2026-10-28, static/scratch/card-verdict-in-next.html:
        // "verdict should just fall under next").  The wrapper keeps the
        // .lm-yv chip styles and hides itself while the chips are hidden.
        // A "Left waiting" strip stays above the verdict (owner's pick C).
        backToStrip(m.back_to) +
        (m.type !== 'working' && !m.late ? '<div class="lm-yv lm-yv-in">' + verdictRows() + '</div>' : ''), 'lm-quiet');
    } else {
      let x = '<p class="lm-ask">' + inl(n.ask) + '</p>';
      let look = (n.look || []).filter(l => !isImg(l));
      // A decision is ONE Decide button; the choices, the page to look at and
      // a box for your own answer live in a full-screen view, one choice at a
      // time (owner, 2026-10-28: "a decide button that expands to a full
      // screen decision environment"; variant A of static/scratch/decide-button.html).
      const decide = n.kind === 'decision' && n.replies && n.replies.length;
      if (decide) {
        const pi = look.findIndex(l => _reviewSrc((l || {}).url));
        const page = pi >= 0 ? look.splice(pi, 1)[0].url : '';
        const data = { ask: n.ask || '', why: n.why || '', page, replies: n.replies.map((r, i) => { r = r || {};
          const k = String(r.key || String.fromCharCode(65 + i));
          const pics = [...optImg].filter(([, key]) => key === k).map(([l]) => ({ label: l.label || '', url: l.url }));
          const rr = pics.length ? Object.assign({}, r, { screens: [].concat(r.screens || [], pics) }) : r;
          return Object.assign({ key: k, label: r.label || '', detail: r.detail || '', reply: r.reply || r.label || '', recommended: !!r.recommended }, _tplFields(rr)); }) };
        // The choices are named on the band, the recommended one tagged, so
        // "which do you recommend" is answered without opening anything (owner,
        // 2026-10-29: 'it should have said "A, my recommendation, and B"').
        if (n.replies.length > 1 && n.replies.some(r => r && r.recommended))
          x += '<ul class="lm-dlist">' + data.replies.map(r => '<li><b>' + esc(r.key) + '</b> ' + inl(r.label) +
            (r.recommended ? '<span class="rec">Recommended</span>' : '') + '</li>').join('') + '</ul>';
        x += '<button type="button" class="lm-decide" data-decide="' + esc(JSON.stringify(data)) + '">' +
          // The band above already reads "Decide", so the button names what it
          // opens, in the band's tone, not an inverted fill (owner, 2026-10-28).
          (n.replies.length > 1 ? 'See the ' + n.replies.length + ' choices' : 'See the choice') +
          '<span class="go">' + I.chev + '</span></button>';
      }
      // A choice with something to look at gets ONE Review button: it opens
      // the page full screen over the chat with the choices in a picker
      // (owner, 2026-10-27: "i have to click to open another tab and then
      // come back here?").  Only for pages this app can show in a frame.
      if (!decide && n.replies && n.replies.length && look.length && _reviewSrc((look[0] || {}).url)) {
        const l0 = look.shift();
        x += '<button type="button" class="lm-review" data-review="' + esc(l0.url) + '"><span class="ic">' + I.eye + '</span>' +
          '<span class="tx"><span class="tt">Review the options</span><span class="ts">' + inl(l0.label || 'Opens here, full screen') + '</span></span>' +
          '<span class="go">' + I.chev + '</span></button>';
      }
      if (look.length) x += '<div class="lm-rows lm-chips">' + look.map(linkRow).join('') + '</div>';
      if (n.steps && n.steps.length) x += '<div class="lm-rows lm-steps">' + n.steps.map((sp, i) => sp.command
        ? copyRow({ icon: '<span style="font:600 10px var(--font-mono,monospace)">&gt;_</span>', title: sp.command, mono: true, sub: sp.text, text: sp.command })
        : row({ tile: String(i + 1), title: inl(sp.text), cls: 'static' })).join('') + '</div>';
      if (!decide && n.replies && n.replies.length) x += replyButtons(n.replies);
      x += '<div class="lm-sent">' + I.check + '<span>Reply sent:</span><b class="v"></b></div>';
      if (n.next) x += '<p class="lm-next"><b>Then:</b> ' + inl(n.next) + '</p>';
      if ((n.kind === 'decision' || n.kind === 'action') && !m.late) x += verdictRows();
      h += band(({ decision: 'Decide', approve: 'Approve', action: 'To do' })[n.kind] || 'Needed from you', x, 'lm-sec lm-needs');
    }
    // After the action band, before More: a side finding and its one button.
    h += findingBand(m.finding);

    let more = '', bits = [];
    if (n.why && n.kind && n.kind !== 'none') { bits.push('why I’m asking'); more += '<div class="lm-sub">Why I’m asking</div><div style="font-size:13px;color:var(--text-muted)">' + inl(n.why) + '</div>'; }
    if (m.verified && m.verified.length) { bits.push('checked ' + m.verified.length + (m.verified.length > 1 ? ' ways' : ' way')); more += '<div class="lm-sub">How I checked it</div>' + list(m.verified.map(v => ({ what: v.check, why: v.result, ok: v.ok })), 'ok'); }
    if (m.watch_for && m.watch_for.length) { bits.push(m.watch_for.length + (m.watch_for.length > 1 ? ' notes' : ' note')); more += '<div class="lm-sub">Worth knowing</div>' + list(m.watch_for, 'dot'); }
    const links = (m.links || []).filter(l => !isImg(l));
    if (links.length) { bits.push(links.length + (links.length > 1 ? ' links' : ' link')); more += '<div class="lm-sub">Look at</div><div class="lm-rows">' + links.map(linkRow).join('') + '</div>'; }
    if (m.details && m.details.length) { bits.push('technical details'); more += '<div class="lm-sub">Technical details</div>' + list(m.details, 'dot'); }
    if (more) h += band('More', '<button type="button" class="lm-more">' + I.chev + ' ' + bits.map(b => b.charAt(0).toUpperCase() + b.slice(1)).join(' · ') + '</button><div class="lm-fold">' + more + '</div>', 'lm-morebar');
    return h + '</div>';
  }

  // A deliverable file marked "copy": true is a Copy row (owner, 2026-10-29:
  // "I want it in my copy buffer when I select the button").  The text is
  // fetched when the card renders, never on the tap: iOS copies only inside
  // the tap's own call stack (same rule as the copy cards in smart-copy.js).
  // Only this app's own static files, so a card cannot make the page fetch
  // anything else.
  // The owner then wanted it as a .txt FILE, not pasted text (2026-10-29: "I
  // need the copy to be a txt file").  A web page cannot put a file on the
  // clipboard, so on a phone the tap opens the share sheet with the .txt
  // (its Copy puts the file on the clipboard; ChatGPT, Files, Mail take it
  // directly).  Elsewhere the tap saves the .txt.  The text is already
  // loaded, so the share runs inside the tap as iOS requires.
  function _shareFiles() {
    try {
      return typeof navigator !== 'undefined' && typeof navigator.canShare === 'function' && typeof File === 'function' &&
        !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);
    } catch (_e) { return false; }
  }
  function _copyAsFile(r) {
    const text = r.dataset.copy;
    if (text == null) return;
    const name = decodeURIComponent(r.dataset.copySrc.split('/').pop() || 'text.txt').replace(/\.[^.]+$/, '') + '.txt';
    const vb = r.querySelector('.vb'), old = vb.innerHTML;
    const done = label => { r.classList.add('copied'); vb.innerHTML = I.check + ' ' + label; setTimeout(() => { r.classList.remove('copied'); vb.innerHTML = old; }, 1600); };
    const file = new File([text], name, { type: 'text/plain' });
    if (_shareFiles() && navigator.canShare({ files: [file] })) {
      navigator.share({ files: [file], title: name }).then(() => done('Shared')).catch(() => {});
      return;
    }
    const a = document.createElement('a');
    a.href = URL.createObjectURL(file); a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    done('Saved');
  }
  function _syncCopySrc(root) {
    root.querySelectorAll('.lm-row[data-copy-src]:not([data-copy])').forEach(r => {
      if (r.dataset.copyLoading) return;
      r.dataset.copyLoading = '1';
      fetch(r.dataset.copySrc, { credentials: 'same-origin' })
        .then(res => res.ok ? res.text() : Promise.reject(res.status))
        .then(t => { r.dataset.copy = t; r.removeAttribute('aria-disabled'); r.classList.remove('loading'); })
        .catch(() => { const vb = r.querySelector('.vb'); if (vb) vb.textContent = 'Could not load'; });
    });
  }

  // ── The verdict bar: only on the open session's newest card, only while
  //    it waits for your verdict (Review); "Session done" once it sleeps.
  function _syncDone() {
    if (window.VNLastCard !== _api) return;
    _queueSoon();   // a state change can mean a queued message was just handled
    _syncEarlier();
    const log = document.getElementById('live-log');
    if (!log) return;
    const cards = log.querySelectorAll('.lm.skim');
    const last = cards.length ? cards[cards.length - 1] : null;
    // A "Left waiting" strip only on the newest card: an older one's way
    // back is out of date.
    log.querySelectorAll('.lm-backto').forEach(b => { b.hidden = !last || b.closest('.lm') !== last; });
    // "Deal with it" acts only from the newest card; the finding's text stays.
    log.querySelectorAll('.lm-find .go').forEach(b => { b.hidden = !last || b.closest('.lm') !== last; });
    _syncDelete(log, last);
    _syncCopySrc(log);
    const all = log.querySelectorAll('.lm-finish');
    if (!all.length) return;
    const sid = (typeof liveSessionId !== 'undefined') ? liveSessionId : null;
    let d = sid && window.SessionVerb ? window.SessionVerb.display(sid) : '';
    // Which verdict buttons fit is the newest card's own kind, not the
    // sidebar's guess: an answer showed Done · Something else · Problem
    // (2026-10-28) when the sidebar had no card for the session and fell back
    // to Review.  Review / Read / Send only; Decide, Standby and Done keep
    // the status's own rows.
    if ((d === 'review' || d === 'read' || d === 'send') && last) {
      let own = '';
      try { own = _cardVerb(JSON.parse(last.getAttribute('data-card') || '{}') || {}); } catch (_e) { own = ''; }
      if (own === 'read' || own === 'send') d = own;
    }
    const state = { review: 'verdict', read: 'read', send: 'send', decide: 'question', unblock: 'question', standby: 'standby', done: 'done' }[d] || '';
    all.forEach(f => {
      const mine = !!(last && f.closest('.lm') === last && state);
      f.hidden = !mine;
      if (mine) { f.classList.toggle('done', d === 'done'); f.dataset.state = state; }
    });
  }
  // ── Delete the session from its newest summary (owner, 2026-10-29: "on
  //    every card should we have delete session button perhaps offset to
  //    bottom right"; picked "confirm on the button" from
  //    static/scratch/summary-delete-confirm.html). A quiet trash at the
  //    bottom right of the NEWEST summary only (every summary in a thread is
  //    the same session). Tapping it turns that spot into "Delete session?"
  //    Delete · Cancel, with Cancel where the trash was so a double-tap can
  //    never delete; left alone it goes back after 6 s.
  const _TRASH = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>';
  function _syncDelete(log, last) {
    const sid = (typeof liveSessionId !== 'undefined') ? liveSessionId : null;
    const can = !!(last && sid && typeof window.deleteSession === 'function');
    log.querySelectorAll('.lm.lm-hasdel').forEach(c => {
      if (c === last && can) return;
      c.classList.remove('lm-hasdel');
      c.querySelectorAll(':scope > .lm-del, :scope > .lm-delask').forEach(e => e.remove());
    });
    if (!can || last.querySelector(':scope > .lm-del')) return;
    last.classList.add('lm-hasdel');
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'lm-del';
    b.title = 'Delete session';
    b.setAttribute('aria-label', 'Delete session');
    b.innerHTML = _TRASH;
    b.addEventListener('click', ev => { ev.preventDefault(); ev.stopPropagation(); _askDelete(last, b, sid); });
    last.appendChild(b);
  }
  function _askDelete(card, b, sid) {
    if (card.querySelector(':scope > .lm-delask')) return;
    b.hidden = true;
    const ask = document.createElement('div');
    ask.className = 'lm-delask';
    ask.setAttribute('role', 'group');
    ask.setAttribute('aria-label', 'Delete session?');
    ask.innerHTML = '<span class="q">Delete session?</span>' +
      '<button type="button" class="yes">Delete</button><button type="button" class="no">Cancel</button>';
    card.appendChild(ask);
    const t = setTimeout(close, 6000);
    function close() { clearTimeout(t); ask.remove(); b.hidden = false; }
    ask.addEventListener('click', ev => ev.stopPropagation());
    ask.querySelector('.no').addEventListener('click', close);
    ask.querySelector('.yes').addEventListener('click', () => {
      clearTimeout(t);
      ask.remove();
      b.hidden = false;
      try { window.deleteSession(sid, { confirmed: true }); } catch (_e) { /* the toast reports a failure */ }
    });
    // preventScroll: a plain focus() scrolled the thread under the tap.
    try { ask.querySelector('.no').focus({ preventScroll: true }); } catch (_e) { /* no focus */ }
  }
  // Something else / Problem / Change it: the chat box opens with a starter
  // line; Ask more opens it empty.
  const _VERDICT_PREFIX = { next: 'That’s done. Next: ', bad: 'Problem: ', change: 'Change: ', ask: '' };
  function _verdictPrefix(kind) {
    const ta = document.getElementById('live-input-ta');
    if (!ta) return;
    const pre = _VERDICT_PREFIX[kind], cur = ta.value || '';
    const bare = !cur.trim() || Object.values(_VERDICT_PREFIX).some(p => cur === p);
    ta.value = bare ? pre : pre + cur;
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    try { ta.focus({ preventScroll: true }); } catch (_e) { ta.focus(); }
    try { ta.setSelectionRange(ta.value.length, ta.value.length); } catch (_e) { /* ok */ }
  }
  // ── Answered: a Needs-you card goes green once he has replied (owner,
  //    2026-10-28: "this should go green after i decide").  A reply sent from
  //    the card marks it at once; any message of his after the card in the
  //    thread marks it too, so a redraw or a reload keeps it green and an
  //    answer typed in the chat box counts.
  function _markAnswered(root, text) {
    if (!root || !root.classList || !root.classList.contains('t-needs') || root.classList.contains('lm-answered')) return;
    let kind = '';
    try { kind = ((JSON.parse(root.getAttribute('data-card') || '{}') || {}).needs || {}).kind || ''; } catch (_e) { kind = ''; }
    const label = kind === 'decision' ? 'Decided' : 'Answered';
    root.classList.add('lm-answered');
    const pill = root.querySelector('.lm-head .lm-pill');
    if (pill) pill.innerHTML = I.check + esc(label);
    const jump = root.querySelector('.lm-stick .jump');
    if (jump) { const st = document.createElement('span'); st.className = 'st'; st.innerHTML = I.check + esc(label); jump.replaceWith(st); }
    const t = String(text || '').trim();
    const short = t.length > 90 ? t.slice(0, 89) + '…' : t;
    const btn = root.querySelector('.lm-decide');
    if (btn && !btn.dataset.sent) {
      btn.dataset.sent = '1';
      btn.classList.add('sent');
      btn.innerHTML = I.check + '<span>' + (short ? 'Sent: “' + esc(short) + '”' : 'Answered') + '</span>';
    }
    const group = root.querySelector('.lm-rows .lm-row.reply') ? root.querySelector('.lm-row.reply').closest('.lm-rows') : null;
    if (group && !group.classList.contains('done')) {
      const hit = [...group.querySelectorAll('.lm-row.reply')].find(x => (x.dataset.reply || '').trim() === t);
      if (hit) group.querySelectorAll('.lm-row.reply').forEach(x => x.classList.toggle('picked', x === hit));
      group.classList.add('done');
    }
    const sent = root.querySelector('.lm-sent');
    if (sent && !btn && !sent.classList.contains('on') && short) { sent.querySelector('.v').textContent = '“' + short + '”'; sent.classList.add('on'); }
  }
  /** His first message after a card's own message, or null. */
  function _replyAfter(card) {
    const msg = card.closest('.msg');
    for (let n = msg && msg.nextElementSibling; n; n = n.nextElementSibling) {
      if (n.classList.contains('msg') && n.classList.contains('user') && !n.classList.contains('vn-event-msg')) return n;
    }
    return null;
  }
  function _syncAnswered() {
    if (window.VNLastCard !== _api) return;
    const log = document.getElementById('live-log');
    if (!log) return;
    log.querySelectorAll('.lm.skim.t-needs:not(.lm-answered)').forEach(card => {
      const u = _replyAfter(card);
      if (!u) return;
      const body = u.querySelector('.msg-body') || u;
      _markAnswered(card, body.textContent || '');
    });
  }
  let _sockHooked = false;
  function _hookSocket() {
    if (_sockHooked || typeof socket === 'undefined' || !socket || !socket.on) return;
    _sockHooked = true;
    // Registered after socket.js's own handler, so the session's state is
    // already updated when this runs.
    socket.on('session_state', () => requestAnimationFrame(() => { _syncDone(); _syncAnswered(); }));
  }

  // ── Placeholders → cards ──
  function _when(ms) {
    if (!ms) return '';
    try { return new Date(ms).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }); } catch (_e) { return ''; }
  }
  // ── A new card lands with its top at the top of the screen (2026-10-29,
  //    the owner on his phone: "When the answer card comes the scroll should
  //    be so the top of the card is at the top of the screen"). The scrolling
  //    is ThreadScroll.landCard (thread-scroll.js). The TRIGGER is here as
  //    well, because this file reaches an open tab without a reload
  //    (live-assets.js) and the thread code does not: his phone tab, open all
  //    evening, ran the thread code from before the fix twice over ("it
  //    doesn't work", "still not working"). A card drawn for a message that is
  //    not yet in the page is a live arrival (renderLiveEntry builds the
  //    message, then appends it); a redraw works on a connected one. One such
  //    draw in a frame is an arrival; several are a full render of the thread.
  let _fresh = 0, _freshEl = null, _freshRaf = 0;
  function _noteFresh(el) {
    _fresh++; _freshEl = el;
    if (_freshRaf) return;
    _freshRaf = requestAnimationFrame(() => {
      _freshRaf = 0;
      const n = _fresh, el = _freshEl;
      _fresh = 0; _freshEl = null;
      if (window.VNLastCard !== _api || n !== 1 || !el) return;
      const log = document.getElementById('live-log');
      const msg = el.closest && el.closest('.msg');
      if (!log || !msg || !log.contains(msg)) return;
      const msgs = log.querySelectorAll('.msg');
      if (msgs[msgs.length - 1] !== msg) return;
      _landCard(msg, log);
    });
  }
  function _landCard(msg, log) {
    if (window.ThreadScroll && typeof ThreadScroll.landCard === 'function') { ThreadScroll.landCard(msg); return; }
    // Thread code from before 2026-10-29 on this tab: land it here.
    const go = () => {
      if (!msg.isConnected) return;
      const top = msg.getBoundingClientRect().top - log.getBoundingClientRect().top + log.scrollTop - 8 - _stickOffset(log);
      log.scrollTop = Math.max(0, Math.min(top, log.scrollHeight - log.clientHeight));
    };
    go(); requestAnimationFrame(go); setTimeout(go, 250);
  }

  function renderIn(container) {
    if (!container || !container.querySelectorAll) return;
    const fresh = !container.isConnected;
    if (fresh) _fresh++;   // counted even when nothing in it is a card: a full render is many draws
    container.querySelectorAll('.vn-card-ph').forEach(ph => {
      let card = null;
      try { card = JSON.parse(ph.getAttribute('data-card') || ''); } catch (_e) { card = null; }
      if (!card || typeof card !== 'object' || Array.isArray(card) || !card.headline) return;  // stays as code
      const msg = ph.closest('.msg');
      const ts = msg && msg.dataset.ts ? Number(msg.dataset.ts) * 1000 : 0;
      const wrap = document.createElement('div');
      wrap.innerHTML = render(card, { clock: ts ? String(ts) : '', when: _when(ts) });
      const el = wrap.firstElementChild;
      // Keep the card's JSON on it so live-assets.js can redraw it when
      // this file changes on disk (no page reload, 2026-10-28).
      if (el) { el.setAttribute('data-card', ph.getAttribute('data-card') || ''); ph.replaceWith(el); if (fresh) _noteFresh(el); }
    });
    _syncCopySrc(container);
    _tick();
    _hookSocket();
    requestAnimationFrame(() => { _syncDone(); _syncAnswered(); _queueSoon(); _syncEarlier(); });
  }

  // ── Also handled: queued messages the session sent and handled since he
  //    last sent something himself (owner's pick C with the history-clock icon
  //    G, 2026-10-28, static/scratch/card-queue.html).  One line under "Just
  //    now" on the open session's newest card, only when there is at least
  //    one; it opens the list, and Show it jumps to that message in the chat.
  //    Data: GET /api/queue-history/<sid> (app/queue_history.py). ──
  const _CLOCK = '<svg class="zq" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v4h4"/><path d="M12 8v4l3 2"/></svg>';
  let _qTimer = null, _qSeq = 0;
  function _queueSoon() { clearTimeout(_qTimer); _qTimer = setTimeout(_syncQueue, 250); }
  async function _syncQueue() {
    if (window.VNLastCard !== _api) return;
    const log = document.getElementById('live-log');
    const sid = (typeof liveSessionId !== 'undefined') ? liveSessionId : null;
    if (!log) return;
    const cards = log.querySelectorAll('.lm.skim');
    const last = cards.length ? cards[cards.length - 1] : null;
    log.querySelectorAll('.lm-qwrap').forEach(x => { if (!last || x.closest('.lm') !== last) x.remove(); });
    if (!last || !sid) return;
    const seq = ++_qSeq;
    let d = null;
    try {
      const r = await fetch('/api/queue-history/' + encodeURIComponent(sid));
      if (r.ok) d = await r.json();
    } catch (_e) { d = null; }
    if (seq !== _qSeq || !last.isConnected || ((typeof liveSessionId !== 'undefined') ? liveSessionId : null) !== sid) return;
    // Only what was sent before this card was written belongs on it.
    const msg = last.closest('.msg');
    const cardAt = msg && msg.dataset.ts ? Number(msg.dataset.ts) : 0;
    const items = ((d && d.items) || []).filter(i => i && i.text && (!cardAt || (i.sent_at || 0) <= cardAt + 2));
    const key = items.map(i => i.sent_at).join(',');
    const had = last.querySelector('.lm-qwrap');
    if (had && had.dataset.key === key) return;
    if (had) had.remove();
    if (!items.length) return;
    const since = d.reviewed_at ? _when(d.reviewed_at * 1000) : '';
    const n = items.length;
    const wrap = document.createElement('div');
    wrap.className = 'lm-qwrap';
    wrap.dataset.key = key;
    wrap.innerHTML = '<button type="button" class="r lm-qline" aria-expanded="false"><div class="nst">' + _CLOCK + '<span class="lbl">Also handled</span><span class="t">' +
      n + ' queued message' + (n === 1 ? '' : 's') + (since ? ' since ' + esc(since) : '') + ' <span class="chev">' + I.chev + '</span></span></div></button>' +
      '<div class="lm-qx"><div><ul class="lm-ql">' + items.map((i, j) => '<li><span class="n">' + (j + 1) + '</span><span class="bd"><span class="tx">' + esc(i.text) + '</span>' +
        '<span class="tm">' + (i.queued_at ? 'Queued ' + esc(_when(i.queued_at * 1000)) + ' · ' : '') + 'sent ' + esc(_when(i.sent_at * 1000)) + '</span></span>' +
        '<button type="button" class="go" data-qjump="' + j + '">Show it</button></li>').join('') + '</ul></div></div>';
    wrap._items = items;
    let zoom = last.querySelector('.lm-zoom');
    if (!zoom) {
      // A card without This task / Just now still gets a History section
      // for the line, above Next and More like everywhere else.
      const sec = document.createElement('div');
      sec.className = 'lm-dl lm-hist';
      sec.innerHTML = '<div class="dt">History</div><div class="dd"><div class="lm-zoom"></div></div>';
      const after = last.querySelector(':scope > .lm-dl.lm-quiet, :scope > .lm-dl.lm-needs, :scope > .lm-dl.lm-morebar');
      if (after) last.insertBefore(sec, after); else last.appendChild(sec);
      zoom = sec.querySelector('.lm-zoom');
    }
    zoom.appendChild(wrap);
  }
  // ── Earlier tasks: the ladder grows upward (owner's pick B, 2026-10-28,
  //    static/scratch/card-earlier.html).  On the open session's newest
  //    summary, the two tasks before this one sit above "This task" as faint
  //    rows with their state; "+N earlier" opens the rest; a row jumps to that
  //    task's last summary.  Read from the summaries on the page (their
  //    data-card), so it needs nothing from the server. ──
  const _EMARK = {
    done: ['Done', '<path d="M20 6 9 17l-5-5"/>'],
    answered: ['Answered', '<path d="M20 6 9 17l-5-5"/>'],
    you: ['Needs you', '<path d="M12 6v7"/><path d="M12 17.5v.5"/>'],
    blocked: ['Blocked', '<path d="M6 6l12 12"/>'],
    working: ['Was working', '<circle cx="12" cy="12" r="3" fill="currentColor" stroke="none"/>'],
  };
  const _emark = st => '<span class="lm-emark ' + st + '" title="' + _EMARK[st][0] + '"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round">' + _EMARK[st][1] + '</svg></span>';
  function _cardTask(c) {
    const z = c && c.zoom && typeof c.zoom === 'object' ? c.zoom : {};
    return String(z.task || c.title || c.headline || '').trim();
  }
  function _earlierTasks(log, last) {
    const cards = [...log.querySelectorAll('.lm.skim[data-card]')];
    const users = [...log.querySelectorAll('.msg.user')];
    // Answered: his next message is one of the card's replies (a Decide pick,
    // an "I did it").  Moving on to something else leaves it waiting on him.
    const norm = t => String(t || '').toLowerCase().replace(/\s+/g, ' ').trim();
    const repliedAfter = (el, c) => {
      const next = users.find(u => el.compareDocumentPosition(u) & Node.DOCUMENT_POSITION_FOLLOWING);
      if (!next) return false;
      const said = norm(next.textContent);
      const rs = (c && c.needs && Array.isArray(c.needs.replies)) ? c.needs.replies : [];
      return rs.some(r => r && [r.reply, r.label].some(x => { const n = norm(x); return n.length >= 3 && said.indexOf(n.slice(0, 40)) !== -1; }));
    };
    let cur = '';
    try { cur = _cardTask(JSON.parse(last.getAttribute('data-card') || '{}')).toLowerCase(); } catch (_e) { cur = ''; }
    const tasks = [];
    cards.forEach(el => {
      if (el === last) return;
      let c = null;
      try { c = JSON.parse(el.getAttribute('data-card') || ''); } catch (_e) { c = null; }
      const task = c && _cardTask(c);
      if (!task || task.toLowerCase() === cur) return;
      const st = c.type === 'blocked' ? 'blocked' : c.type === 'working' ? 'working'
        : c.type === 'needs_you' ? (repliedAfter(el, c) ? 'answered' : 'you') : 'done';
      const prev = tasks[tasks.length - 1];
      if (prev && prev.task.toLowerCase() === task.toLowerCase()) { prev.el = el; prev.st = st; return; }
      tasks.push({ task, st, el });
    });
    // Tasks that came back after a different one appear once, at their last place.
    const seen = new Set();
    return tasks.reverse().filter(t => { const k = t.task.toLowerCase(); if (seen.has(k)) return false; seen.add(k); return true; }).reverse();
  }
  function _syncEarlier() {
    if (window.VNLastCard !== _api) return;
    const log = document.getElementById('live-log');
    if (!log) return;
    const cards = log.querySelectorAll('.lm.skim');
    const last = cards.length ? cards[cards.length - 1] : null;
    log.querySelectorAll('.lm-earlier').forEach(x => { if (!last || x.closest('.lm') !== last) x.remove(); });
    if (!last) return;
    const zoom = last.querySelector('.lm-zoom');
    const tasks = zoom ? _earlierTasks(log, last) : [];
    const key = tasks.map(t => t.st + ':' + t.task).join('|');
    const had = last.querySelector('.lm-earlier');
    if (had && had.dataset.key === key) { had._tasks = tasks; return; }
    if (had) had.remove();
    if (!tasks.length) return;
    const shown = tasks.slice(-2), rest = tasks.slice(0, -2);
    const row = (t, j) => '<button type="button" class="lm-erow" data-ejump="' + j + '">' + _emark(t.st) + '<span class="lbl">Earlier</span><span class="t">' + inl(t.task) + '</span></button>';
    const w = document.createElement('div');
    w.className = 'lm-earlier';
    w.dataset.key = key;
    w.innerHTML = (rest.length ? '<button type="button" class="lm-emore" aria-expanded="false"><span class="t">+ ' + rest.length + ' earlier <span class="chev">' + I.chev + '</span></span></button>' +
        '<div class="lm-qx"><div>' + rest.map((t, j) => row(t, j)).join('') + '</div></div>' : '') +
      shown.map((t, k) => row(t, rest.length + k)).join('');
    w._tasks = tasks;
    zoom.insertBefore(w, zoom.firstChild);
  }

  /** The user's bubble in the chat for a queued message he sent. */
  function _queueBubble(text) {
    const log = document.getElementById('live-log');
    if (!log) return null;
    const want = String(text || '').replace(/\s+/g, ' ').trim().slice(0, 80);
    const bubbles = [...log.querySelectorAll('.msg.user')];
    for (let k = bubbles.length - 1; k >= 0; k--) {
      if ((bubbles[k].textContent || '').replace(/\s+/g, ' ').indexOf(want) !== -1) return bubbles[k];
    }
    return null;
  }

  // ── Interactions (one delegated listener for every card) ──
  function _send(text) {
    const sid = (typeof liveSessionId !== 'undefined') ? liveSessionId : null;
    if (!sid || !text) return false;
    const kind = (typeof sessionKinds !== 'undefined') ? sessionKinds[sid] : '';
    let sent = false;
    if ((kind === 'idle' || kind === 'question' || !kind) && typeof _liveSubmitDirect === 'function') { _liveSubmitDirect(sid, text); sent = true; }
    else if (typeof _addQueue === 'function') { _addQueue(sid, text); sent = true; }
    // A card answer is a request to watch what it starts: follow the work
    // until the owner scrolls, whatever Auto scroll is set to (2026-10-28).
    if (sent && window.ThreadScroll && typeof ThreadScroll.followWork === 'function') {
      try { ThreadScroll.followWork(); } catch (_e) { /* scrolling is never worth a failed send */ }
    }
    return sent;
  }
  async function _copy(t) {
    if (typeof vnCopyText === 'function') { try { return (await vnCopyText(t)) !== false; } catch (_e) { /* fall through */ } }
    try { await navigator.clipboard.writeText(t); return true; } catch (_e) { return false; }
  }
  /** Send one reply of a card and mark it picked (the card's buttons and the
   *  review overlay's Choose both come here). */
  function _choose(root, reply) {
    const group = reply.closest('.lm-rows');
    if (!group || group.classList.contains('done')) return false;
    if (!_send(reply.dataset.reply)) return false;
    group.querySelectorAll('.lm-row.reply').forEach(x => x.classList.toggle('picked', x === reply));
    group.classList.add('done');
    const sent = root.querySelector('.lm-sent');
    if (sent) { sent.querySelector('.v').textContent = '“' + reply.dataset.reply + '”'; sent.classList.add('on'); }
    _markAnswered(root, reply.dataset.reply);
    return true;
  }

  // ── Review overlay: the page full screen, the card's choices in a picker ──
  // The choices are read from the card's own reply buttons, so the overlay
  // and the card can never disagree.  Picking a choice scrolls the page to
  // that option when the page has an element whose id is the choice's key
  // (a, b, c…) or [data-option="<key>"]; Choose sends it like the card does.
  // Find an option on a page however it is labelled (2026-10-27: a pick
  // that found nothing looked like a dead button).  In order: the contract
  // [data-option="A"], an id (a, A, v-a, option-a, opt-a), then a heading
  // that starts with the key ("A ·", "A.", "A:", "Option A").
  function _findOption(d, k) {
    const lo = k.toLowerCase(), safe = k.replace(/["\\]/g, '');
    let t = d.querySelector('[data-option="' + safe + '" i]');
    if (!t) for (const id of [lo, k, 'v-' + lo, 'option-' + lo, 'opt-' + lo]) { t = d.getElementById(id); if (t) break; }
    if (!t) {
      const re = new RegExp('^\\s*(?:option\\s+)?' + safe.replace(/[.*+?^${}()|[\]]/g, m => '\\' + m) + '\\s*(?:[·.:)\\-\\u2013\\u2014]|\\s)', 'i');
      const heads = [...d.querySelectorAll('h1, h2, h3, h4, legend, summary')];
      const h = heads.find(x => re.test(x.textContent || '')) ||
        // "<h2><b>A</b>Name</h2>": the key in its own tag at the start
        heads.find(x => x.firstElementChild && (x.firstElementChild.textContent || '').trim().toUpperCase() === k.toUpperCase());
      if (h) t = h.closest('section, article, fieldset, details, [class*="variant"], [class*="option"]') || h;
    }
    return t;
  }

  // ── Decide: the decision full screen, one choice at a time ──
  // The page shows only the option on the current tab: its title, intro and
  // control bar and every other option are hidden.  Returns false when the
  // page marks no option for this key (the whole page then stays visible).
  // Hide everything on the page except `els` and their ancestors.
  function _isolate(d, els, padTop) {
    if (!els.length) return;
    const anc = new Set([d.body]);
    els.forEach(o => { for (let p = o.parentElement; p && p !== d.documentElement; p = p.parentElement) anc.add(p); });
    anc.forEach(a => [...a.children].forEach(ch => {
      if (anc.has(ch) || els.includes(ch) || ch.matches('script, style, link') || els.some(o => ch.contains(o))) return;
      ch.style.display = 'none';
    }));
    els.forEach(o => { o.style.marginTop = '0'; });
    d.body.style.paddingTop = padTop + 'px';
  }
  function _solo(frame, keys, key) {
    let d;
    try { d = frame.contentDocument; } catch (_e) { return false; }
    if (!d || !d.body) return false;
    if (!frame._vnOpts) {
      const map = new Map();
      keys.forEach(k => { const t = _findOption(d, k); if (t) map.set(k, t); });
      d.querySelectorAll('[data-option]').forEach(t => { if (![...map.values()].includes(t)) map.set('~' + t.getAttribute('data-option'), t); });
      frame._vnOpts = map;
      _isolate(d, [...map.values()], 52);
    }
    const target = frame._vnOpts.get(key);
    frame._vnOpts.forEach(t => { t.style.display = (!target || t === target) ? '' : 'none'; });
    try { frame.contentWindow.scrollTo(0, 0); } catch (_e) { /* ignore */ }
    return !!target;
  }
  // A page opened over the chat takes the app's theme as it is right now (owner,
  // 2026-10-28: the decide pages did not always match).  Same-origin pages
  // (scratch pages, the preview proxy) get data-theme set on load, whatever
  // their own theme script decided; a static page also gets ?theme= so it
  // paints in the right theme from its first frame.
  function _appTheme() { return document.documentElement.getAttribute('data-theme') || 'dark'; }
  // The query goes BEFORE any #fragment: appended after it, "page.html#v-a"
  // became "#v-a?theme=dark", the anchor matched nothing, and every screen
  // of a decision showed the top of the same page (owner, 2026-10-28:
  // "these are all showing as the same ... its a systemic failure").
  // Another app on this machine, framed as itself (_reviewSrc), gets ?theme=
  // too: it is another origin, so data-theme cannot be set inside it, and the
  // address is the only way to tell it (CustomerNode's trial pages read it).
  function _themedSrc(src) {
    if (!/^(\/static\/|https?:\/\/)/.test(src)) return src;
    const h = src.indexOf('#'), path = h === -1 ? src : src.slice(0, h), frag = h === -1 ? '' : src.slice(h);
    if (/[?&]theme=/.test(path)) return src;
    return path + (path.indexOf('?') === -1 ? '?' : '&') + 'theme=' + encodeURIComponent(_appTheme()) + frag;
  }
  function _matchTheme(frame) {
    const apply = () => {
      try {
        const d = frame.contentDocument;
        if (d && d.documentElement) d.documentElement.setAttribute('data-theme', _appTheme());
      } catch (_e) { /* another origin: its own look */ }
    };
    frame.addEventListener('load', apply);
    return apply;
  }
  // ── Decide templates (owner's pick, 2026-10-28, static/scratch/decide-motion.html) ──
  // A reply may carry a design-choice read-out (strengths / weaknesses /
  // opportunities / risks, effort / undo / risk, "pick this if") and screens
  // (pages this app can frame, shown in a Web or Phone frame).  They are drawn
  // only inside the popup; a reply without them keeps the plain slide.
  const _SWOT = [['strengths', 'q-s', '+', 'Strengths'], ['weaknesses', 'q-w', '−', 'Weaknesses'], ['opportunities', 'q-o', '↗', 'Opportunities'], ['risks', 'q-r', '!', 'Risks']];
  const _LEVEL = { none: 0, small: 1, low: 1, easy: 1, medium: 2, moderate: 2, large: 3, big: 3, high: 3, hard: 3 };
  const _DEV = { web: [1280, 800], phone: [390, 844] };
  function _tplFields(r) {
    const o = {};
    const strs = v => (Array.isArray(v) ? v : []).filter(x => typeof x === 'string' && x.trim()).slice(0, 6);
    _SWOT.forEach(q => { const a = strs(r[q[0]]); if (a.length) o[q[0]] = a; });
    ['effort', 'undo', 'risk', 'fit'].forEach(k => { const v = typeof r[k] === 'string' ? r[k].trim().slice(0, k === 'fit' ? 300 : 40) : ''; if (v) o[k] = v; });
    const sc = (Array.isArray(r.screens) ? r.screens : []).filter(s => s && typeof s.url === 'string' && _reviewSrc(s.url)).slice(0, 8)
      .map(s => {
        const o = { label: String(s.label || ''), url: s.url, device: s.device === 'phone' || s.device === 'both' ? s.device : 'web' };
        const im = _screenImage(s.image);
        if (im) o.image = im;
        return o;
      });
    if (sc.length) o.screens = sc;
    return o;
  }
  // A screen's "image": its picture, drawn where its page cannot be shown live
  // (the phone, for another app on this machine: _tplStage).  A picture, or one
  // per device {"web", "phone"}, each a picture or one per theme {"light", "dark"}.
  // Only an address the card can load counts (as isImg): a relative path or a
  // file:// with two slashes drew a broken picture.  daemon/handoff_rules.py
  // _screen_image_ok holds the same shapes.
  function _screenImage(v) {
    const pic = x => (typeof x === 'string' && _isPic(x.trim()) && /^(\/(?!\/)|https?:\/\/|~\/|file:\/\/\/|[A-Za-z]:[\\/])/.test(x.trim()) ? x.trim() : '');
    const keep = (x, keys, inner) => {
      if (!x || typeof x !== 'object') return '';
      const o = {};
      keys.forEach(k => { const y = inner(x[k]); if (y) o[k] = y; });
      return Object.keys(o).length ? o : '';
    };
    const themed = x => pic(x) || keep(x, ['light', 'dark'], pic);
    return pic(v) || keep(v, ['web', 'phone'], themed);
  }
  function _screenPic(s, dev) {
    let v = s && s.image;
    if (v && typeof v === 'object') v = v[dev === 'phone' ? 'phone' : 'web'] || v.web || v.phone;
    if (v && typeof v === 'object') v = v[_appTheme()] || v.light || v.dark;
    return typeof v === 'string' ? v : '';
  }
  function _hasTpl(r) { return !!(r && !r.other && (r.screens || r.fit || r.effort || r.undo || r.risk || _SWOT.some(q => r[q[0]]))); }
  function _cap1(s) { s = String(s || ''); return s.charAt(0).toUpperCase() + s.slice(1); }
  // Pictures in links / needs.look that belong to one of a Decide card's
  // choices: Map(link -> reply key).  Empty for any other card.
  const _OPT_LABEL = /^\s*(?:option\s+|choice\s+|variant\s+)?([A-Za-z0-9]{1,3})\s*(?:[:.)\u00b7\-\u2013\u2014]|$)/i;
  function _optionImages(m) {
    const out = new Map(), n = (m && m.needs) || {};
    if (n.kind !== 'decision' || !Array.isArray(n.replies) || !n.replies.length) return out;
    const keys = n.replies.map((r, i) => [String((r && r.key) || String.fromCharCode(65 + i)), String((r && r.label) || '').toLowerCase()]);
    [].concat(m.links || [], n.look || []).filter(isImg).forEach(l => {
      const label = String(l.label || ''), mm = _OPT_LABEL.exec(label);
      const hit = keys.find(([k, lab]) => (mm && mm[1].toUpperCase() === k.toUpperCase()) || (lab && label.toLowerCase().indexOf(lab) !== -1));
      if (hit) out.set(l, hit[0]);
    });
    return out;
  }
  const _isPic = u => /\.(?:png|jpe?g|webp|gif)(?:[?#]|$)/i.test(String(u || ''));
  function _tplStage(r, dev, shot) {
    const s = r.screens[Math.min(shot, r.screens.length - 1)];
    if (_isPic(s.url)) return '<div class="fr pic"><img alt="' + esc(s.label || 'Screen') + '" src="' + esc(_loopbackPic(s.url) || _reviewSrc(s.url)) + '"></div>';
    const d = dev || s.device, devs = d === 'both' ? ['web', 'phone'] : [d];
    // Its page would reach this browser only through the preview proxy (the
    // phone, for another app on this machine), which cannot sign in to it or
    // run it: its picture for that device instead.
    if (s.image && /^\/api\/preview\/proxy\?/.test(_reviewSrc(s.url))) {
      return devs.map(k => '<div class="fr pic"><img alt="' + esc(s.label || 'Screen') + '" src="' + esc(_imgSrc(_screenPic(s, k))) + '"></div>').join('');
    }
    return devs.map(k => {
      const wh = _DEV[k], ifr = '<iframe title="' + esc(s.label || 'Screen') + '" data-src="' + esc(_reviewSrc(s.url)) + '"></iframe>';
      return k === 'web'
        ? '<div class="fr web" data-w="' + wh[0] + '" data-h="' + wh[1] + '"><div class="chrome"><i></i><i></i><i></i><span>' + esc(s.label || s.url) + '</span></div><div class="scr">' + ifr + '</div></div>'
        : '<div class="fr phone" data-w="' + wh[0] + '" data-h="' + wh[1] + '"><div class="scr">' + ifr + '</div></div>';
    }).join('');
  }
  // The Web / Phone / Both switch (and a switch per screen when a choice has
  // several) sit in the popup's top bar, beside the choices and Choose
  // (owner's pick, Layout 4 of static/scratch/decide-topbar.html, 2026-10-28:
  // "put all of it into that topnav so i can distinguish the wrapper from
  // what im reviewing, and web need not be so small").
  function _tplBar(r, dev, shot) {
    if (!r || !r.screens) return '';
    const s = r.screens[Math.min(shot, r.screens.length - 1)], cur = dev || s.device;
    const pics = r.screens.every(x => _isPic(x.url));
    return (pics ? '' : '<span class="tp-seg">' + [['web', 'Web'], ['phone', 'Phone'], ['both', 'Both']].map(v =>
      '<button type="button" data-dev="' + v[0] + '" class="' + (cur === v[0] ? 'on' : '') + '">' + v[1] + '</button>').join('') + '</span>') +
      (r.screens.length > 1 ? '<span class="tp-seg">' + r.screens.map((x, j) => '<button type="button" data-shot="' + j + '" class="' + (j === shot ? 'on' : '') + '">' +
        esc(x.label || 'Screen ' + (j + 1)) + '</button>').join('') + '</span>' : '');
  }
  /** The body of a choice with a read-out or screens.  The question, the
   *  choice's key, label and detail are in the top bar, not here: the body is
   *  only what is being reviewed, and a screen fills it. */
  function _tplHTML(d, r, n, dev, shot) {
    let h = '<div class="tp' + (r.screens ? ' has-stage' : '') + '">';
    if (r.screens) {
      const s = r.screens[Math.min(shot, r.screens.length - 1)], cur = dev || s.device;
      h += '<div class="tp-stage' + (cur === 'both' ? ' both' : '') + '">' + _tplStage(r, dev, shot) + '</div>';
    }
    const lv = v => _LEVEL[String(v || '').toLowerCase()];
    const m = [];
    if (r.effort) m.push('<span class="m">Effort' + (lv(r.effort) != null ? ' <span class="dots">' + [1, 2, 3].map(j => '<i class="' + (j <= lv(r.effort) ? 'f' : '') + '"></i>').join('') + '</span>' : '') + ' <b>' + esc(_cap1(r.effort)) + '</b></span>');
    if (r.undo) m.push('<span class="m">Easy to undo <b>' + esc(_cap1(r.undo)) + '</b></span>');
    if (r.risk) m.push('<span class="m">Risk <b>' + esc(_cap1(r.risk)) + '</b></span>');
    if (m.length) h += '<div class="tp-meters">' + m.join('') + '</div>';
    const qs = _SWOT.filter(q => r[q[0]]);
    if (qs.length) h += '<div class="tp-swot">' + qs.map(q => '<div class="q ' + q[1] + '"><div class="qh"><span class="g">' + q[2] + '</span>' + q[3] + '</div><ul>' +
      r[q[0]].map(x => '<li>' + inl(x) + '</li>').join('') + '</ul></div>').join('') + '</div>';
    if (r.fit) h += '<div class="tp-fit"><b>Pick this if</b> ' + inl(r.fit) + '</div>';
    return h + '</div>';
  }
  // Each screen shows ONLY its own option, not the whole page: the frame
  // finds the option named by the url's #fragment (an id or data-option),
  // else the reply's key ([data-option="A"], v-a, a heading "A ·"…), and
  // hides everything around it, as the Decide page's own tabs do (_solo).
  // A page with no such option is shown whole.
  function _screenOption(d, frag, key) {
    let t = null;
    if (frag) {
      t = d.getElementById(frag) || d.querySelector('[data-option="' + frag.replace(/["\\]/g, '') + '" i]');
      if (!t) t = _findOption(d, frag.replace(/^(?:v|option|opt)-/i, ''));
    }
    return t || (key ? _findOption(d, key) : null);
  }
  function _loadFrames(scope, key) {
    scope.querySelectorAll('iframe[data-src]').forEach(f => {
      _matchTheme(f);
      const src = f.dataset.src, h = src.indexOf('#');
      let frag = '';
      try { frag = h === -1 ? '' : decodeURIComponent(src.slice(h + 1)); } catch (_e) { frag = src.slice(h + 1); }
      // A page of this app is loaded WITHOUT its #fragment: the option is
      // found and shown by _screenOption / _isolate below.  Loaded with it
      // ("standby-bar.html#v-d", where id="v-d" is the last section), the
      // browser scrolled to that section, in the page and in the popup
      // around the frame, so a choice opened scrolled to the bottom (owner,
      // 2026-10-28: "i open a variant and it opens as scrolled to bottom").
      const own = h !== -1 && src.charAt(0) === '/' && src.charAt(1) !== '/';
      const top = d => {
        try { f.contentWindow.scrollTo(0, 0); } catch (_e) { /* ignore */ }
        if (d && d.querySelectorAll) d.querySelectorAll('*').forEach(x => { if (x.scrollTop) x.scrollTop = 0; });
        for (let p = f.parentElement; p; p = p.parentElement) {
          if (p.classList.contains('lm-dz')) break;
          if (p.scrollTop) p.scrollTop = 0;
        }
      };
      f.addEventListener('load', () => {
        let d;
        try { d = f.contentDocument; } catch (_e) { return; }
        if (!d || !d.body) return;
        const t = _screenOption(d, frag, key);
        if (t) _isolate(d, [t], 16);
        // again once late content (fonts, frames inside the page) has laid out
        top(d);
        requestAnimationFrame(() => top(d));
        setTimeout(() => top(d), 400);
      });
      f.src = _themedSrc(own ? src.slice(0, h) : src);
      f.removeAttribute('data-src');
    });
    _fitFrames(scope);
  }
  // A screen is laid out at its device's real width and scaled to its frame.
  // In the Decide popup the stage fills the window under the top bar: a web
  // screen takes the whole width and height (laid out at 1280px, shrunk only
  // as far as the width needs, and showing as much of the page as fits), a
  // phone the full height.  Stacked (Both on a phone) it falls back to fitting
  // by width.
  function _fitFrames(scope) {
    scope.querySelectorAll('.fr').forEach(f => {
      const scr = f.querySelector('.scr'), ifr = f.querySelector('iframe'), w = +f.dataset.w, h = +f.dataset.h;
      if (!scr || !ifr || !w) return;
      const stage = f.closest('.tp.has-stage .tp-stage');
      const fill = stage && getComputedStyle(stage).flexDirection !== 'column';
      if (fill && f.classList.contains('web')) {
        const s = Math.min(1, scr.clientWidth / w);
        if (!s) return;
        scr.style.height = '';
        ifr.style.width = (scr.clientWidth / s) + 'px'; ifr.style.height = (scr.clientHeight / s) + 'px'; ifr.style.transform = 'scale(' + s + ')';
        return;
      }
      if (fill) {
        const cs = getComputedStyle(stage), room = stage.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom) - 18;
        const s = Math.min(1, room / h);
        if (!(s > 0)) return;
        f.style.width = Math.round(w * s + 18) + 'px';
        ifr.style.width = w + 'px'; ifr.style.height = h + 'px'; ifr.style.transform = 'scale(' + s + ')';
        scr.style.height = Math.round(h * s) + 'px';
        return;
      }
      f.style.width = '';
      const s = scr.clientWidth / w;
      if (!s) return;
      ifr.style.width = w + 'px'; ifr.style.height = h + 'px'; ifr.style.transform = 'scale(' + s + ')';
      scr.style.height = Math.round(h * s) + 'px';
    });
  }

  // ── Decide motion (owner's pick A, 2026-10-28: "way more premium transitions") ──
  // The popup grows out of the button that opened it and folds back into it;
  // the chat dims behind it; a choice slides in the direction you move; the
  // tab highlight glides; Choose shows a check before the fold.  Reduced
  // motion gets a short fade.  Nothing here waits on an animation to work:
  // the reply is sent before any of it runs.
  const _EXPO = 'cubic-bezier(.16,1,.3,1)', _IN = 'cubic-bezier(.4,0,1,1)', _INOUT = 'cubic-bezier(.65,0,.35,1)';
  const _rm = () => !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  function _anim(el, kf, o) {
    if (!el || !el.animate) return Promise.resolve();
    if (_rm()) {
      const op = k => (k.opacity == null ? 1 : k.opacity);
      kf = [{ opacity: op(kf[0]) }, { opacity: op(kf[kf.length - 1]) }];
      o = Object.assign({}, o, { duration: 140, delay: 0 });
    }
    const a = el.animate(kf, Object.assign({ fill: 'backwards' }, o));
    return a.finished.then(() => a, () => a);
  }
  // The button's box as a clip-path, or '' when it is not on screen.
  function _btnClip(btn) {
    if (!btn || !btn.isConnected) return '';
    const r = btn.getBoundingClientRect(), W = document.documentElement.clientWidth || window.innerWidth, H = window.innerHeight;
    if (!r.width || !r.height || r.bottom < 0 || r.top > H) return '';
    const p = v => Math.max(0, Math.round(v)) + 'px';
    return 'inset(' + p(r.top) + ' ' + p(W - r.right) + ' ' + p(H - r.bottom) + ' ' + p(r.left) + ' round 9px)';
  }
  const _FULL = 'inset(0px 0px 0px 0px round 0px)';
  function _stagger(pane, from, base, step) {
    pane.querySelectorAll('.slide > *, .tp > *').forEach((k, j) =>
      _anim(k, [Object.assign({ opacity: 0 }, from), { opacity: 1, transform: 'none' }], { duration: 420, delay: base + j * step, easing: _EXPO }));
  }

  let _dz = null;
  /** Close the Decide popup.  `animate` folds it back into its button
   *  (Close, Escape, Choose); without it, it goes at once (a new popup,
   *  a Review overlay, a newer copy of this file). */
  function _closeDecide(animate, sent) {
    if (!_dz) return;
    const z = _dz;
    _dz = null;
    _panelClosed();
    document.removeEventListener('keydown', z.onKey, true);
    window.removeEventListener('resize', z.onResize);
    const done = () => {
      z.el.remove(); if (z.scrim) z.scrim.remove();
      if (!_dz && !_ov) document.documentElement.classList.remove('lm-ov-open');
      if (sent && z.btn && z.btn.isConnected) _anim(z.btn, [{ opacity: 0, transform: 'scale(.92)' }, { opacity: 1, transform: 'none' }], { duration: 460, easing: _EXPO });
    };
    if (!animate) { done(); return; }
    z.el.style.pointerEvents = 'none';
    const clip = _btnClip(z.btn);
    z.el.querySelectorAll('.body, .top').forEach(x => _anim(x, [{ opacity: 1 }, { opacity: 0 }], { duration: 150, easing: _IN, fill: 'forwards' }));
    if (z.scrim) _anim(z.scrim, [{ opacity: 1 }, { opacity: 0 }], { duration: 420, delay: 60, fill: 'forwards' });
    (clip
      ? _anim(z.el, [{ clipPath: _FULL }, { clipPath: clip }], { duration: 420, delay: 70, easing: _INOUT, fill: 'forwards' })
      : _anim(z.el, [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'scale(.98)' }], { duration: 220, easing: _IN, fill: 'forwards' })
    ).then(done);
  }
  /** One full-screen panel at a time (owner, 2026-10-28: "sometimes like
   *  multiple get opened ... the opener [must] make sure only one at a time").
   *  Every opener calls this first.  It closes this copy's panel without
   *  touching history (the opener replaces the ?panel entry), tells every
   *  other copy of this file to close theirs (live-assets.js loads a newer
   *  copy beside the old one, and each keeps its own slot), and then removes
   *  any panel still in the page: a Decide popup still folding shut, or one
   *  drawn by a copy from before this rule. */
  const _CLOSE_ALL = 'vn-lm-close-panels';
  function _closeAll() {
    _switching = true;
    try { _closeDecide(); _closeReview(); } finally { _switching = false; }
    try { document.dispatchEvent(new CustomEvent(_CLOSE_ALL, { detail: _api })); } catch (_e) { /* no CustomEvent */ }
    document.querySelectorAll('body > .lm-ov, body > .lm-dz, body > .lm-dz-scrim').forEach(x => x.remove());
  }
  document.addEventListener(_CLOSE_ALL, e => {
    if (e.detail === _api) return;
    _navClosing = true;   // the copy that is opening owns the URL now
    try { _closeDecide(); _closeReview(); } finally { _navClosing = false; }
  });
  function _openDecide(root, btn) {
    _closeAll();
    let d = null;
    try { d = JSON.parse(btn.dataset.decide || ''); } catch (_e) { d = null; }
    const rs = (d && d.replies) || [];
    if (!rs.length) return;
    const src = d.page ? _reviewSrc(d.page) : '';
    // With a page, the first tab is "Now": the page's [data-option="now"]
    // section, how it is today, so every option can be compared with it and
    // kept (owner, 2026-10-28: "the decide page should show me current").
    const NOW = { key: 'Now', now: true, label: 'Keep it as it is', reply: 'Keep it as it is: no change' };
    const all = (src ? [NOW] : []).concat(rs, [{ key: 'Other', other: true }]);
    const keys = rs.map(r => r.key);
    let i = (src ? 1 : 0) + Math.max(0, rs.findIndex(r => r.recommended));
    const el = document.createElement('div');
    el.className = 'lm-dz';
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-modal', 'true');
    // Everything that frames the choice is in ONE top bar: the question, the
    // choice's key, label and detail, the choices, Web / Phone / Both, Choose
    // and Close.  The body below is only what is being reviewed (owner's pick,
    // Layout 4 of static/scratch/decide-topbar.html, 2026-10-28).
    el.innerHTML = '<div class="top"><span class="key"></span><div class="txt"><p class="q"' + (d.why ? ' title="' + esc(d.why) + '"' : '') + '>' + inl(d.ask) + '</p><div class="lab"></div></div>' +
      '<div class="tabs"><span class="pill" aria-hidden="true"></span>' + all.map((r, j) =>
        '<button type="button" class="tab' + (r.other ? ' other' : '') + '" data-j="' + j + '">' + esc(r.key) + '</button>').join('') + '</div>' +
      '<span class="ctl"></span><button type="button" class="go"></button>' +
      '<button type="button" class="xf" aria-label="Close">' + I.close + '</button></div>' +
      '<div class="body">' + (src ? '<iframe title="Decide"></iframe>' : '') + '</div>';
    const frame = el.querySelector('iframe'), body = el.querySelector('.body'), go = el.querySelector('.go'), cap = el.querySelector('.top .lab'), pill = el.querySelector('.pill');
    const top = el.querySelector('.top'), keyEl = top.querySelector('.key'), ctl = top.querySelector('.ctl');
    const sizeTop = () => el.style.setProperty('--dz-top', top.offsetHeight + 'px');
    // Screens open on Web whatever device the card named; Phone and Both are
    // a tap away (owner, 2026-10-28: "i want it to default to web").
    let ready = false, typed = '', dev = 'web', shot = 0;
    const head = d.why ? '<p class="why">' + inl(d.why) + '</p>' : '';   // the question itself is in the top bar
    const usesFrame = r => !!frame && !r.other && !_hasTpl(r);
    const placePill = instant => {
      const t = el.querySelectorAll('.tab')[i];
      if (!t) return;
      pill.style.transitionDuration = instant || _rm() ? '0ms' : '';
      pill.style.width = t.offsetWidth + 'px';
      pill.style.transform = 'translateX(' + t.offsetLeft + 'px)';
    };
    const solo = () => {
      const r = all[i];
      if (ready && usesFrame(r) && !_solo(frame, keys, r.now ? '~now' : r.key)) cap.innerHTML += ' <span class="nf">(not marked on this page)</span>';
    };
    const paneHTML = r => {
      if (r.other) return '<div class="slide">' + head + '<textarea placeholder="What do you want instead?"></textarea></div>';
      if (_hasTpl(r)) return _tplHTML(d, r, rs.length, dev, shot);
      // A choice with nothing to look at: its words ARE what is reviewed.
      return '<div class="slide"><div class="k">Choice ' + esc(r.key) + ' of ' + rs.length + '</div>' +
        '<div class="l">' + inl(r.label) + (r.recommended ? '<span class="rec">Recommended</span>' : '') + '</div>' +
        (r.detail ? '<div class="d">' + inl(r.detail) + '</div>' : '') + head + '</div>';
    };
    const wirePane = (pane, r) => {
      if (r.screens) _loadFrames(pane, r.key);
      const ta = pane.querySelector('textarea');
      if (!ta) return;
      ta.value = typed;
      ta.addEventListener('input', () => { typed = ta.value; go.disabled = !typed.trim(); });
      // Same send key as the chat composer: the saved Enter / Ctrl+Enter
      // choice, and Enter is a new line on a touch keyboard (_shouldSend, utils.js).
      ta.addEventListener('keydown', ev => {
        if (ev.isComposing) return;
        const send = typeof _shouldSend === 'function' ? _shouldSend(ev) : (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey));
        if (!send) return;
        ev.preventDefault();
        if (!go.disabled) go.click();
      });
      setTimeout(() => ta.focus({ preventScroll: true }), 0);
    };
    // dir: +1 / -1 for a move to the next / previous choice, 0 for a redraw;
    // `opening` draws without a swap (the open animation runs instead).
    const show = (dir, opening) => {
      const r = all[i];
      el.querySelectorAll('.tab').forEach((t, j) => t.classList.toggle('on', j === i));
      keyEl.textContent = r.other ? '?' : r.now ? 'Now' : r.key;
      cap.innerHTML = r.other ? '<b>Something else</b><span class="dt"> · say what you want instead</span>'
        : r.now ? '<b>Keep it as it is</b><span class="dt"> · how it is today</span>'
        : '<b>' + inl(r.label) + '</b>' + (r.recommended ? '<span class="rec">Recommended</span>' : '') + (r.detail ? '<span class="dt"> · ' + inl(r.detail) + '</span>' : '');
      ctl.innerHTML = _hasTpl(r) ? _tplBar(r, dev, shot) : '';
      placePill(opening);
      sizeTop();
      if (!opening) _anim(top.querySelector('.txt'), [{ opacity: 0 }, { opacity: 1 }], { duration: 260, easing: 'ease-out' });
      go.disabled = r.other ? !typed.trim() : false;
      go.textContent = r.other ? 'Send' : r.now ? 'Keep it as it is' : 'Choose ' + r.key;
      const old = body.querySelector('.pane:not(.leaving)');
      if (old) {
        if (opening) old.remove();
        else {
          old.classList.add('leaving');
          old.style.top = (-body.scrollTop) + 'px';
          _anim(old, [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: dir ? 'translateX(' + (-28 * dir) + 'px)' : 'translateY(-8px)' }],
            { duration: 170, easing: _IN, fill: 'forwards' }).then(() => old.remove());
        }
      }
      if (usesFrame(r)) {
        frame.style.display = '';
        body.scrollTop = 0;
        solo();
        if (!opening) _anim(frame, [{ opacity: 0, transform: dir ? 'translateX(' + (24 * dir) + 'px)' : 'none' }, { opacity: 1, transform: 'none' }], { duration: 380, delay: 60, easing: _EXPO });
        return;
      }
      if (frame) frame.style.display = 'none';
      const pane = document.createElement('div');
      pane.className = 'pane';
      pane.innerHTML = paneHTML(r);
      body.appendChild(pane);
      body.scrollTop = 0;
      wirePane(pane, r);
      if (!opening) _stagger(pane, { transform: dir ? 'translateX(' + (32 * dir) + 'px)' : 'translateY(10px)' }, 90, 30);
    };
    const move = j => {
      j = (j + all.length) % all.length;
      if (j === i) return;
      const dir = j > i ? 1 : -1;
      i = j; shot = 0;
      show(dir);
    };
    const restage = () => {
      const pane = body.querySelector('.pane:not(.leaving)'), r = all[i];
      const stg = pane && pane.querySelector('.tp-stage');
      if (!stg || !r.screens) return;
      const cur = dev || r.screens[Math.min(shot, r.screens.length - 1)].device;
      el.querySelectorAll('[data-dev]').forEach(b => b.classList.toggle('on', b.dataset.dev === cur));
      el.querySelectorAll('[data-shot]').forEach(b => b.classList.toggle('on', +b.dataset.shot === shot));
      stg.classList.toggle('both', cur === 'both');
      stg.innerHTML = _tplStage(r, dev, shot);
      _loadFrames(stg, r.key);
      stg.querySelectorAll('.fr').forEach((f, j) => _anim(f, [{ opacity: 0, transform: 'translateY(10px)' }, { opacity: 1, transform: 'none' }], { duration: 420, delay: j * 50, easing: _EXPO }));
    };
    if (frame) {
      frame.addEventListener('load', () => { frame._vnOpts = null; ready = true; setTimeout(() => { if (_dz && _dz.el === el) solo(); }, 30); });
      _matchTheme(frame);
      frame.src = _themedSrc(src);
    }
    el.addEventListener('click', ev => {
      if (ev.target.closest('.xf')) { _closeDecide(true); return; }
      const t = ev.target.closest('.tab');
      if (t) { move(Number(t.dataset.j)); return; }
      const dv = ev.target.closest('[data-dev]');
      if (dv) { dev = dv.dataset.dev; restage(); return; }
      const sh = ev.target.closest('[data-shot]');
      if (sh) { shot = Number(sh.dataset.shot); restage(); return; }
      if (ev.target.closest('.go') && !go.disabled) {
        const r = all[i], text = r.other ? typed.trim() : r.reply;
        if (!text) return;
        if (!_send(text)) { if (typeof showToast === 'function') showToast('Couldn’t send that. Type your choice in the box below.', true); return; }
        btn.dataset.sent = '1';
        btn.classList.add('sent');
        btn.innerHTML = I.check + '<span>Sent: “' + esc(text) + '”</span>';
        _markAnswered(btn.closest('.lm'), text);
        go.disabled = true;
        go.innerHTML = I.check + '<span>Sent</span>';
        _anim(go, [{ transform: 'scale(1)' }, { transform: 'scale(.94)' }, { transform: 'scale(1.04)' }, { transform: 'scale(1)' }], { duration: 380, easing: 'ease-out' });
        const me = _dz;
        setTimeout(() => { if (_dz === me) _closeDecide(true, true); }, _rm() ? 0 : 520);
      }
    });
    const onKey = ev => {
      if (ev.key === 'Escape') { ev.stopPropagation(); _closeDecide(true); return; }
      if (ev.target && ev.target.tagName === 'TEXTAREA') return;
      if (ev.key === 'ArrowRight' || ev.key === 'ArrowLeft') move(i + (ev.key === 'ArrowRight' ? 1 : -1));
    };
    const onResize = () => { placePill(true); sizeTop(); _fitFrames(el); };
    document.addEventListener('keydown', onKey, true);
    window.addEventListener('resize', onResize);
    const scrim = document.createElement('div');
    scrim.className = 'lm-dz-scrim';
    document.body.appendChild(scrim);
    document.body.appendChild(el);
    document.documentElement.classList.add('lm-ov-open');
    _dz = { el, onKey, onResize, btn, scrim };
    const dl = [...document.querySelectorAll('#live-log .lm-decide')], di = dl.indexOf(btn);
    _panelOpened(di >= 0 ? 'decide:' + (dl.length - di) : '');
    show(0, true);
    // the open: grow out of the button (measured after the page lost its scrollbar)
    const clip = _btnClip(btn);
    if (clip) _anim(el, [{ clipPath: clip }, { clipPath: _FULL }], { duration: 560, easing: _EXPO });
    else _anim(el, [{ opacity: 0, transform: 'scale(.98)' }, { opacity: 1, transform: 'none' }], { duration: 320, easing: _EXPO });
    _anim(scrim, [{ opacity: 0 }, { opacity: 1 }], { duration: 420, easing: 'ease-out' });
    _anim(top, [{ opacity: 0, transform: 'translateY(-12px)' }, { opacity: 1, transform: 'none' }], { duration: 440, delay: 200, easing: _EXPO });
    const p0 = body.querySelector('.pane');
    if (p0) _stagger(p0, { transform: 'translateY(14px)' }, 170, 40);
    else if (frame) _anim(frame, [{ opacity: 0 }, { opacity: 1 }], { duration: 400, delay: 160, easing: _EXPO });
  }

  let _ov = null;
  function _closeReview() {
    if (!_ov) return;
    _panelClosed();
    _ov.el.remove();
    document.documentElement.classList.remove('lm-ov-open');
    document.removeEventListener('keydown', _ov.onKey, true);
    _ov = null;
  }
  /** A link or screenshot full screen over the chat, with Close and a way out
   *  to a real tab.  Shares the review overlay's slot, Escape and styles. */
  function _openViewer(title, src, isImage, href) {
    _closeAll();
    if (!src) return;
    const el = document.createElement('div');
    el.className = 'lm-ov lm-view';
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-modal', 'true');
    el.innerHTML = '<div class="lm-ov-top"><span class="t">' + esc(title) + '</span>' +
      '<a class="nt" href="' + esc(href || src) + '" target="_blank" rel="noopener" aria-label="Open in a new tab" title="Open in a new tab">' + I.arrow + '</a>' +
      '<button type="button" class="x" aria-label="Close">' + I.close + '</button></div>' +
      (isImage ? '<div class="lm-view-img"><img alt="' + esc(title) + '"></div>' : '<iframe class="lm-ov-frame" title="' + esc(title) + '"></iframe>');
    document.body.appendChild(el);
    document.documentElement.classList.add('lm-ov-open');
    const vf = el.querySelector('iframe');
    if (vf) { _matchTheme(vf); vf.src = _themedSrc(src); } else el.querySelector('img').src = src;
    el.addEventListener('click', ev => { if (ev.target.closest('.x')) _closeReview(); });
    const onKey = ev => { if (ev.key === 'Escape') { ev.stopPropagation(); _closeReview(); } };
    document.addEventListener('keydown', onKey, true);
    _ov = { el, onKey };
    _panelOpened((isImage ? 'img:' : 'page:') + src);
  }
  /** A deliverable full screen, drawn as its format says, with Copy.  HTML
   *  goes in a sandboxed frame (no scripts, no same-origin): the page is the
   *  session's text, so it must never reach the app or its API. */
  function _openDeliverable(btn) {
    const dd = btn.closest('.dd') || btn.closest('.lm');
    const src = dd && dd.querySelector('.lm-row[data-copy]:not([data-copy-src])');
    if (!src) return;
    _closeAll();
    const body = src.dataset.copy || '', fmt = btn.dataset.dv || 'markdown', title = btn.dataset.title || 'Deliverable';
    const el = document.createElement('div');
    el.className = 'lm-ov lm-view lm-dvview';
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-modal', 'true');
    el.innerHTML = '<div class="lm-ov-top"><span class="t">' + esc(title) + '</span>' +
      '<button type="button" class="cp">' + I.copy + ' <span>Copy</span></button>' +
      '<button type="button" class="x" aria-label="Close">' + I.close + '</button></div>' +
      (fmt === 'html' ? '<iframe class="lm-ov-frame lm-dvframe" sandbox="allow-popups allow-popups-to-escape-sandbox" title="' + esc(title) + '"></iframe>'
        : '<div class="lm-dv-scroll"><div class="dv-body lm-dv-full">' + dvBodyHtml(fmt, body) + '</div></div>');
    document.body.appendChild(el);
    document.documentElement.classList.add('lm-ov-open');
    if (fmt === 'html') el.querySelector('iframe').srcdoc = body;
    el.addEventListener('click', async ev => {
      if (ev.target.closest('.x')) { _closeReview(); return; }
      const cp = ev.target.closest('.cp');
      if (cp && await _copy(body)) {
        cp.classList.add('done'); cp.querySelector('span').textContent = 'Copied';
        setTimeout(() => { cp.classList.remove('done'); cp.querySelector('span').textContent = 'Copy'; }, 1400);
      }
    });
    const onKey = ev => { if (ev.key === 'Escape') { ev.stopPropagation(); _closeReview(); } };
    document.addEventListener('keydown', onKey, true);
    _ov = { el, onKey };
    const all = [...document.querySelectorAll('#live-log .lm-dvopen')], i = all.indexOf(btn);
    _panelOpened(i >= 0 ? 'dv:' + (all.length - i) : '');
  }
  function _openReview(root, btn) {
    _closeAll();
    const src = _reviewSrc(btn.dataset.review);
    if (!src) return;
    const replies = [...root.querySelectorAll('.lm-rows .lm-row.reply')];
    const done = replies.length && replies[0].closest('.lm-rows').classList.contains('done');
    const headline = (root.querySelector('.lm-headline') || {}).textContent || '';
    const ask = (root.querySelector('.lm-ask') || {}).textContent || '';
    const el = document.createElement('div');
    el.className = 'lm-ov';
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-modal', 'true');
    el.innerHTML =
      '<div class="lm-ov-top"><span class="t">' + esc(headline) + '</span>' +
        '<button type="button" class="x" aria-label="Close">' + I.close + '</button></div>' +
      '<iframe class="lm-ov-frame" title="Review"></iframe>' +
      '<div class="lm-ov-pick">' +
        (ask ? '<div class="q">' + esc(ask) + '</div>' : '') +
        '<div class="opts">' + replies.map((r, i) => {
          const key = (r.querySelector('.tile') || {}).textContent || String.fromCharCode(65 + i);
          const tt = r.querySelector('.tt');
          const label = tt ? [...tt.childNodes].filter(n => !(n.classList && n.classList.contains('rec'))).map(n => n.textContent).join('').trim() : r.dataset.reply;
          const rec = !!(tt && tt.querySelector('.rec'));
          return '<button type="button" class="opt' + (r.classList.contains('picked') ? ' on' : '') + '" data-i="' + i + '" data-key="' + esc(key) + '">' +
            '<span class="k">' + esc(key) + '</span><span class="l">' + esc(label) + (rec ? '<span class="rec">Recommended</span>' : '') + '</span></button>';
        }).join('') + '</div>' +
        '<div class="lm-ov-note"></div>' +
        '<button type="button" class="send" disabled>' + (done ? 'Already sent' : 'Pick one above') + '</button>' +
      '</div>';
    // style the overlay in the card's colour
    const tone = getComputedStyle(root).getPropertyValue('--tone');
    if (tone) el.style.setProperty('--tone', tone);
    document.body.appendChild(el);
    document.documentElement.classList.add('lm-ov-open');
    const frame = el.querySelector('iframe');
    _matchTheme(frame);
    frame.src = _themedSrc(src);
    const send = el.querySelector('.send');
    let picked = -1;
    const findOption = _findOption;
    const scrollTo = key => {
      let found = false;
      try {
        const d = frame.contentDocument;
        const k = String(key || '').trim();
        const t = d && k && findOption(d, k);
        if (t) {
          found = true;
          t.scrollIntoView({ behavior: 'smooth', block: 'start' });
          t.animate([{ outline: '2px solid rgb(' + (tone || '96,158,255') + ')', outlineOffset: '4px' }, { outline: '2px solid transparent', outlineOffset: '4px' }], { duration: 1400 });
        }
      } catch (_e) { /* a page we can't reach into: the pick still works */ }
      return found;
    };
    el.addEventListener('click', ev => {
      if (ev.target.closest('.x')) { _closeReview(); return; }
      const o = ev.target.closest('.opt');
      if (o && !done) {
        picked = Number(o.dataset.i);
        el.querySelectorAll('.opt').forEach(x => x.classList.toggle('on', x === o));
        send.disabled = false;
        send.textContent = 'Choose ' + o.dataset.key;
        const note = el.querySelector('.lm-ov-note');
        if (note) note.textContent = scrollTo(o.dataset.key) ? '' : 'This page doesn’t mark option ' + o.dataset.key + '; it is on the page above.';
        return;
      }
      if (ev.target.closest('.send') && picked >= 0 && replies[picked]) {
        if (_choose(root, replies[picked])) _closeReview();
        else if (typeof showToast === 'function') showToast('Couldn’t send that. Type your choice in the box below.', true);
      }
    });
    const onKey = ev => { if (ev.key === 'Escape') { ev.stopPropagation(); _closeReview(); } };
    document.addEventListener('keydown', onKey, true);
    _ov = { el, onKey };
    _panelOpened('review:' + btn.dataset.review);
  }

  // ── Full-screen panels have a URL (owner, 2026-10-28: "anytime i click a
  //    panel and it goes fullscreen i want the url to work so i can use
  //    browser forward back buttons, and refresh").  Opening one adds
  //    ?panel=<kind>:<what> as a new history entry; Back closes it, Forward
  //    and a refresh open it again.  Kinds: decide:<n> (the n-th Decide
  //    button from the end of the thread), review:<url>, img:<src>,
  //    page:<src>.  A URL only reopens what a card in the open thread shows,
  //    so a crafted link can never frame some other page in the app.
  const _P = 'panel';
  const _hasHistory = typeof history !== 'undefined' && typeof location !== 'undefined' && !!location.href && typeof window.addEventListener === 'function';
  const _bootState = _hasHistory ? history.state : null;   // read before app.js / workspace.js replace it
  let _curKey = '';      // the panel on screen
  let _urlKey = _hasHistory ? (new URL(location.href).searchParams.get(_P) || '') : '';   // the panel in the URL, as last known
  let _lastBase = '';    // the URL without ?panel when this file last wrote a panel entry
  let _switching = false, _navClosing = false;
  function _panelUrl(key) {
    const u = new URL(location.href);
    if (key) u.searchParams.set(_P, key); else u.searchParams.delete(_P);
    return u;
  }
  function _panelOpened(key) {
    _curKey = key || '';
    if (!_hasHistory || !key || _urlKey === key) return;          // reopened from the URL: it is already there
    const st = Object.assign({}, history.state || {}, { vnPanel: key });
    if (_urlKey && history.state && history.state.vnPanel) history.replaceState(st, '', _panelUrl(key));
    else history.pushState(st, '', _panelUrl(key));
    _urlKey = key;
    _lastBase = _panelUrl(null).href;
  }
  function _panelClosed() {
    _curKey = '';
    if (_switching || _navClosing || !_urlKey) return;
    // Our own entry: step back over it, so Forward reopens the panel (the
    // popstate handler sees ?panel go and keeps the app's handlers out).  A
    // pasted link has no entry of ours behind it: drop the parameter in place.
    if (history.state && history.state.vnPanel) { _lastBase = _panelUrl(null).href; history.back(); return; }
    _urlKey = '';
    const st = Object.assign({}, history.state || {});
    delete st.vnPanel;
    history.replaceState(st, '', _panelUrl(null));
  }
  function _findPanel(key) {
    const log = document.getElementById('live-log');
    if (!log) return null;
    const i = key.indexOf(':'), kind = key.slice(0, i), arg = key.slice(i + 1);
    if (kind === 'img' || kind === 'page') {
      const r = [...log.querySelectorAll('.lm-row[data-view]')].reverse().find(x => x.dataset.view === arg && (x.dataset.img === '1') === (kind === 'img'));
      return r && (() => _openViewer(r.dataset.title || '', arg, kind === 'img', r.dataset.href || arg));
    }
    if (kind === 'review') {
      const b = [...log.querySelectorAll('.lm-review')].reverse().find(x => x.dataset.review === arg);
      return b && (() => _openReview(b.closest('.lm'), b));
    }
    if (kind === 'decide') {
      const l = [...log.querySelectorAll('.lm-decide')], b = l[l.length - Number(arg)];
      return b && !b.dataset.sent && (() => _openDecide(b.closest('.lm'), b));
    }
    if (kind === 'dv') {
      const l = [...log.querySelectorAll('.lm-dvopen')], b = l[l.length - Number(arg)];
      return b && (() => _openDeliverable(b));
    }
    return null;
  }
  // The thread renders after the session opens, so wait for the card.
  function _restorePanel(key, tries) {
    if (window.VNLastCard !== _api || _urlKey !== key || _curKey === key) return;
    const open = _findPanel(key);
    if (open) {
      open();
      _lastBase = _panelUrl(null).href;
      // After a refresh, app.js rewrote this entry's state; mark it ours again
      // so closing steps back to the entry it was opened from.
      if (_bootState && _bootState.vnPanel === key && !(history.state && history.state.vnPanel))
        history.replaceState(Object.assign({}, history.state || {}, { vnPanel: key }), '', location.href);
      return;
    }
    if ((tries || 0) < 60) { setTimeout(() => _restorePanel(key, (tries || 0) + 1), 300); return; }
    _urlKey = '';
    history.replaceState(history.state, '', _panelUrl(null));   // nothing to show: drop it
  }
  // Capture phase on window runs before the app's own popstate handlers, so a
  // move that only opens or closes a panel never re-routes the chat, the
  // folder or the Workflow view underneath.
  if (_hasHistory) window.addEventListener('popstate', e => {
    if (window.VNLastCard !== _api) return;
    const key = new URL(location.href).searchParams.get(_P) || '';
    const base = _panelUrl(null).href;
    if (key !== _urlKey && base === _lastBase) e.stopImmediatePropagation();
    _urlKey = key;
    _lastBase = base;
    if (_curKey && _curKey !== key) {
      _navClosing = true;
      try { _closeDecide(); _closeReview(); } finally { _navClosing = false; }
    }
    if (key && key !== _curKey) _restorePanel(key, 0);
  }, true);
  if (_urlKey) setTimeout(() => _restorePanel(_urlKey, 0), 0);

  // ── The sticky title bar (titled cards).  The card's head scrolls away
  //    normally; once it has, the slim .lm-stick bar (sticky, zero height,
  //    so it never moves the layout) fades in at the top of the thread until
  //    the card's end leaves too.  One passive capture listener for every
  //    scroller, at most one pass per frame.
  function _scrollerOf(el) {
    for (let p = el.parentElement; p; p = p.parentElement) {
      const o = getComputedStyle(p).overflowY;
      if (o === 'auto' || o === 'scroll') return p;
    }
    return document.scrollingElement || document.documentElement;
  }
  // How far the bar sits below the scroller's top: 0, or below the "you asked" pill (--vn-stick-top, last-card.css).
  function _stickOffset(sc) {
    return sc && sc.nodeType === 1 ? (parseFloat(getComputedStyle(sc).getPropertyValue('--vn-stick-top')) || 0) : 0;
  }
  function _stickSync(sc) {
    const cards = sc.querySelectorAll ? sc.querySelectorAll('.lm.lm-titled') : [];
    if (!cards.length) return;
    const top = (sc === document.scrollingElement || sc === document.documentElement ? 0 : sc.getBoundingClientRect().top) + _stickOffset(sc);
    cards.forEach(lm => {
      const hd = lm.querySelector('.lm-head');
      if (!hd) return;
      const r = lm.getBoundingClientRect(), hb = hd.getBoundingClientRect().bottom;
      lm.classList.toggle('stuck', hb < top + 4 && r.bottom > top + 72);
    });
  }
  let _stickRaf = 0;
  const _stickPending = new Set();
  document.addEventListener('scroll', e => {
    if (window.VNLastCard !== _api) return;
    const sc = e.target === document ? (document.scrollingElement || document.documentElement) : e.target;
    if (!sc || !sc.querySelector || !sc.querySelector('.lm.lm-titled')) return;
    _stickPending.add(sc);
    if (_stickRaf) return;
    _stickRaf = requestAnimationFrame(() => { _stickRaf = 0; _stickPending.forEach(_stickSync); _stickPending.clear(); });
  }, { capture: true, passive: true });
  function _stickJump(root, toAction) {
    const sc = _scrollerOf(root);
    const target = toAction ? (root.querySelector('.lm-needs, .lm-quiet') || root) : root;
    const top = (sc === document.scrollingElement || sc === document.documentElement ? 0 : sc.getBoundingClientRect().top) + _stickOffset(sc);
    sc.scrollBy({ top: target.getBoundingClientRect().top - top - (toAction ? 52 : 8), behavior: 'smooth' });
  }

  // The deliverable preview is a role=button div: Enter / Space open it too.
  document.addEventListener('keydown', e => {
    if (window.VNLastCard !== _api || (e.key !== 'Enter' && e.key !== ' ')) return;
    const dvo = e.target && e.target.closest && e.target.closest('.lm-dvopen');
    if (dvo && dvo === e.target) { e.preventDefault(); _openDeliverable(dvo); }
  });

  document.addEventListener('click', async e => {
    if (window.VNLastCard !== _api) return;   // a newer copy of this file took over (live-assets.js)
    const root = e.target.closest && e.target.closest('.lm');
    if (!root) return;
    const sk = e.target.closest('.lm-stick .in');
    if (sk) { _stickJump(root, !!e.target.closest('.jump')); return; }
    const em = e.target.closest('.lm-emore');
    if (em) {
      const w = em.closest('.lm-earlier'), open = !w.classList.contains('open');
      w.classList.toggle('open', open);
      em.setAttribute('aria-expanded', String(open));
      return;
    }
    const ej = e.target.closest('[data-ejump]');
    if (ej) {
      const w = ej.closest('.lm-earlier'), t = w && w._tasks && w._tasks[Number(ej.dataset.ejump)];
      if (t && t.el && t.el.isConnected) {
        t.el.scrollIntoView({ behavior: 'smooth', block: 'start' });
        t.el.classList.add('lm-eflash');
        setTimeout(() => t.el.classList.remove('lm-eflash'), 1600);
      }
      return;
    }
    const ql = e.target.closest('.lm-qline');
    if (ql) {
      const w = ql.closest('.lm-qwrap'), open = !w.classList.contains('open');
      w.classList.toggle('open', open);
      ql.setAttribute('aria-expanded', String(open));
      return;
    }
    const qj = e.target.closest('[data-qjump]');
    if (qj) {
      const w = qj.closest('.lm-qwrap'), it = w && w._items && w._items[Number(qj.dataset.qjump)];
      const bub = it && _queueBubble(it.text);
      if (bub) {
        bub.scrollIntoView({ behavior: 'smooth', block: 'center' });
        bub.classList.add('lm-qflash');
        setTimeout(() => bub.classList.remove('lm-qflash'), 1600);
      } else if (typeof showToast === 'function') showToast('That message is further up than this chat has loaded.');
      return;
    }
    const dc = e.target.closest('.lm-decide');
    if (dc) { if (!dc.dataset.sent) _openDecide(root, dc); return; }
    const vd = e.target.closest('[data-verdict]');
    if (vd) {
      const sid = (typeof liveSessionId !== 'undefined') ? liveSessionId : null;
      const act = vd.dataset.verdict;
      if (!sid) return;
      if ((act === 'done' || act === 'gotit' || act === 'sent') && window.SessionVerdict) window.SessionVerdict.accept(sid);
      // Do what it suggested: says yes to the card's offer.
      else if (act === 'offer') {
        if (!vd.disabled && _send('Yes, do that: ' + (vd.dataset.offer || ''))) {
          vd.disabled = true; const tt = vd.querySelector('.tt'); if (tt) tt.textContent = 'Sent: do what it suggested';
        }
        return;
      }
      else if (act === 'standby' && window.SessionVerdict && window.SessionVerdict.standby) window.SessionVerdict.standby(sid);
      else if (act === 'unpark' && window.SessionVerdict && window.SessionVerdict.unpark) window.SessionVerdict.unpark(sid);
      // Undo wakes it again: the sidebar's own double-tap wakes a sleeping session.
      else if (act === 'undo') {
        // Undo a Done: clear the mark and wake it (a double-click on Done now
        // means Standby, so the card calls the plain wake).
        if (window.SessionVerb && window.SessionVerb.holdOf && window.SessionVerb.holdOf(sid) === 'done') window.SessionVerb.setHold(sid, null);
        if (typeof window._sleepOrWake === 'function') window._sleepOrWake(sid, null);
        else if (typeof window._dblclickSleepIfIdle === 'function') window._dblclickSleepIfIdle(sid, null);
      }
      else if (Object.prototype.hasOwnProperty.call(_VERDICT_PREFIX, act)) _verdictPrefix(act);
      _syncDone(); requestAnimationFrame(_syncDone);
      return;
    }
    const rv = e.target.closest('.lm-review');
    if (rv) { _openReview(root, rv); return; }
    const reply = e.target.closest('.lm-row.reply');
    if (reply) { _choose(root, reply); return; }
    const bk = e.target.closest('.lm-backto .go');
    if (bk) {
      if (bk.disabled) return;
      if (_send(bk.dataset.back)) { bk.disabled = true; bk.textContent = 'Sent'; bk.closest('.lm-backto').classList.add('sent'); }
      else if (typeof showToast === 'function') showToast('Couldn’t send that. Type it in the box below.', true);
      return;
    }
    // Also found → Deal with it: sends the finding's reply as the user's message.
    const fd = e.target.closest('.lm-find .go');
    if (fd) {
      if (fd.disabled) return;
      if (_send(fd.dataset.find)) { fd.disabled = true; fd.textContent = 'Sent'; fd.closest('.lm-find').classList.add('sent'); }
      else if (typeof showToast === 'function') showToast('Couldn’t send that. Type it in the box below.', true);
      return;
    }
    const dvo =e.target.closest('.lm-dvopen');
    if (dvo) { _openDeliverable(dvo); return; }
    const vw = e.target.closest('.lm-row[data-view]');
    if (vw) { e.preventDefault(); _openViewer(vw.dataset.title || '', vw.dataset.view, vw.dataset.img === '1', vw.dataset.href || vw.dataset.view); return; }
    const link = e.target.closest('.lm-row[data-href]');
    if (link) {
      e.preventDefault();
      const u = link.dataset.href || '';
      if (/^(https?:|\/)/.test(u)) window.open(u, '_blank', 'noopener');
      else if (u && await _copy(u) && typeof showToast === 'function') showToast('Path copied: ' + u);
      return;
    }
    const cf = e.target.closest('.lm-row[data-copy-src]');
    if (cf) { _copyAsFile(cf); return; }
    const cp = e.target.closest('.lm-row[data-copy]');
    if (cp) {
      if (!(await _copy(cp.dataset.copy))) return;
      const vb = cp.querySelector('.vb'), old = vb.innerHTML;
      cp.classList.add('copied'); vb.innerHTML = I.check + ' Copied';
      setTimeout(() => { cp.classList.remove('copied'); vb.innerHTML = old; }, 1400);
      return;
    }
    const exp = e.target.closest('.dv-expand');
    if (exp) {
      const dv = exp.closest('.dv'); dv.classList.toggle('open'); exp.classList.toggle('open');
      exp.querySelector('span').textContent = dv.classList.contains('open') ? 'Show less' : 'Show all';
      return;
    }
    const more = e.target.closest('.lm-more');
    if (more && more.nextElementSibling) { more.classList.toggle('open'); more.nextElementSibling.classList.toggle('open'); }
  });

  // ── Live timing: one shared ticker, only while a timing card is on the page ──
  let _timer = null;
  function _tick() {
    const els = document.querySelectorAll('.lm .tm[data-clock]');
    if (!els.length) { if (_timer) { clearInterval(_timer); _timer = null; } return; }
    if (!_timer) _timer = setInterval(_tick, 1000);
    const now = Date.now();
    els.forEach(el => {
      const start = Number(el.dataset.clock), eta = Number(el.dataset.eta);
      if (!start || !eta) return;
      const t = Math.max(0, now - start), over = t > eta;
      el.querySelector('[data-f="since"]').textContent = fmtDur(t);
      el.querySelector('[data-f="delta"]').textContent = (over ? '+' : '') + fmtDur(Math.abs(eta - t));
      el.querySelector('[data-f="dlabel"]').textContent = over ? 'overdue' : 'left';
      el.classList.toggle('is-over', over);
      const scale = Math.max(eta * 1.25, t * 1.06);
      el.querySelector('.fill').style.width = (Math.min(t, eta) / scale * 100) + '%';
      el.querySelector('.over').style.left = (eta / scale * 100) + '%';
      el.querySelector('.over').style.width = (over ? (t - eta) / scale * 100 : 0) + '%';
      el.querySelector('.mark').style.left = (eta / scale * 100) + '%';
    });
  }

  // "Explain more" opens and closes in place (its own listener, so it never
  // depends on the order of the card's other click handling).
  document.addEventListener('click', e => {
    if (window.VNLastCard !== _api) return;
    const xm = e.target && e.target.closest && e.target.closest('.lm-xpl-tg');
    if (!xm) return;
    const o = xm.parentElement.classList.toggle('open');
    xm.setAttribute('aria-expanded', String(o));
  });
  const _api = { render, renderIn, openReview: _openReview, closeReview: _closeReview, reviewSrc: _reviewSrc,
    openDecide: _openDecide, closeDecide: _closeDecide, syncDone: _syncDone, syncAnswered: _syncAnswered, markAnswered: _markAnswered, themedSrc: _themedSrc, screenOption: _screenOption, tplStage: _tplStage, deliverableBody: dvBodyHtml };
  window.VNLastCard = _api;
})();
