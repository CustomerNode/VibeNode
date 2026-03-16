/* extract.js — code block extraction drawer */

let extractBlocks = [];

async function openExtract() {
  if (!activeId) return;
  const body = document.getElementById('extract-body');
  body.innerHTML = '<p style="padding:20px;color:#555;font-size:12px;">Loading\u2026</p>';
  document.getElementById('extract-drawer').classList.add('open');
  try {
    const r = await fetch('/api/extract-code/' + activeId);
    const d = await r.json();
    extractBlocks = d.blocks || [];
    renderExtractBlocks(extractBlocks);
  } catch(e) {
    body.innerHTML = '<p style="padding:20px;color:#cc4444;font-size:12px;">Error loading code blocks.</p>';
  }
}

function closeExtract() {
  document.getElementById('extract-drawer').classList.remove('open');
}

function renderExtractBlocks(blocks) {
  const body = document.getElementById('extract-body');
  const copyAll = document.getElementById('extract-copy-all');
  if (!blocks.length) {
    body.innerHTML = '<p style="padding:20px;color:#555;font-size:13px;text-align:center;">No code blocks found in this session.</p>';
    copyAll.style.display = 'none';
    return;
  }
  copyAll.style.display = 'inline-block';
  body.innerHTML = blocks.map((b, i) => {
    const langBadge = b.is_shell
      ? `<span class="code-shell-badge">${escHtml(b.language||'shell')}</span>`
      : `<span class="code-lang-badge">${escHtml(b.language||'text')}</span>`;
    const dupBadge = b.duplicate_of !== null && b.duplicate_of !== undefined
      ? `<span class="code-dup-badge">duplicate of #${b.duplicate_of+1}</span>` : '';
    const fname = b.inferred_filename ? `<span class="code-filename">${escHtml(b.inferred_filename)}</span>` : '';
    const preview = escHtml((b.content||'').slice(0, 2000));
    return `<div class="code-block-card">
      <div class="code-block-header">
        <div style="display:flex;gap:6px;align-items:center;">${langBadge}${dupBadge}${fname}</div>
        <button class="code-copy-btn" onclick="copyBlock(${i})">Copy</button>
      </div>
      <pre class="code-block-pre">${preview}</pre>
    </div>`;
  }).join('');
}

function copyBlock(i) {
  const b = extractBlocks[i];
  if (!b) return;
  navigator.clipboard.writeText(b.content).then(() => {
    const btns = document.querySelectorAll('.code-copy-btn');
    if (btns[i]) { btns[i].textContent = 'Copied!'; setTimeout(() => btns[i].textContent = 'Copy', 1200); }
  });
}

function triggerExport() {
  if (!activeId) return;
  const a = document.createElement('a');
  a.href = '/api/export-project/' + activeId;
  a.download = 'session_export.zip';
  a.click();
}

document.getElementById('extract-copy-all').addEventListener('click', () => {
  const all = extractBlocks.map((b, i) => `// --- Block ${i+1}: ${b.inferred_filename||b.language||'code'} ---\n${b.content}`).join('\n\n');
  navigator.clipboard.writeText(all).then(() => {
    const btn = document.getElementById('extract-copy-all');
    btn.textContent = 'Copied!'; setTimeout(() => btn.textContent = 'Copy All', 1500);
  });
});

document.getElementById('extract-export-btn').addEventListener('click', () => {
  if (activeId) triggerExport();
});
