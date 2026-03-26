/* smart-copy.js — auto-detect copyable content blocks and add copy buttons */

// SVG icons
const _COPY_ICON = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
const _CHECK_ICON = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polyline points="20 6 9 17 4 12"/></svg>';

/**
 * Scan a DOM element for copyable content blocks and add copy buttons.
 * Call this after inserting rendered markdown into the page.
 * @param {HTMLElement} container - the .msg-body or .msg-content element
 * @param {string} rawText - the original markdown source (for extracting raw content)
 */
function addSmartCopyButtons(container, rawText) {
  if (!container) return;

  // 1. Code blocks
  _addCodeCopyButtons(container);

  // 2. Email blocks
  _addEmailCopyButton(container, rawText || '');

  // 3. Tables
  _addTableCopyButtons(container);
}

// -----------------------------------------------------------------------
// Code blocks
// -----------------------------------------------------------------------
function _addCodeCopyButtons(container) {
  container.querySelectorAll('pre').forEach(pre => {
    if (pre.querySelector('.smart-copy-btn')) return;
    const code = pre.querySelector('code');
    const text = (code || pre).textContent;
    if (!text.trim()) return;

    pre.style.position = 'relative';
    const btn = _makeCopyBtn(text, 'Copy code');
    btn.style.top = '6px';
    btn.style.right = '6px';
    pre.appendChild(btn);
  });
}

// -----------------------------------------------------------------------
// Email detection
// -----------------------------------------------------------------------

// Greeting patterns (start of email)
const _EMAIL_GREETINGS = /^(Hi|Hello|Hey|Dear|Good morning|Good afternoon|Good evening|Greetings)\b[^.!?\n]{0,40}[,:]?\s*$/;

// Sign-off patterns (formal closings)
const _EMAIL_SIGNOFFS = /^(Best|Best regards|Regards|Kind regards|Warm regards|Thanks|Thank you|Many thanks|Cheers|Sincerely|Yours truly|All the best|Take care|Looking forward|With appreciation|Respectfully|Cordially)[,.]?\s*$/;

