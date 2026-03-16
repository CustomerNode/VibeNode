/* markdown.js — lightweight markdown renderer (no CDN dependency) */

function mdParse(md) {
    if (!md) return '';
    let html = md;
    // Escape HTML in non-code regions (we'll handle code blocks first)
    const codeBlocks = [];
    // Fenced code blocks
    html = html.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
      const idx = codeBlocks.length;
      codeBlocks.push('<pre><code>' + code.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') + '</code></pre>');
      return '\x00CODE' + idx + '\x00';
    });
    // Inline code
    html = html.replace(/`([^`]+)`/g, (_, code) => {
      const idx = codeBlocks.length;
      codeBlocks.push('<code>' + code.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') + '</code>');
      return '\x00CODE' + idx + '\x00';
    });
    // Escape remaining HTML
    html = html.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    // Headers
    html = html.replace(/^######\s+(.+)$/gm, '<h6>$1</h6>');
    html = html.replace(/^#####\s+(.+)$/gm, '<h5>$1</h5>');
    html = html.replace(/^####\s+(.+)$/gm, '<h4>$1</h4>');
    html = html.replace(/^###\s+(.+)$/gm, '<h3>$1</h3>');
    html = html.replace(/^##\s+(.+)$/gm, '<h2>$1</h2>');
    html = html.replace(/^#\s+(.+)$/gm, '<h1>$1</h1>');
    // Bold / italic
    html = html.replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>');
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
    html = html.replace(/___(.+?)___/g, '<strong><em>$1</em></strong>');
    html = html.replace(/__(.+?)__/g, '<strong>$1</strong>');
    html = html.replace(/_(.+?)_/g, '<em>$1</em>');
    // Blockquotes
    html = html.replace(/^&gt;\s?(.+)$/gm, '<blockquote>$1</blockquote>');
    // Horizontal rule
    html = html.replace(/^[-*_]{3,}$/gm, '<hr>');
    // Lists — collect consecutive lines
    html = html.replace(/((?:^[-*+]\s+.+\n?)+)/gm, (block) => {
      const items = block.trim().split('\n').map(l => '<li>' + l.replace(/^[-*+]\s+/, '') + '</li>').join('');
      return '<ul>' + items + '</ul>\n';
    });
    html = html.replace(/((?:^\d+\.\s+.+\n?)+)/gm, (block) => {
      const items = block.trim().split('\n').map(l => '<li>' + l.replace(/^\d+\.\s+/, '') + '</li>').join('');
      return '<ol>' + items + '</ol>\n';
    });
    // Tables — | col | col | rows with a separator row of |---|---|
    html = html.replace(/((?:^\|.+\|\n?)+)/gm, (block) => {
      const rows = block.trim().split('\n').filter(r => r.trim());
      if (rows.length < 2) return block;
      const sepIdx = rows.findIndex(r => /^\|[\s\-|:]+\|$/.test(r.trim()));
      if (sepIdx < 0) return block;
      const headerRows = rows.slice(0, sepIdx);
      const bodyRows = rows.slice(sepIdx + 1);
      const parseRow = (r, tag) => '<tr>' + r.replace(/^\||\|$/g,'').split('|').map(c => `<${tag}>${c.trim()}</${tag}>`).join('') + '</tr>';
      const thead = '<thead>' + headerRows.map(r => parseRow(r,'th')).join('') + '</thead>';
      const tbody = bodyRows.length ? '<tbody>' + bodyRows.map(r => parseRow(r,'td')).join('') + '</tbody>' : '';
      return '<table>' + thead + tbody + '</table>\n';
    });
    // Links
    html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
    // Paragraphs — wrap double-newline separated blocks
    html = html.split(/\n{2,}/).map(para => {
      para = para.trim();
      if (!para) return '';
      if (/^<(h[1-6]|ul|ol|li|blockquote|hr|pre|table)/.test(para)) return para;
      if (para.includes('\x00CODE')) return para;
      return '<p>' + para.replace(/\n/g, '<br>') + '</p>';
    }).join('\n');
    // Restore code blocks
    codeBlocks.forEach((block, idx) => {
      html = html.replace('\x00CODE' + idx + '\x00', block);
    });
    return html;
}
