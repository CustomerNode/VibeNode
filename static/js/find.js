/* find.js — find-in-session search with highlighting */

let findMatches = [];
let findCurrent = -1;

function openFind() {
  document.getElementById('find-bar').classList.add('open');
  document.getElementById('find-input').focus();
}

function closeFind() {
  clearFindHighlights();
  document.getElementById('find-bar').classList.remove('open');
  document.getElementById('find-input').value = '';
  document.getElementById('find-count').textContent = '';
  findMatches = []; findCurrent = -1;
}

function runFind() {
  clearFindHighlights();
  findMatches = []; findCurrent = -1;
  const q = document.getElementById('find-input').value;
  if (!q || q.length < 2) { document.getElementById('find-count').textContent = ''; return; }

  const msgEls = document.querySelectorAll('.msg-content');

  // Highlight all matches using a regex replacement
  const re = new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
  msgEls.forEach(el => {
    if (el.textContent.toLowerCase().includes(q.toLowerCase())) {
      el.innerHTML = el.innerHTML.replace(re, m => `<mark class="find-match">${escHtml(m)}</mark>`);
    }
  });

  const allMarks = document.querySelectorAll('mark.find-match');
  findMatches = Array.from(allMarks);
  document.getElementById('find-count').textContent = findMatches.length ? `1 / ${findMatches.length}` : 'No matches';
  if (findMatches.length) { findCurrent = 0; highlightCurrent(); }
}

function clearFindHighlights() {
  document.querySelectorAll('mark.find-match').forEach(m => {
    m.outerHTML = m.textContent;
  });
}

function highlightCurrent() {
  document.querySelectorAll('mark.find-match').forEach((m, i) => {
    m.classList.toggle('current', i === findCurrent);
  });
  if (findMatches[findCurrent]) findMatches[findCurrent].scrollIntoView({block:'center', behavior:'smooth'});
  document.getElementById('find-count').textContent = `${findCurrent+1} / ${findMatches.length}`;
}

function findNav(dir) {
  if (!findMatches.length) return;
  findCurrent = (findCurrent + dir + findMatches.length) % findMatches.length;
  highlightCurrent();
}

function findKeyNav(e) {
  if (e.key === 'Enter') findNav(e.shiftKey ? -1 : 1);
  if (e.key === 'Escape') closeFind();
}

document.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'f') {
    e.preventDefault();
    openFind();
  }
});
