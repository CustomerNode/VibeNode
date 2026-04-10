"""
VibeNode Photo Shoot -- Safe Mode
==================================
Marketing screenshots via headless Chrome + pure DOM injection.
Zero API writes. Zero database changes. Won't touch 5050/5051.
Usage:  pip install selenium requests && python docs/photoshoot.py
"""
import os, sys, time
import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

BASE = "http://localhost:5050"
SCREENSHOT_DIR = "docs/screenshots"
PROJECT = str(os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))).replace("\\", "-").replace("/", "-").replace(":", "-")

# ---------------------------------------------------------------------------
# Reusable SVG icons (match KI in kanban.js)
# ---------------------------------------------------------------------------
_ICON_MENU = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>'
_ICON_CHEVR = '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polyline points="9 18 15 12 9 6"/></svg>'
_ICON_PLAN = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>'

# ---------------------------------------------------------------------------
# DATA
# ---------------------------------------------------------------------------

BOARD = [
    {"status": "not_started", "label": "Not Started", "color": "#6b7280", "tasks": [
        {"title": "Mobile responsive layouts", "desc": "Audit and fix all views for mobile breakpoints.", "tags": [["frontend", "#8b5cf6"], ["design", "#ec4899"]], "owner": "M", "date": "Mar 30"},
        {"title": "Database migration tooling", "desc": "Migration runner with up/down, dry-run, auto backup.", "tags": [["backend", "#3b82f6"], ["devops", "#f97316"]], "owner": "D", "date": "Mar 28"},
        {"title": "User role permissions", "desc": "RBAC: admin > editor > viewer.", "tags": [["backend", "#3b82f6"]], "owner": "A", "date": "Mar 29"},
    ]},
    {"status": "working", "label": "Working", "color": "#3b82f6", "tasks": [
        {"title": "Auth System Overhaul", "desc": "OAuth2+PKCE, encrypted tokens, rate limiting, RBAC.", "tags": [["epic", "#a855f7"], ["security", "#ef4444"]], "owner": "A", "date": "Today", "subtasks": "2/4 subtasks", "sessions": "1 session"},
        {"title": "Performance Sprint", "desc": "Sub-200ms, 90+ Lighthouse.", "tags": [["epic", "#a855f7"], ["perf", "#f59e0b"]], "owner": "J", "date": "Today", "subtasks": "1/3 subtasks", "sessions": "1 session"},
        {"title": "OAuth2 provider integration", "desc": "PKCE for Google, GitHub, Microsoft.", "tags": [["backend", "#3b82f6"]], "owner": "K", "date": "Yesterday", "sessions": "1 session"},
        {"title": "DB query caching layer", "desc": "Redis caching, invalidation on writes.", "tags": [["backend", "#3b82f6"], ["perf", "#f59e0b"]], "owner": "S", "date": "Today", "sessions": "1 session"},
        {"title": "API response compression", "desc": "Brotli/gzip + ETags.", "tags": [["backend", "#3b82f6"]], "owner": "R", "date": "Mar 31"},
    ]},
    {"status": "validating", "label": "Validating", "color": "#f59e0b", "tasks": [
        {"title": "Rate limiting middleware", "desc": "Sliding window + Redis.", "tags": [["backend", "#3b82f6"]], "owner": "K", "date": "Today"},
        {"title": "API Documentation v2", "desc": "OpenAPI 3.1 from decorators.", "tags": [["docs", "#06b6d4"]], "owner": "L", "date": "Mar 31"},
    ]},
    {"status": "remediating", "label": "Remediating", "color": "#ef4444", "tasks": [
        {"title": "WebSocket reconnection", "desc": "Backoff + jitter. Quality indicator.", "tags": [["frontend", "#8b5cf6"], ["reliability", "#14b8a6"]], "owner": "J", "date": "Today"},
    ]},
    {"status": "complete", "label": "Complete", "color": "#22c55e", "tasks": [
        {"title": "Session token encryption", "desc": "AES-256-GCM with key rotation.", "tags": [["backend", "#3b82f6"], ["security", "#ef4444"]], "owner": "A", "date": "Mar 29"},
        {"title": "Frontend bundle splitting", "desc": "Route-based dynamic imports.", "tags": [["frontend", "#8b5cf6"]], "owner": "M", "date": "Mar 28"},
        {"title": "CI/CD pipeline hardening", "desc": "Parallel tests, scanning, auto-rollback.", "tags": [["devops", "#f97316"]], "owner": "D", "date": "Mar 27"},
        {"title": "Telemetry and observability", "desc": "Logging, OpenTelemetry, Prometheus.", "tags": [["devops", "#f97316"], ["observability", "#06b6d4"]], "owner": "R", "date": "Mar 28"},
    ]},
]

