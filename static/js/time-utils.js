/**
 * time-utils.js — shared smart date formatting.
 * Extracted from sessions.js per plan Section 14 line 2894.
 * Used by sessions.js and kanban.js.
 *
 * Today      → "2:34 PM"
 * Yesterday  → "Yesterday"
 * 2-6 days   → "Tuesday"
 * This year  → "Mar 18"
 * Older      → "Dec 5 '25"
 *
 * Plan Section 2 lines 753-779: exact implementation.
 */
function _shortDate(dateStr) {
  const d = new Date(dateStr);
  if (isNaN(d)) return dateStr;
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const target = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const diffDays = Math.round((today - target) / 86400000);

  let h = d.getHours();
  const ampm = h >= 12 ? 'PM' : 'AM';
  h = h % 12 || 12;
  const min = String(d.getMinutes()).padStart(2, '0');
  const time = h + ':' + min + ' ' + ampm;

  if (diffDays === 0) return time;                // "2:34 PM"
  if (diffDays === 1) return 'Yesterday';          // "Yesterday"

  const dayNames = ['Sunday','Monday','Tuesday','Wednesday',
                    'Thursday','Friday','Saturday'];
  if (diffDays >= 2 && diffDays <= 6) return dayNames[d.getDay()];

  const months = ['Jan','Feb','Mar','Apr','May','Jun',
                  'Jul','Aug','Sep','Oct','Nov','Dec'];
  if (d.getFullYear() !== now.getFullYear())
      return months[d.getMonth()] + ' ' + d.getDate() + " '" + String(d.getFullYear()).slice(-2);
  return months[d.getMonth()] + ' ' + d.getDate(); // "Mar 18"
}

// Periodically re-format every element tagged with data-short-date so labels
// like "Yesterday" or "2:34 PM" don't go stale when the day rolls over without
// a fresh render. Elements opt in by setting data-short-date="<raw timestamp>".
function _refreshShortDates() {
  const elements = document.querySelectorAll('[data-short-date]');
  for (const el of elements) {
    const raw = el.getAttribute('data-short-date');
    if (raw) el.textContent = _shortDate(raw);
  }
}

setInterval(_refreshShortDates, 60000);