// Claude's outro lines that come AFTER the email (not part of it)
const _CLAUDE_OUTRO = /^(Let me know|Feel free|I can adjust|Would you like|Want me to|Happy to|Shall I|I hope this|Does this|Should I|Here's what I|Note:|---)/;

function _addEmailCopyButton(container, rawText) {
  if (!rawText) return;

  const email = _extractEmail(rawText);
  if (!email) return;

  // Find the DOM element containing the greeting line
  const firstLine = email.split('\n')[0].trim();
  const startEl = _findNodeContaining(container, firstLine);
  if (!startEl) return;

  // Create wrapper container for the email
  const wrap = document.createElement('div');
  wrap.className = 'smart-copy-email-wrap';

  // Insert wrapper before the first email element
  startEl.parentNode.insertBefore(wrap, startEl);

  // Add label inside wrapper
  const label = document.createElement('div');
  label.className = 'smart-copy-email-label';
  label.innerHTML = '<span>Email</span>';
  wrap.appendChild(label);

  // Move email elements into wrapper
  const lastLine = email.split('\n').filter(l => l.trim()).pop() || '';
  let el = wrap.nextSibling;
  while (el && el.parentNode === wrap.parentNode) {
    const next = el.nextSibling;
    const isTarget = el.nodeType === 1 || (el.nodeType === 3 && el.textContent.trim());
    if (isTarget) wrap.appendChild(el);
    else { wrap.appendChild(el); }
    if (el.textContent && el.textContent.includes(lastLine.trim())) break;
    el = next;
  }

  // Add copy button (absolutely positioned in top-right of wrapper)
  const btn = _makeCopyBtn(email, 'Copy email');
  btn.style.top = '6px';
  btn.style.right = '6px';
  wrap.appendChild(btn);
}

function _extractEmail(rawText) {
  const lines = rawText.split('\n');

  // Find greeting line
  let startIdx = -1;
  for (let i = 0; i < lines.length; i++) {
    if (_EMAIL_GREETINGS.test(lines[i].trim())) {
      startIdx = i;
      break;
    }
  }
  if (startIdx < 0) return null;

  // Find the end of the email. Strategy:
  // 1. Look for a formal sign-off (Best, Regards, etc.)
  // 2. If found, include up to 3 lines after it (for the name)
  // 3. If no formal sign-off, scan backwards from the end for Claude's
  //    outro lines and stop just before them. The last non-empty line
  //    before the outro (often just a name) is the email end.

  let endIdx = -1;

  // Try formal sign-off first (search from end)
  for (let i = lines.length - 1; i > startIdx; i--) {
    if (_EMAIL_SIGNOFFS.test(lines[i].trim())) {
      // Include sign-off + scan forward for the name (1-2 non-blank lines)
      endIdx = i;
      for (let j = i + 1; j <= Math.min(i + 3, lines.length - 1); j++) {
        const t = lines[j].trim();
        if (!t) continue; // skip blanks between sign-off and name
        if (_CLAUDE_OUTRO.test(t)) break; // Claude is talking again
        endIdx = j; // include this line (the name)
      }
      break;
    }
  }

  // No formal sign-off? Look for where Claude's commentary starts
  if (endIdx < 0) {
    // Find where Claude's outro begins
    let outroStart = lines.length;
    for (let i = startIdx + 1; i < lines.length; i++) {
      const trimmed = lines[i].trim();
      if (trimmed && _CLAUDE_OUTRO.test(trimmed)) {
        outroStart = i;
        break;
      }
    }
    // Email ends just before the outro (trim trailing blanks)
    endIdx = outroStart - 1;
    while (endIdx > startIdx && !lines[endIdx].trim()) endIdx--;
  }

  if (endIdx <= startIdx) return null;

  const emailLines = lines.slice(startIdx, endIdx + 1);
  // Must be at least 2 lines (greeting + something)
  if (emailLines.length < 2) return null;

  return emailLines.join('\n').trim();
}

function _findNodeContaining(container, text) {
  const searchText = text.slice(0, 30);
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    if (node.textContent.includes(searchText)) {
      let el = node.parentElement;
      while (el && el !== container && !['P', 'DIV', 'BLOCKQUOTE', 'UL', 'OL'].includes(el.tagName)) {
        el = el.parentElement;
      }
      return el !== container ? el : null;
    }
  }
  return null;
}

// -----------------------------------------------------------------------
// Tables
// -----------------------------------------------------------------------
function _addTableCopyButtons(container) {
  container.querySelectorAll('table').forEach(table => {
    if (table.parentElement && table.parentElement.querySelector('.smart-copy-btn')) return;
    const rows = [];
    table.querySelectorAll('tr').forEach(tr => {
      const cells = [];
      tr.querySelectorAll('th, td').forEach(td => cells.push(td.textContent.trim()));
      rows.push(cells.join('\t'));
    });
    const tsv = rows.join('\n');
    if (!tsv.trim()) return;

    const wrap = document.createElement('div');
    wrap.className = 'smart-copy-table-wrap';
    table.parentNode.insertBefore(wrap, table);
    wrap.appendChild(table);

    const btn = _makeCopyBtn(tsv, 'Copy table');
    btn.style.top = '4px';
    btn.style.right = '4px';
    wrap.appendChild(btn);
  });
}

// -----------------------------------------------------------------------
// Shared: create a copy button
// -----------------------------------------------------------------------
function _makeCopyBtn(textToCopy, tooltip) {
  const btn = document.createElement('button');
  btn.className = 'smart-copy-btn';
  btn.title = tooltip || 'Copy';
  btn.innerHTML = _COPY_ICON;
  btn.onclick = (e) => {
    e.stopPropagation();
    e.preventDefault();
    navigator.clipboard.writeText(textToCopy).then(() => {
      btn.innerHTML = _CHECK_ICON;
      btn.classList.add('copied');
      btn.title = 'Copied!';
      setTimeout(() => {
        btn.innerHTML = _COPY_ICON;
        btn.classList.remove('copied');
        btn.title = tooltip || 'Copy';
      }, 1500);
    });
  };
  return btn;
}