EPIC_SUBTASKS = {
    "title": "Authentication System Overhaul",
    "status": "WORKING", "color": "#3b82f6",
    "desc": "Modernize auth: OAuth2+PKCE, encrypted tokens, rate limiting, RBAC.",
    "tags": [["epic", "#a855f7"], ["security", "#ef4444"]],
    "created": "2:25 PM", "updated": "2:26 PM", "pct": 25,
    "subtasks": [
        {"status": "Not Started", "color": "#6b7280", "title": "User role permissions", "meta": ""},
        {"status": "Working", "color": "#3b82f6", "title": "OAuth2 provider integration", "meta": "1 session"},
        {"status": "Validating", "color": "#f59e0b", "title": "Rate limiting middleware", "meta": ""},
        {"status": "Complete", "color": "#22c55e", "title": "Session token encryption", "meta": ""},
    ],
}

EPIC_SESSIONS = {
    "title": "Performance Sprint",
    "status": "WORKING", "color": "#3b82f6",
    "desc": "Achieve sub-200ms page loads and 90+ Lighthouse scores across all routes.",
    "tags": [["epic", "#a855f7"], ["perf", "#f59e0b"]],
    "created": "1:10 PM", "updated": "2:05 PM",
    "sessions": [
        {"status": "Working", "color": "var(--status-working)", "name": "DB query caching layer"},
        {"status": "Idle", "color": "var(--status-complete)", "name": "Frontend bundle analysis"},
        {"status": "Sleeping", "color": "var(--text-dim)", "name": "Lighthouse perf baseline"},
    ],
}

EPIC_CHOOSER = {
    "title": "API Gateway Refactor",
    "status": "NOT STARTED", "color": "#6b7280",
    "desc": "Consolidate all API routes behind a unified gateway with centralized auth, rate limiting, and request validation.",
    "tags": [["backend", "#3b82f6"], ["architecture", "#14b8a6"]],
    "created": "2:40 PM", "updated": "2:40 PM",
}

SESSIONS = [
    {"name": "OAuth2 provider integration", "status": "working", "date": "2:31 PM", "size": "148 KB"},
    {"name": "Rate limiting middleware", "status": "working", "date": "2:28 PM", "size": "92 KB"},
    {"name": "DB query caching layer", "status": "working", "date": "2:15 PM", "size": "203 KB"},
    {"name": "Fix WebSocket reconnect bug", "status": "question", "date": "2:10 PM", "size": "67 KB"},
    {"name": "Frontend bundle analysis", "status": "idle", "date": "1:45 PM", "size": "312 KB"},
    {"name": "API response compression", "status": "idle", "date": "1:30 PM", "size": "85 KB"},
    {"name": "Session token encryption", "status": "idle", "date": "12:50 PM", "size": "176 KB"},
    {"name": "CI/CD pipeline hardening", "status": "idle", "date": "12:15 PM", "size": "245 KB"},
    {"name": "OpenAPI spec generation", "status": "sleeping", "date": "11:30 AM", "size": "134 KB"},
    {"name": "Database migration runner", "status": "sleeping", "date": "11:00 AM", "size": "98 KB"},
    {"name": "Telemetry dashboard setup", "status": "sleeping", "date": "10:20 AM", "size": "167 KB"},
    {"name": "RBAC permission matrix", "status": "sleeping", "date": "9:45 AM", "size": "54 KB"},
    {"name": "Mobile breakpoint audit", "status": "sleeping", "date": "Yesterday", "size": "189 KB"},
    {"name": "Redis cache invalidation", "status": "sleeping", "date": "Yesterday", "size": "112 KB"},
    {"name": "Lighthouse perf baseline", "status": "sleeping", "date": "Mar 30", "size": "276 KB"},
    {"name": "Route-based code splitting", "status": "sleeping", "date": "Mar 30", "size": "143 KB"},
]

CONVERSATION = [
    {"role": "user", "text": "Implement rate limiting middleware using a sliding window algorithm with Redis as the backing store. It should support per-route and per-user limits."},
    {"role": "tool", "name": "Read", "desc": "app/middleware/__init__.py", "ok": True},
    {"role": "tool", "name": "Read", "desc": "app/config.py (lines 45-80)", "ok": True},
    {"role": "tool", "name": "Write", "desc": "app/middleware/rate_limit.py", "ok": True},
    {"role": "assistant", "text": "I've created the rate limiting middleware with the following design:\n\n**Sliding Window Algorithm** \u2014 Uses Redis sorted sets to track request timestamps per key. Each request adds the current timestamp and removes entries outside the window.\n\n**Key features:**\n- **Per-route limits** \u2014 Configurable via `@rate_limit(requests=100, window=60)` decorator\n- **Per-user limits** \u2014 Automatically scoped by authenticated user ID, falls back to IP\n- **Response headers** \u2014 `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`\n- **Graceful degradation** \u2014 If Redis is unreachable, requests pass through with a warning log\n\nThe middleware is registered in `app/middleware/__init__.py`. Let me add the tests next."},
    {"role": "tool", "name": "Write", "desc": "tests/test_rate_limit.py", "ok": True},
    {"role": "tool", "name": "Bash", "desc": "pytest tests/test_rate_limit.py -v", "ok": True},
    {"role": "assistant", "text": "All 8 tests pass:\n\n```\ntests/test_rate_limit.py::test_allows_under_limit PASSED\ntests/test_rate_limit.py::test_blocks_over_limit PASSED\ntests/test_rate_limit.py::test_sliding_window_reset PASSED\ntests/test_rate_limit.py::test_per_user_isolation PASSED\ntests/test_rate_limit.py::test_per_route_config PASSED\ntests/test_rate_limit.py::test_response_headers PASSED\ntests/test_rate_limit.py::test_redis_fallback PASSED\ntests/test_rate_limit.py::test_concurrent_requests PASSED\n```\n\nThe rate limiter is ready. Want me to wire it into the existing routes?"},
]

