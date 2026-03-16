/* theme.js — dark/light/auto theme switching */

const _themeSvg = {
  dark: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>',
  light: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>',
  auto: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 3v18" /><path d="M12 3a9 9 0 0 1 0 18" fill="currentColor"/></svg>'
};
const _themeOrder = ['dark', 'light', 'auto'];

function getEffectiveTheme(pref) {
  if (pref !== 'auto') return pref;
  var h = new Date().getHours();
  return (h >= 7 && h < 19) ? 'light' : 'dark';
}

function applyTheme(pref) {
  document.documentElement.setAttribute('data-theme', getEffectiveTheme(pref));
  document.getElementById('btn-theme').innerHTML = _themeSvg[pref];
  document.getElementById('btn-theme').title = pref.charAt(0).toUpperCase() + pref.slice(1) + ' theme';
}

function cycleTheme() {
  var cur = localStorage.getItem('theme') || 'dark';
  var next = _themeOrder[(_themeOrder.indexOf(cur) + 1) % _themeOrder.length];
  localStorage.setItem('theme', next);
  applyTheme(next);
  var labels = {dark: 'Dark theme', light: 'Light theme', auto: 'Auto theme (adapts to time of day)'};
  showToast(labels[next]);
}

applyTheme(localStorage.getItem('theme') || 'dark');

// If auto theme, check every minute for sunrise/sunset change
setInterval(function() {
  var pref = localStorage.getItem('theme');
  if (pref === 'auto') applyTheme('auto');
}, 60000);
