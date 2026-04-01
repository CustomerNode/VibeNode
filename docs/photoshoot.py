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
PROJECT = "C--Users-15512-Documents-VibeNode"

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

EPIC = {
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

_JS_DISMISS = r"(function(){var s=document.getElementById('ps-hide');if(!s){s=document.createElement('style');s.id='ps-hide';s.textContent='#project-overlay,#health-blocker,#pm-overlay,#compare-overlay,.modal-overlay,#extract-drawer,.boot-splash,#project-card,.dashboard,#btn-git-publish,#git-sync-overlay{display:none!important;visibility:hidden!important;opacity:0!important}img{visibility:hidden!important;width:0!important;height:0!important;position:absolute!important;}';document.head.appendChild(s);}['project-overlay','health-blocker','pm-overlay','compare-overlay','git-sync-overlay'].forEach(function(id){var e=document.getElementById(id);if(e){e.classList.remove('show');e.style.display='none';}});var gb=document.getElementById('btn-git-publish');if(gb)gb.style.display='none';var d=document.querySelector('.dashboard');if(d)d.style.display='none';document.querySelectorAll('.show').forEach(function(e){var p=getComputedStyle(e).position;if(p==='fixed'||p==='absolute'){e.classList.remove('show');e.style.display='none';}});window.openProjectOverlay=function(){};window.openHealthBlocker=function(){};window.openGitPublish=function(){};})();"

_JS_BOARD = r"""(function(cols){var board=document.getElementById('kanban-board');if(!board)return;board.innerHTML='';var w=document.createElement('div');w.className='kanban-columns-wrapper';board.appendChild(w);cols.forEach(function(col){var c=document.createElement('div');c.className='kanban-column';c.setAttribute('data-status',col.status);var h=document.createElement('div');h.className='kanban-column-header';h.innerHTML='<div class="kanban-column-color-bar" style="background:'+col.color+'"></div><span class="kanban-column-name">'+col.label+'</span><span class="kanban-column-count">'+col.tasks.length+'</span>';c.appendChild(h);var b=document.createElement('div');b.className='kanban-column-body';b.setAttribute('data-status',col.status);c.appendChild(b);col.tasks.forEach(function(t){var card=document.createElement('div');card.className='kanban-card';card.setAttribute('data-status',col.status);var tags='';(t.tags||[]).forEach(function(g){tags+='<span class="kanban-tag-pill" style="background:'+g[1]+'22;color:'+g[1]+';border-color:'+g[1]+'44;">'+g[0]+'</span>';});var mp=[];if(t.subtasks)mp.push('<div class="kanban-card-subtask-text">'+t.subtasks+'</div>');if(t.sessions)mp.push('<div class="kanban-card-session-badge" style="background:#3b82f622;color:#60a5fa;">\u25cf '+t.sessions+'</div>');var mr=mp.length?'<div style="display:flex;gap:6px;align-items:center;margin-top:6px;flex-wrap:wrap;">'+mp.join('')+'</div>':'';card.innerHTML='<div class="kanban-card-header"><div class="kanban-card-title-row"><span class="kanban-card-title">'+t.title+'</span><span class="kanban-card-time">'+(t.date||'')+'</span></div><span class="kanban-card-owner">'+(t.owner||'')+'</span></div><div class="kanban-card-desc">'+(t.desc||'')+'</div>'+mr+'<div class="kanban-card-bottom"><div class="kanban-card-tags">'+tags+'</div></div>';b.appendChild(card);});w.appendChild(c);});window.initKanban=function(){};
})(arguments[0]);"""

_JS_DRILL = r"""(function(d){var board=document.getElementById('kanban-board');if(!board)return;board.innerHTML='';var tb=document.createElement('div');tb.className='kanban-drill-titlebar';tb.innerHTML='<div class="kanban-drill-breadcrumb"><span class="kanban-drill-crumb"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12h18M3 6h18M3 18h18"/></svg> Board</span><span class="kanban-drill-sep"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18l6-6-6-6"/></svg></span><span class="kanban-drill-crumb current">'+d.title+'</span></div>';board.appendChild(tb);var th='';(d.tags||[]).forEach(function(g){th+='<span class="kanban-tag-pill" style="background:'+g[1]+'22;color:'+g[1]+';border-color:'+g[1]+'44;">'+g[0]+'</span>';});var rh='';d.subtasks.forEach(function(s){var m=s.meta?'<span class="kanban-drill-subtask-meta">'+s.meta+'</span>':'';rh+='<div class="kanban-drill-subtask-row"><div class="kanban-drill-subtask-status" style="background:'+s.color+'26;color:'+s.color+';">'+s.status+'</div><span class="kanban-drill-subtask-title">'+s.title+'</span>'+m+'<span class="kanban-drill-subtask-chevron"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18l6-6-6-6"/></svg></span></div>';});rh+='<div class="kanban-drill-subtask-row kanban-drill-ghost-row"><div class="kanban-drill-subtask-status" style="background:var(--bg-subtle);color:var(--text-dim);">New</div><span style="color:var(--text-dim);font-size:13px;">Add subtask\u2026</span></div>';var bd=document.createElement('div');bd.className='kanban-drill-body';bd.innerHTML='<div class="kanban-drill-split"><div class="kanban-drill-left"><div class="kanban-drill-status" style="background:'+d.color+'26;color:'+d.color+';display:inline-block;padding:4px 14px;border-radius:6px;font-weight:700;font-size:12px;text-transform:uppercase;letter-spacing:0.5px;">'+d.status+' <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M9 18l6-6-6-6"/></svg></div><div class="kanban-drill-title" style="font-size:22px;font-weight:700;margin:12px 0 4px;">'+d.title+'</div><div style="font-size:11px;color:var(--text-dim);margin:4px 0 16px;">Created '+d.created+' \u00b7 Updated '+d.updated+'</div><div class="kanban-drill-desc-wrap"><div class="kanban-drill-desc" style="font-size:14px;line-height:1.6;">'+d.desc+'</div></div><div class="kanban-drill-tags-section" style="margin-top:16px;"><div class="kanban-drill-tags-list" style="display:flex;gap:6px;flex-wrap:wrap;">'+th+'</div></div><div class="kanban-drill-ai-plan-card" style="margin-top:24px;padding:14px 18px;border-radius:10px;background:linear-gradient(135deg,rgba(139,92,246,0.12),rgba(59,130,246,0.10));border:1px solid rgba(139,92,246,0.2);display:flex;align-items:center;gap:12px;"><div style="font-size:20px;"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#a855f7" stroke-width="2"><path d="M12 2a5 5 0 0 1 5 5c0 1.5-.5 2.5-1.5 3.5L12 14l-3.5-3.5C7.5 9.5 7 8.5 7 7a5 5 0 0 1 5-5z"/><path d="M12 14v8M8 18h8"/></svg></div><div style="font-weight:600;font-size:14px;color:var(--accent,#a855f7);">Plan with AI</div></div></div><div class="kanban-drill-right"><div class="kanban-drill-panel-header" style="display:flex;align-items:center;gap:8px;padding:0 0 8px;"><span style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;color:var(--text-dim);">SUBTASKS</span><span style="margin-left:auto;display:flex;align-items:center;gap:6px;"><span class="kanban-drill-inline-bar" style="width:60px;height:6px;border-radius:3px;background:var(--bg-subtle);display:inline-block;overflow:hidden;"><span class="kanban-drill-inline-fill" style="display:block;height:100%;width:'+d.pct+'%;background:#22c55e;border-radius:3px;"></span></span><span style="font-size:11px;color:var(--text-dim);">'+d.pct+'%</span></span></div><div class="kanban-drill-panel"><div class="kanban-drill-panel-body">'+rh+'</div></div></div></div>';board.appendChild(bd);
})(arguments[0]);"""

def _driver():
    opts = Options()
    for a in ["--headless=new","--window-size=1920,1080","--force-device-scale-factor=1","--disable-gpu","--no-sandbox","--hide-scrollbars"]:
        opts.add_argument(a)
    return webdriver.Chrome(options=opts)

def _init(drv):
    drv.get(BASE); time.sleep(1)
    drv.execute_script("localStorage.setItem('activeProject',arguments[0]);localStorage.setItem('theme','dark');localStorage.setItem('viewMode','workforce');localStorage.setItem('sidebarCollapsed','false');", PROJECT)
    drv.get(BASE); time.sleep(4)
    drv.execute_script("document.documentElement.setAttribute('data-theme','dark');")
    drv.execute_script(_JS_DISMISS); time.sleep(2)
    drv.execute_script(_JS_DISMISS); time.sleep(1)
    n = drv.execute_script("return document.querySelectorAll('.wf-card,.card,.session-item,.kanban-card').length;")
    print(f"  Browser ready ({n} items)")
    if n == 0:
        drv.execute_script("if(typeof loadSessions==='function') loadSessions();")
        time.sleep(3)

def _snap(drv, name, label):
    drv.execute_script(_JS_DISMISS); time.sleep(0.5)
    p = os.path.join(SCREENSHOT_DIR, f"{name}.png")
    drv.save_screenshot(p)
    print(f"    {name}.png ({os.path.getsize(p)//1024}KB) {label}")

def run():
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    drv = _driver()
    print("\n--- Taking screenshots ---\n")
    try:
        _init(drv)

        print("  [1/5] Session Grid")
        drv.execute_script("if(typeof setViewMode==='function') setViewMode('workforce');")
        time.sleep(2)
        drv.execute_script("var p=document.querySelector('.live-panel');if(p)p.style.display='none';var m=document.querySelector('.main');if(m)m.style.flex='1';")
        time.sleep(1); _snap(drv, "session-grid", "workforce grid")

        print("  [2/5] Workflow Board")
        drv.execute_script("if(typeof setViewMode==='function') setViewMode('kanban');")
        time.sleep(3); drv.execute_script(_JS_DISMISS)
        drv.execute_script(_JS_BOARD, BOARD)
        time.sleep(1); _snap(drv, "workflow-board", "kanban with demo tasks")

        print("  [3/5] Task Hierarchy")
        drv.execute_script(_JS_DRILL, EPIC)
        time.sleep(1); _snap(drv, "task-hierarchy", "epic drill-down")

        print("  [4/5] Live Session")
        drv.execute_script("if(typeof setViewMode==='function') setViewMode('workforce');")
        time.sleep(2)
        drv.execute_script("var p=document.querySelector('.live-panel');if(p)p.style.display='';var m=document.querySelector('.main');if(m)m.style.flex='';")
        time.sleep(1)
        drv.execute_script("var c=document.querySelectorAll('.wf-card');if(c.length)c[0].click();")
        time.sleep(4); _snap(drv, "live-session", "session with live panel")

        print("  [5/5] Session List")
        drv.execute_script("var p=document.querySelector('.live-panel');if(p)p.style.display='none';")
        drv.execute_script("if(typeof setViewMode==='function') setViewMode('list');")
        time.sleep(2); _snap(drv, "session-list", "compact list view")
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