# ---------------------------------------------------------------------------
# JS TEMPLATES
# ---------------------------------------------------------------------------

_JS_DISMISS = r"(function(){var s=document.getElementById('ps-hide');if(!s){s=document.createElement('style');s.id='ps-hide';s.textContent='#project-overlay,#health-blocker,#pm-overlay,#compare-overlay,.modal-overlay,#extract-drawer,.boot-splash,#project-card,.dashboard,#btn-git-publish,#git-sync-overlay{display:none!important;visibility:hidden!important;opacity:0!important}';document.head.appendChild(s);}['project-overlay','health-blocker','pm-overlay','compare-overlay','git-sync-overlay'].forEach(function(id){var e=document.getElementById(id);if(e){e.classList.remove('show');e.style.display='none';}});var gb=document.getElementById('btn-git-publish');if(gb)gb.style.display='none';var d=document.querySelector('.dashboard');if(d)d.style.display='none';document.querySelectorAll('.show').forEach(function(e){var p=getComputedStyle(e).position;if(p==='fixed'||p==='absolute'){e.classList.remove('show');e.style.display='none';}});window.openProjectOverlay=function(){};window.openHealthBlocker=function(){};window.openGitPublish=function(){};})();"

_JS_FREEZE = r"(function(){window.renderWorkforce=function(){};window.loadSessions=function(){};window.renderList=function(){};window.initKanban=function(){};window._pollSessions=function(){};if(typeof pollTimer!=='undefined'&&pollTimer){clearInterval(pollTimer);pollTimer=null;}})();"

_JS_BOARD = r"""(function(cols){var board=document.getElementById('kanban-board');if(!board)return;board.innerHTML='';var w=document.createElement('div');w.className='kanban-columns-wrapper';board.appendChild(w);cols.forEach(function(col){var c=document.createElement('div');c.className='kanban-column';c.setAttribute('data-status',col.status);var h=document.createElement('div');h.className='kanban-column-header';h.innerHTML='<div class="kanban-column-color-bar" style="background:'+col.color+'"></div><span class="kanban-column-name">'+col.label+'</span><span class="kanban-column-count">'+col.tasks.length+'</span>';c.appendChild(h);var b=document.createElement('div');b.className='kanban-column-body';b.setAttribute('data-status',col.status);c.appendChild(b);col.tasks.forEach(function(t){var card=document.createElement('div');card.className='kanban-card';card.setAttribute('data-status',col.status);var tags='';(t.tags||[]).forEach(function(g){tags+='<span class="kanban-tag-pill" style="background:'+g[1]+'22;color:'+g[1]+';border-color:'+g[1]+'44;">'+g[0]+'</span>';});var mp=[];if(t.subtasks)mp.push('<div class="kanban-card-subtask-text">'+t.subtasks+'</div>');if(t.sessions)mp.push('<div class="kanban-card-session-badge" style="background:#3b82f622;color:#60a5fa;">\u25cf '+t.sessions+'</div>');var mr=mp.length?'<div style="display:flex;gap:6px;align-items:center;margin-top:6px;flex-wrap:wrap;">'+mp.join('')+'</div>':'';card.innerHTML='<div class="kanban-card-header"><div class="kanban-card-title-row"><span class="kanban-card-title">'+t.title+'</span><span class="kanban-card-time">'+(t.date||'')+'</span></div><span class="kanban-card-owner" style="background:transparent;color:var(--text-faint);font-size:9px;opacity:0.5;">'+(t.owner||'')+'</span></div><div class="kanban-card-desc">'+(t.desc||'')+'</div>'+mr+'<div class="kanban-card-bottom"><div class="kanban-card-tags">'+tags+'</div></div>';b.appendChild(card);});w.appendChild(c);});
})(arguments[0]);"""

