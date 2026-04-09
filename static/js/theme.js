/* theme.js — dark/light/auto theme switching
   Auto mode uses real sunrise/sunset times via timezone geolocation. */

const _themeSvg = {
  dark: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>',
  light: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>',
  auto: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 3v18" /><path d="M12 3a9 9 0 0 1 0 18" fill="currentColor"/></svg>'
};
const _themeOrder = ['dark', 'light', 'auto'];

/* ── Timezone → coordinates for sunrise/sunset API ── */
const _tzCoords = {
  // North America
  'America/New_York': [40.71,-74.01],
  'America/Chicago': [41.88,-87.63],
  'America/Denver': [39.74,-104.99],
  'America/Los_Angeles': [34.05,-118.24],
  'America/Phoenix': [33.45,-112.07],
  'America/Anchorage': [61.22,-149.90],
  'Pacific/Honolulu': [21.31,-157.86],
  'America/Toronto': [43.65,-79.38],
  'America/Vancouver': [49.28,-123.12],
  'America/Edmonton': [53.55,-113.49],
  'America/Winnipeg': [49.90,-97.14],
  'America/Halifax': [44.65,-63.57],
  'America/St_Johns': [47.56,-52.71],
  'America/Regina': [50.45,-104.62],
  'America/Mexico_City': [19.43,-99.13],
  'America/Tijuana': [32.51,-117.02],
  'America/Monterrey': [25.67,-100.31],
  // Central America & Caribbean
  'America/Guatemala': [14.63,-90.51],
  'America/Costa_Rica': [9.93,-84.08],
  'America/Panama': [8.98,-79.52],
  'America/Havana': [23.11,-82.37],
  'America/Jamaica': [18.11,-77.30],
  'America/Puerto_Rico': [18.47,-66.11],
  // South America
  'America/Sao_Paulo': [-23.55,-46.63],
  'America/Argentina/Buenos_Aires': [-34.60,-58.38],
  'America/Bogota': [4.71,-74.07],
  'America/Lima': [-12.05,-77.04],
  'America/Santiago': [-33.45,-70.67],
  'America/Caracas': [10.48,-66.90],
  // Europe
  'Europe/London': [51.51,-0.13],
  'Europe/Paris': [48.86,2.35],
  'Europe/Berlin': [52.52,13.41],
  'Europe/Madrid': [40.42,-3.70],
  'Europe/Rome': [41.90,12.50],
  'Europe/Amsterdam': [52.37,4.90],
  'Europe/Brussels': [50.85,4.35],
  'Europe/Zurich': [47.38,8.54],
  'Europe/Vienna': [48.21,16.37],
  'Europe/Stockholm': [59.33,18.07],
  'Europe/Oslo': [59.91,10.75],
  'Europe/Copenhagen': [55.68,12.57],
  'Europe/Helsinki': [60.17,24.94],
  'Europe/Warsaw': [52.23,21.01],
  'Europe/Prague': [50.08,14.44],
  'Europe/Budapest': [47.50,19.04],
  'Europe/Bucharest': [44.43,26.10],
  'Europe/Athens': [37.98,23.73],
  'Europe/Istanbul': [41.01,28.98],
  'Europe/Moscow': [55.76,37.62],
  'Europe/Dublin': [53.35,-6.26],
  'Europe/Lisbon': [38.72,-9.14],
  'Europe/Kiev': [50.45,30.52],
  // Middle East
  'Asia/Dubai': [25.20,55.27],
  'Asia/Riyadh': [24.71,46.67],
  'Asia/Tehran': [35.69,51.39],
  'Asia/Jerusalem': [31.77,35.23],
  'Asia/Baghdad': [33.31,44.37],
  'Asia/Qatar': [25.29,51.53],
  'Asia/Kuwait': [29.38,47.99],
  // Asia
  'Asia/Kolkata': [28.61,77.21],
  'Asia/Colombo': [6.93,79.85],
  'Asia/Dhaka': [23.81,90.41],
  'Asia/Karachi': [24.86,67.01],
  'Asia/Kathmandu': [27.72,85.32],
  'Asia/Almaty': [43.24,76.95],
  'Asia/Tashkent': [41.30,69.28],
  'Asia/Bangkok': [13.76,100.50],
  'Asia/Ho_Chi_Minh': [10.82,106.63],
  'Asia/Jakarta': [-6.21,106.85],
  'Asia/Singapore': [1.35,103.82],
  'Asia/Kuala_Lumpur': [3.14,101.69],
  'Asia/Manila': [14.60,120.98],
  'Asia/Shanghai': [31.23,121.47],
  'Asia/Hong_Kong': [22.32,114.17],
  'Asia/Taipei': [25.03,121.57],
  'Asia/Tokyo': [35.68,139.69],
  'Asia/Seoul': [37.57,126.98],
  // Oceania
  'Australia/Sydney': [-33.87,151.21],
  'Australia/Melbourne': [-37.81,144.96],
  'Australia/Brisbane': [-27.47,153.03],
  'Australia/Perth': [-31.95,115.86],
  'Australia/Adelaide': [-34.93,138.60],
  'Australia/Darwin': [-12.46,130.84],
  'Australia/Hobart': [-42.88,147.33],
  'Pacific/Auckland': [-36.85,174.76],
  'Pacific/Fiji': [-18.14,178.44],
  'Pacific/Guam': [13.44,144.79],
  // Africa
  'Africa/Cairo': [30.04,31.24],
  'Africa/Lagos': [6.52,3.38],
  'Africa/Johannesburg': [-26.20,28.05],
  'Africa/Nairobi': [-1.29,36.82],
  'Africa/Casablanca': [33.57,-7.59],
  'Africa/Accra': [5.56,-0.19],
  'Africa/Addis_Ababa': [9.02,38.75],
  'Africa/Dar_es_Salaam': [-6.79,39.28],
  'Africa/Algiers': [36.75,3.04],
  'Africa/Tunis': [36.81,10.17]
};