_JS_DRILL_BASE = r"""(function(d){
var board=document.getElementById('kanban-board');if(!board)return;board.innerHTML='';
var menuIcon=arguments[1],chevR=arguments[2],planIcon=arguments[3];
var tb=document.createElement('div');tb.className='kanban-drill-titlebar';
tb.innerHTML='<div class="kanban-drill-breadcrumb"><span class="kanban-drill-crumb">'+menuIcon+' Board</span><span class="kanban-drill-sep">'+chevR+'</span><span class="kanban-drill-crumb current">'+d.title+'</span></div>';
board.appendChild(tb);
var th='';(d.tags||[]).forEach(function(g){th+='<span class="kanban-tag-pill" style="background:'+g[1]+'22;color:'+g[1]+';border-color:'+g[1]+'44;">'+g[0]+'</span>';});
var left='<div class="kanban-drill-status kanban-status-clickable" style="background:'+d.color+'26;color:'+d.color+';display:inline-flex;align-items:center;gap:4px;padding:5px 12px;border-radius:6px;font-weight:700;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;">'+d.status+' '+chevR+'</div><div class="kanban-drill-title" style="font-size:22px;font-weight:700;margin:12px 0 4px;padding:2px 6px;margin-left:-6px;border-radius:6px;border:1px solid transparent;">'+d.title+'</div><div style="font-size:11px;color:var(--text-dim);margin:4px 0 16px;">Created '+d.created+' \u00b7 Updated '+d.updated+'</div><div class="kanban-drill-desc-wrap kanban-drill-desc-collapsed"><div class="kanban-drill-desc" style="font-size:14px;line-height:1.6;padding:4px 6px;">'+d.desc+'</div></div><div class="kanban-drill-tags-section" style="margin:8px 0 0;"><div class="kanban-drill-tags-list" style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:4px;">'+th+'</div><span class="kanban-tag-add-trigger" style="display:inline-flex;align-items:center;gap:2px;padding:0 8px;border-radius:4px;color:var(--text-dim);"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="opacity:0.5;"><path d="M12 5v14M5 12h14"/></svg><span style="font-size:11px;">Add tag</span></span></div>';
var bd=document.createElement('div');bd.className='kanban-drill-body';
bd.innerHTML='<div class="kanban-drill-split"><div class="kanban-drill-left">'+left+'</div><div class="kanban-drill-right">'+d.rightHTML+'</div></div>';
board.appendChild(bd);
})(arguments[0],arguments[1],arguments[2],arguments[3]);"""

_JS_GRID = r"""(function(sessions){
var grid=document.getElementById('workforce-grid');if(!grid)return;grid.innerHTML='';
var icons={
working:'<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#5555bb" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>',
idle:'<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#44aa66" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M9 12l2 2 4-4"/></svg>',
question:'<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#ff9500" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M9 9a3 3 0 115 2c0 1.5-2 2-2 3"/><circle cx="12" cy="17" r="0.5" fill="#ff9500"/></svg>',
sleeping:'<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#888" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M15 9l-6 6M9 9h6M9 15h6"/></svg>'
};
var labels={working:'Working',idle:'Idle',question:'Question',sleeping:'Sleeping'};
sessions.forEach(function(s,i){
var card=document.createElement('div');
card.className='wf-card wf-'+s.status+(i===0?' wf-selected':'');
card.innerHTML='<div class="wf-avatar">'+icons[s.status]+'</div><div class="wf-status-label">'+labels[s.status]+'</div><div class="wf-name">'+s.name+'</div><div class="wf-meta">'+s.date+'</div>';
grid.appendChild(card);
});
})(arguments[0]);"""

_JS_LIST = r"""(function(sessions){
var list=document.getElementById('session-list');if(!list)return;list.innerHTML='';
var hdr=document.createElement('div');hdr.className='col-header-row';
hdr.innerHTML='<div class="col-header sortable sort-active" id="col-h-name">Name</div><div class="col-header sortable" id="col-h-date">Date</div><div class="col-header sortable" id="col-h-size">Size</div>';
list.appendChild(hdr);
var icons={
working:'<svg class="state-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#5555bb" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg> ',
idle:'<svg class="state-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#44aa66" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><path d="M9 12l2 2 4-4"/></svg> ',
question:'<svg class="state-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#ff9500" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><path d="M9 9a3 3 0 115 2c0 1.5-2 2-2 3"/><circle cx="12" cy="17" r="0.5" fill="#ff9500"/></svg> ',
sleeping:''
};
sessions.forEach(function(s,i){
var row=document.createElement('div');
row.className='session-item'+(i===0?' active':'')+(s.status==='working'?' running':'')+(s.status==='question'?' waiting':'');
row.innerHTML='<div class="session-col-name">'+(icons[s.status]||'')+s.name+'</div><div class="session-col-date">'+s.date+'</div><div class="session-col-size">'+s.size+'</div>';
list.appendChild(row);
});
})(arguments[0]);"""

_JS_LIVE = r"""(function(msgs){
var log=document.getElementById('live-log');
if(!log){var mb=document.getElementById('main-body');if(mb){mb.innerHTML='<div class="live-panel" id="live-panel"><div class="conversation live-log" id="live-log"></div><div class="live-input-bar" id="live-input-bar"></div></div>';mb.style.display='';log=document.getElementById('live-log');var lp=document.getElementById('live-panel');if(lp)lp.style.display='';}}
if(!log)return;log.innerHTML='';
var sticky=document.querySelector('.sticky-user-bar');if(sticky){sticky.innerHTML='<span class="sticky-user-text">Rate limiting middleware</span>';sticky.classList.add('visible');}
var toolIcon='<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z"/></svg>';
var checkIcon='<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#44aa66" stroke-width="2.5"><path d="M20 6L9 17l-5-5"/></svg>';
msgs.forEach(function(m){
if(m.role==='user'){
var d=document.createElement('div');d.className='msg user';
d.innerHTML='<div class="msg-role">me</div><div class="msg-body msg-content"><pre style="white-space:pre-wrap;margin:0;">'+m.text+'</pre></div>';
log.appendChild(d);
}else if(m.role==='assistant'){
var d=document.createElement('div');d.className='msg assistant';
d.innerHTML='<div class="msg-role">claude</div><div class="msg-body msg-content">'+m.text.replace(/\n/g,'<br>')+'</div>';
log.appendChild(d);
}else if(m.role==='tool'){
var e=document.createElement('div');e.className='live-entry live-entry-tool';
e.innerHTML='<div class="live-tool-line"><span class="live-tool-icon">'+toolIcon+'</span><span class="live-tool-name">'+m.name+'</span><span class="live-tool-desc">'+m.desc+'</span></div>';
log.appendChild(e);
if(m.ok){
var r=document.createElement('div');r.className='live-entry live-entry-result';
r.innerHTML='<div class="live-result-line live-result-ok">'+checkIcon+' Success</div>';
log.appendChild(r);
}
}
});
var bar=document.getElementById('live-input-bar');
if(bar)bar.innerHTML='<textarea class="live-textarea" rows="3" placeholder="Describe what you want Claude to do\u2026" disabled></textarea><div class="live-bar-row"><span class="send-hint" style="font-size:10px;color:var(--text-faint);">Enter to send · Ctrl+Enter for new line<span class="send-hint-btn" title="Change send shortcut"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/></svg></span></span><button class="live-send-btn" disabled>Send</button></div>';
})(arguments[0]);"""

_JS_KANBAN_SESSION = r"""(function(d){
var menuIcon=arguments[1],chevR=arguments[2];
var kb=document.getElementById('kanban-board');if(kb)kb.style.display='none';
var mb=document.getElementById('main-body');if(mb)mb.style.display='';
var old=document.getElementById('kanban-session-bar');if(old)old.remove();
var html='<div class="kanban-drill-titlebar" id="kanban-session-bar"><div class="kanban-drill-breadcrumb">';
html+='<span class="kanban-drill-crumb">'+menuIcon+' Board</span>';
html+='<span class="kanban-drill-sep">'+chevR+'</span>';
html+='<span class="kanban-drill-crumb">'+d.taskTitle+'</span>';
html+='<span class="kanban-drill-sep">'+chevR+'</span>';
html+='<span class="kanban-drill-crumb current">'+d.sessionName+'</span>';
html+='</div><div class="kanban-drill-actions"><span class="btn-group-label">Actions</span><div class="btn-group-divider"></div><span class="btn-group-label">Analyze &#9662;</span></div></div>';
if(mb)mb.insertAdjacentHTML('beforebegin',html);
})(arguments[0],arguments[1],arguments[2]);"""

# ---------------------------------------------------------------------------
# DRIVER / HELPERS
# ---------------------------------------------------------------------------

def _driver():
    opts = Options()
    for a in ["--headless=new","--window-size=1920,1080","--force-device-scale-factor=1","--disable-gpu","--no-sandbox","--hide-scrollbars"]:
        opts.add_argument(a)
    return webdriver.Chrome(options=opts)

def _fresh_page(drv, mode="workforce"):
    drv.get(BASE); time.sleep(1)
    drv.execute_script(
        "localStorage.setItem('activeProject',arguments[0]);"
        "localStorage.setItem('theme','dark');"
        "localStorage.setItem('viewMode',arguments[1]);"
        "localStorage.setItem('sidebarCollapsed','false');",
        PROJECT, mode)
    drv.get(BASE); time.sleep(2)
    # Freeze render functions ASAP so they don't overwrite our injections
    drv.execute_script(_JS_FREEZE)
    time.sleep(3)
    drv.execute_script("document.documentElement.setAttribute('data-theme','dark');")
    drv.execute_script(_JS_DISMISS); time.sleep(1)
    drv.execute_script(_JS_DISMISS)
    drv.execute_script(_JS_FREEZE)

def _snap(drv, name, label):
    drv.execute_script(_JS_DISMISS); time.sleep(0.5)
    p = os.path.join(SCREENSHOT_DIR, f"{name}.png")
    drv.save_screenshot(p)
    print(f"    {name}.png ({os.path.getsize(p)//1024}KB) {label}")

def _hide_live(drv):
    drv.execute_script("var p=document.querySelector('.live-panel');if(p)p.style.display='none';var m=document.querySelector('.main');if(m)m.style.flex='1';var s=document.querySelector('.sticky-user-bar');if(s){s.classList.remove('visible');s.style.display='none';}")

def _drill(drv, data):
    drv.execute_script(_JS_DRILL_BASE, data, _ICON_MENU, _ICON_CHEVR, _ICON_PLAN)