/* ── Sunrise/sunset state ── */
let _sunriseMin = null;   // minutes since midnight
let _sunsetMin = null;
let _sunDataDate = null;  // date string (YYYY-MM-DD) of cached data
let _sunFetchInFlight = false;

function _parseSunTime(s) {
  var m = s.match(/(\d+):(\d+):(\d+)\s*(AM|PM)/i);
  if (!m) return null;
  var h = parseInt(m[1]), mn = parseInt(m[2]);
  if (m[4].toUpperCase() === 'PM' && h !== 12) h += 12;
  if (m[4].toUpperCase() === 'AM' && h === 12) h = 0;
  return h * 60 + mn;
}

function _todayStr() {
  var d = new Date();
  return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0');
}

function _fetchSunTimes() {
  if (_sunFetchInFlight) return;
  var today = _todayStr();
  if (_sunDataDate === today && _sunriseMin !== null) return; // already have today's data

  var tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  var coords = _tzCoords[tz] || [40.71, -74.01]; // fallback: New York
  var url = 'https://api.sunrisesunset.io/json?lat=' + coords[0] + '&lng=' + coords[1] + '&timezone=' + encodeURIComponent(tz);

  _sunFetchInFlight = true;
  fetch(url)
    .then(function(r) { return r.json(); })
    .then(function(d) {
      _sunFetchInFlight = false;
      if (d.status !== 'OK') return;
      _sunriseMin = _parseSunTime(d.results.sunrise);
      _sunsetMin = _parseSunTime(d.results.sunset);
      _sunDataDate = today;
      // Cache for the inline flash-prevention script in index.html
      localStorage.setItem('_sunriseMin', _sunriseMin);
      localStorage.setItem('_sunsetMin', _sunsetMin);
      // Re-apply in case we were waiting on this data
      var pref = localStorage.getItem('theme');
      if (pref === 'auto') applyTheme('auto');
    })
    .catch(function() {
      _sunFetchInFlight = false;
    });
}

function _isDark() {
  var now = new Date();
  var mins = now.getHours() * 60 + now.getMinutes();
  if (_sunriseMin !== null && _sunsetMin !== null) {
    return mins < _sunriseMin || mins >= _sunsetMin;
  }
  // Fallback while API hasn't responded yet: 7am-7pm
  return !(now.getHours() >= 7 && now.getHours() < 19);
}

function getEffectiveTheme(pref) {
  if (pref !== 'auto') return pref;
  return _isDark() ? 'dark' : 'light';
}

function applyTheme(pref) {
  if (pref === 'auto') _fetchSunTimes();
  const effective = getEffectiveTheme(pref);
  document.documentElement.setAttribute('data-theme', effective);
  document.getElementById('btn-theme').innerHTML = _themeSvg[pref];
  document.getElementById('btn-theme').title = pref.charAt(0).toUpperCase() + pref.slice(1) + ' theme';
  // Swap logo for dark/light
  const logo = document.querySelector('.app-logo');
  if (logo) logo.src = effective === 'dark' ? '/static/images/logo-dark.png' : '/static/images/logo.png';
}

function cycleTheme() {
  var cur = localStorage.getItem('theme') || 'dark';
  var next = _themeOrder[(_themeOrder.indexOf(cur) + 1) % _themeOrder.length];
  localStorage.setItem('theme', next);
  applyTheme(next);
  var labels = {dark: 'Dark theme', light: 'Light theme', auto: 'Auto theme (sunrise/sunset)'};
  showToast(labels[next]);
}

applyTheme(localStorage.getItem('theme') || 'dark');

// If auto theme, re-check every minute and refetch sun times at midnight
setInterval(function() {
  var pref = localStorage.getItem('theme');
  if (pref === 'auto') applyTheme('auto');
}, 60000);