def _build_subtasks_right(epic):
    chevR = _ICON_CHEVR
    planIcon = _ICON_PLAN
    rows = ''
    for s in epic["subtasks"]:
        meta = '<span class="kanban-drill-subtask-meta">' + s["meta"] + '</span>' if s.get("meta") else ''
        rows += '<div class="kanban-drill-subtask-row"><span class="kanban-drill-subtask-grip"><svg width="4" height="14" viewBox="0 0 4 14"><circle cx="2" cy="2" r="1" fill="currentColor"/><circle cx="2" cy="7" r="1" fill="currentColor"/><circle cx="2" cy="12" r="1" fill="currentColor"/></svg></span><div class="kanban-drill-subtask-status kanban-status-clickable" style="background:' + s["color"] + '26;color:' + s["color"] + ';">' + s["status"] + '</div><span class="kanban-drill-subtask-title">' + s["title"] + '</span>' + meta + '<span class="kanban-drill-subtask-chevron">' + chevR + '</span></div>'
    rows += '<div class="kanban-drill-subtask-row kanban-drill-ghost-row"><span class="kanban-drill-subtask-grip" style="visibility:hidden;"></span><div class="kanban-drill-subtask-status" style="background:var(--bg-subtle);color:var(--text-dim);">new</div><input type="text" class="kanban-drill-ghost-input" placeholder="Add subtask\u2026" disabled style="flex:1;background:none;border:none;outline:none;font-size:13px;color:var(--text-primary);font-family:inherit;padding:0 0 0 5px;"></div>'
    pct = epic.get("pct", 0)
    return '<div class="kanban-drill-panel-header"><span style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;color:var(--text-dim);">Subtasks</span><span class="kanban-drill-inline-progress" style="display:inline-flex;align-items:center;gap:6px;margin-left:auto;"><span class="kanban-drill-inline-bar"><span class="kanban-drill-inline-fill" style="width:' + str(pct) + '%;"></span></span><span class="kanban-drill-inline-pct">' + str(pct) + '%</span></span></div><div class="kanban-drill-panel"><div class="kanban-drill-panel-body">' + rows + '</div></div><button class="kanban-drill-ai-plan-btn" style="margin-top:10px;">' + planIcon + ' Plan with AI</button>'

def _build_sessions_right(epic):
    chevR = _ICON_CHEVR
    rows = ''
    for s in epic["sessions"]:
        rows += '<div class="kanban-drill-session-row" style="cursor:pointer;"><div class="kanban-drill-subtask-status" style="background:' + s["color"] + '26;color:' + s["color"] + ';">' + s["status"] + '</div><span class="kanban-drill-session-name">' + s["name"] + '</span><span class="kanban-drill-subtask-chevron">' + chevR + '</span></div>'
    rows += '<div class="kanban-drill-session-row kanban-drill-ghost-row" style="cursor:pointer;"><div class="kanban-drill-subtask-status" style="background:var(--bg-subtle);color:var(--text-dim);">new</div><span class="kanban-drill-session-name" style="color:var(--text-dim);">Spawn session\u2026</span></div>'
    return '<div class="kanban-drill-panel-header"><span style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;color:var(--text-dim);">Sessions</span></div><div class="kanban-drill-panel"><div class="kanban-drill-panel-body">' + rows + '</div></div>'

def _build_chooser_right():
    listIcon = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>'
    boltIcon = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>'
    planIcon = _ICON_PLAN
    return '<div class="kanban-drill-chooser"><div style="font-size:12px;color:var(--text-dim);margin-bottom:6px;text-transform:uppercase;letter-spacing:0.5px;font-weight:600;">How to proceed</div>' \
        '<div class="kanban-drill-chooser-card"><div class="kanban-drill-chooser-icon" style="color:var(--accent);">' + listIcon + '</div><div><div class="kanban-drill-chooser-title">Break into subtasks</div><div class="kanban-drill-chooser-desc">Subdivide into smaller pieces. Each subtask gets its own status and sessions.</div></div></div>' \
        '<div class="kanban-drill-chooser-card"><div class="kanban-drill-chooser-icon" style="color:var(--green);">' + boltIcon + '</div><div><div class="kanban-drill-chooser-title">Spawn sessions</div><div class="kanban-drill-chooser-desc">Start working directly. Spawn Claude sessions scoped to this task.</div></div></div>' \
        '<div class="kanban-drill-chooser-card kanban-drill-chooser-ai"><div class="kanban-drill-chooser-icon" style="color:var(--purple);">' + planIcon + '</div><div><div class="kanban-drill-chooser-title">Plan with AI</div><div class="kanban-drill-chooser-desc">Describe a goal and Claude will break it down into a structured set of subtasks.</div></div><svg class="kanban-drill-chooser-ai-arrow" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M5 12h14"/><path d="M12 5l7 7-7 7"/></svg></div>' \
        '</div>'

# ---------------------------------------------------------------------------
# SCREENSHOT FLOW
# ---------------------------------------------------------------------------

def run():
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    drv = _driver()
    print("\n--- Taking screenshots ---\n")
    try:
        # 1 -- Homepage
        print("  [1/8] Homepage")
        _fresh_page(drv, "homepage")
        # Scale up the homepage cards by 20%
        drv.execute_script("var cards=document.querySelector('.homepage-cards');if(cards){cards.style.transform='scale(1.2)';cards.style.transformOrigin='center center';}")
        time.sleep(2); _snap(drv, "homepage", "homepage with 3 cards")

        # 2 -- Session Grid (sidebar grid + conversation in main)
        print("  [2/8] Session Grid")
        drv.execute_script("localStorage.setItem('viewMode','sessions');localStorage.setItem('sessionDisplayMode','grid');")
        drv.get(BASE); time.sleep(2)
        drv.execute_script(_JS_FREEZE); time.sleep(3)
        drv.execute_script(_JS_DISMISS); time.sleep(1)
        drv.execute_script(_JS_DISMISS); drv.execute_script(_JS_FREEZE)
        # Click a real card to open the live panel
        drv.execute_script("var g=document.getElementById('workforce-grid');if(g)g.classList.add('visible');")
        drv.execute_script("var c=document.querySelectorAll('.wf-card');if(c.length)c[0].click();")
        time.sleep(5)
        # Stop live stream, freeze renders, then inject our content
        drv.execute_script("if(typeof stopLivePanel==='function')stopLivePanel();")
        drv.execute_script(_JS_FREEZE)
        drv.execute_script(_JS_LIVE, CONVERSATION)
        drv.execute_script(_JS_GRID, SESSIONS)
        drv.execute_script("var g=document.getElementById('workforce-grid');if(g)g.classList.add('visible');")
        time.sleep(1); _snap(drv, "session-grid", "session grid with conversation")

        # 3 -- Session List (sidebar list + conversation in main)
        print("  [3/8] Session List")
        drv.execute_script("localStorage.setItem('viewMode','sessions');localStorage.setItem('sessionDisplayMode','list');")
        drv.get(BASE); time.sleep(5)
        drv.execute_script(_JS_DISMISS); time.sleep(1)
        drv.execute_script(_JS_DISMISS)
        # Open a real session to bootstrap the live panel
        drv.execute_script("var s=typeof allSessions!=='undefined'&&allSessions.length?allSessions[0]:null;if(s&&typeof openInGUI==='function')openInGUI(s.id);")
        time.sleep(5)
        # Stop live stream, freeze, then inject our content
        drv.execute_script("if(typeof stopLivePanel==='function')stopLivePanel();")
        drv.execute_script(_JS_FREEZE)
        drv.execute_script(_JS_LIVE, CONVERSATION)
        drv.execute_script(_JS_LIST, SESSIONS)
        time.sleep(1); _snap(drv, "session-list", "session list with conversation")

        # 4 -- Workflow Board
        print("  [4/8] Workflow Board")
        _fresh_page(drv, "kanban")
        drv.execute_script(_JS_BOARD, BOARD)
        time.sleep(1); _snap(drv, "workflow-board", "kanban with demo tasks")

        # 5 -- Task Hierarchy: Subtasks
        print("  [5/8] Task Hierarchy (subtasks)")
        d = dict(EPIC_SUBTASKS)
        d["rightHTML"] = _build_subtasks_right(d)
        _drill(drv, d)
        time.sleep(1); _snap(drv, "task-hierarchy", "epic drill-down subtasks")

        # 6 -- Task Hierarchy: Sessions
        print("  [6/8] Task Hierarchy (sessions)")
        d = dict(EPIC_SESSIONS)
        d["rightHTML"] = _build_sessions_right(d)
        _drill(drv, d)
        time.sleep(1); _snap(drv, "task-sessions", "epic drill-down sessions")

        # 7 -- Task Hierarchy: Chooser
        print("  [7/8] Task Hierarchy (chooser)")
        d = dict(EPIC_CHOOSER)
        d["rightHTML"] = _build_chooser_right()
        _drill(drv, d)
        time.sleep(1); _snap(drv, "task-chooser", "empty task chooser")

        # 8 -- Kanban Session (Board > Task > Session with conversation)
        print("  [8/9] Kanban Session")
        # Load kanban mode to get real sidebar, then inject session view
        _fresh_page(drv, "kanban")
        drv.execute_script(_JS_BOARD, BOARD)
        time.sleep(1)
        # Inject breadcrumb bar, hide kanban board, show main-body with conversation
        drv.execute_script(_JS_KANBAN_SESSION,
            {"taskTitle": "Auth System Overhaul", "sessionName": "OAuth2 provider integration"},
            _ICON_MENU, _ICON_CHEVR)
        drv.execute_script(_JS_LIVE, CONVERSATION)
        drv.execute_script("document.getElementById('main-toolbar').style.display='none';")
        time.sleep(1); _snap(drv, "kanban-session", "session from kanban drill-down")


        # 9 -- Workforce (inject command center into main-body)
        print("  [9/9] Workforce")
        _fresh_page(drv, "workplace")
        drv.execute_script(r"""(function(){
var mb=document.getElementById('main-body');if(!mb)return;mb.style.display='';
var wI='<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#5555bb" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>';
var qI='<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#ff9500" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><circle cx="12" cy="17" r=".5" fill="#ff9500"/></svg>';
var iI='<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#44aa66" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>';
var sI='<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#888" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M15 9l-6 6M9 9h6M9 15h6"/></svg>';
var fI='<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';
var ic={working:wI,question:qI,idle:iI,sleeping:sI};
var depts=[['Backend','#3b82f6',6,3],['Frontend','#8b5cf6',4,1],['DevOps','#f97316',3,1],['Security','#ef4444',2,1],['QA','#14b8a6',3,0]];
var recent=[['OAuth2 provider integration','working','Backend','2:31 PM'],['Rate limiting middleware','working','Security','2:28 PM'],['DB query caching layer','working','Backend','2:15 PM'],['Fix WebSocket reconnect bug','question','Frontend','2:10 PM'],['Frontend bundle analysis','idle','Frontend','1:45 PM'],['API response compression','idle','Backend','1:30 PM'],['CI/CD pipeline hardening','idle','DevOps','12:15 PM'],['Session token encryption','idle','Security','12:50 PM']];
var h='<div class="ws-container"><div class="wf-command-center">';
h+='<div class="wf-cc-header"><div class="wf-cc-title">Workforce</div><div class="wf-cc-subtitle">5 departments \u00b7 3 sub-departments \u00b7 16 sessions</div></div>';
h+='<div class="wf-cc-stats">';
h+='<div class="wf-cc-stat wf-cc-stat-working"><div class="wf-cc-stat-icon">'+wI+'</div><div class="wf-cc-stat-num">3</div><div class="wf-cc-stat-label">Working</div></div>';
h+='<div class="wf-cc-stat wf-cc-stat-waiting"><div class="wf-cc-stat-icon">'+qI+'</div><div class="wf-cc-stat-num">1</div><div class="wf-cc-stat-label">Waiting</div></div>';
h+='<div class="wf-cc-stat wf-cc-stat-idle"><div class="wf-cc-stat-icon">'+iI+'</div><div class="wf-cc-stat-num">4</div><div class="wf-cc-stat-label">Idle</div></div>';
h+='<div class="wf-cc-stat wf-cc-stat-sleeping"><div class="wf-cc-stat-icon">'+sI+'</div><div class="wf-cc-stat-num">8</div><div class="wf-cc-stat-label">Sleeping</div></div>';
h+='</div><div class="wf-cc-section-label">Departments</div><div class="ws-canvas">';
depts.forEach(function(d){h+='<div class="ws-folder-card"><div class="ws-folder-icon" style="color:'+d[1]+';">'+fI+'</div><div class="ws-folder-name">'+d[0]+'</div><div class="ws-folder-meta">'+d[2]+' agents \u00b7 '+d[3]+' active</div></div>';});
h+='</div><div class="wf-cc-section-label">Recent Sessions</div><div class="wf-cc-recent">';
recent.forEach(function(r){h+='<div class="wf-cc-recent-row"><span class="wf-cc-recent-icon">'+ic[r[1]]+'</span><span class="wf-cc-recent-name">'+r[0]+'</span><span class="wf-cc-recent-dept">'+r[2]+'</span><span class="wf-cc-recent-date">'+r[3]+'</span></div>';});
h+='</div></div></div>';mb.innerHTML=h;
var sp=document.getElementById('sidebar-perm-panel');
if(sp){sp.style.display='';sp.innerHTML='<div class="kanban-sidebar-section"><div class="kanban-sidebar-label">Permissions</div><button class="kanban-sidebar-btn"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg> Manual</button><div class="ws-perm-card"><div class="ws-perm-card-top"><span class="ws-perm-card-session">Fix WebSocket reconnect bug</span><span class="ws-perm-tool">Bash</span></div><div class="ws-perm-cmd">npm test -- --grep reconnect</div><div class="ws-perm-actions"><button class="ws-perm-btn ws-perm-allow">Allow</button><button class="ws-perm-btn ws-perm-deny">Deny</button><button class="ws-perm-btn ws-perm-always">Always</button></div></div><div class="ws-perm-card"><div class="ws-perm-card-top"><span class="ws-perm-card-session">OAuth2 provider integration</span><span class="ws-perm-tool">Write</span></div><div class="ws-perm-cmd">app/auth/oauth2.py</div><div class="ws-perm-actions"><button class="ws-perm-btn ws-perm-allow">Allow</button><button class="ws-perm-btn ws-perm-deny">Deny</button><button class="ws-perm-btn ws-perm-always">Always</button></div></div></div>';}
})();""")
        time.sleep(1); _snap(drv, "workforce", "workforce command center")
        print(f"\n  All saved to {SCREENSHOT_DIR}/")
    finally:
        drv.quit()

def main():
    print("\nVibeNode Photo Shoot (safe mode)")
    print("=" * 40)
    print("  DOM injection only -- no API writes, no DB changes\n")
    try:
        r = requests.get(f"{BASE}/api/sessions", timeout=5)
        r.raise_for_status()
        print(f"  Server OK at {BASE} ({len(r.json())} sessions)\n")
    except Exception as e:
        print(f"  ERROR: VibeNode not running at {BASE}\n  {e}\n")
        sys.exit(1)
    run()
    print("\n  Done! No data was created, modified, or deleted.\n")

if __name__ == "__main__":
    main()
