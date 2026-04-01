"""
VibeNode README Photo Shoot
============================
Stages demo data, takes marketing screenshots via headless Chrome, then cleans up.
Run: python docs/photoshoot.py
"""

import json
import time
import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

BASE = "http://localhost:5050"
API = f"{BASE}/api/kanban"
HEADERS = {"Content-Type": "application/json"}
SCREENSHOT_DIR = "docs/screenshots"
PROJECT_ENCODED = "C--Users-15512-Documents-VibeNode"

# Track all created task IDs for cleanup
created_ids = []


def api(method, path, data=None):
    """Make an API call and return the JSON response."""
    url = f"{BASE}{path}"
    r = getattr(requests, method)(url, json=data, headers=HEADERS)
    if r.status_code >= 400:
        print(f"  ERROR {r.status_code}: {r.text[:200]}")
        return None
    return r.json()


def create_task(title, parent_id=None, description="", status="not_started", tags=None):
    """Create a task and optionally move it + add tags."""
    data = {"title": title, "description": description}
    if parent_id:
        data["parent_id"] = parent_id
    result = api("post", "/api/kanban/tasks", data)
    if not result:
        return None
    tid = result["id"]
    created_ids.append(tid)
    if status != "not_started":
        api("post", f"/api/kanban/tasks/{tid}/move", {"status": status, "force": True})
    if tags:
        for tag in tags:
            api("post", f"/api/kanban/tasks/{tid}/tags", {"tag": tag})
    print(f"  Created: {title} [{status}]")
    return tid


def link_session(task_id, session_id):
    """Link a Claude session to a task."""
    api("post", f"/api/kanban/tasks/{task_id}/sessions", {"session_id": session_id})
    print(f"  Linked session {session_id[:8]}... to task {task_id[:8]}...")


def cleanup():
    """Delete all tasks we created (children first, then parents)."""
    print("\n--- Cleaning up demo data ---")
    for tid in reversed(created_ids):
        try:
            requests.delete(f"{BASE}/api/kanban/tasks/{tid}", headers=HEADERS)
        except Exception:
            pass
    print(f"  Deleted {len(created_ids)} demo tasks")


def get_sessions():
    """Get list of real sessions for linking."""
    r = requests.get(f"{BASE}/api/sessions")
    return r.json() if r.status_code == 200 else []


def stage_demo_data():
    """Create a realistic task hierarchy for screenshots."""
    print("\n=== Staging demo data ===\n")

    sessions = get_sessions()
    session_ids = [s["id"] for s in sessions if s.get("message_count", 0) > 0]

    # -----------------------------------------------------------------------
    # Epic 1: Authentication System (Working) - has subtasks showing hierarchy
    # -----------------------------------------------------------------------
    auth_epic = create_task(
        "Authentication System Overhaul",
        description="Modernize the auth stack: migrate to OAuth2 with PKCE flow, implement encrypted session tokens, add rate limiting, and build role-based access control.",
        status="working",
        tags=["epic", "security"]
    )

    auth_oauth = create_task(
        "OAuth2 provider integration",
        parent_id=auth_epic,
        description="Implement OAuth2 authorization code flow with PKCE for Google, GitHub, and Microsoft identity providers.",
        status="working",
        tags=["backend"]
    )
    if session_ids:
        link_session(auth_oauth, session_ids[0])

    create_task(
        "Session token encryption",
        parent_id=auth_epic,
        description="Replace plain JWT tokens with AES-256-GCM encrypted tokens. Add key rotation support.",
        status="complete",
        tags=["backend", "security"]
    )

    create_task(
        "Rate limiting middleware",
        parent_id=auth_epic,
        description="Implement sliding window rate limiter with Redis backend. Per-endpoint and per-user limits.",
        status="validating",
        tags=["backend"]
    )

    create_task(
        "User role permissions",
        parent_id=auth_epic,
        description="Build RBAC system with hierarchical roles: admin > editor > viewer. Middleware for route protection.",
        status="not_started",
        tags=["backend"]
    )

    # -----------------------------------------------------------------------
    # Epic 2: Performance Sprint (Working) - another epic with subtasks
    # -----------------------------------------------------------------------
    perf_epic = create_task(
        "Performance Optimization Sprint",
        description="Target: sub-200ms API responses, 90+ Lighthouse score. Focus on caching, code splitting, and compression.",
        status="working",
        tags=["epic", "performance"]
    )

    perf_cache = create_task(
        "Database query caching layer",
        parent_id=perf_epic,
        description="Add Redis caching for expensive queries. Implement cache invalidation on write paths. Target: 10x read speedup.",
        status="working",
        tags=["backend", "performance"]
    )
    if len(session_ids) > 1:
        link_session(perf_cache, session_ids[1])

    create_task(
        "Frontend bundle splitting",
        parent_id=perf_epic,
        description="Split vendor and app bundles. Implement route-based code splitting with dynamic imports.",
        status="complete",
        tags=["frontend"]
    )

    create_task(
        "API response compression",
        parent_id=perf_epic,
        description="Enable Brotli/gzip compression. Implement ETags for conditional requests.",
        status="working",
        tags=["backend"]
    )

    # -----------------------------------------------------------------------
    # Standalone tasks across columns
    # -----------------------------------------------------------------------
    create_task(
        "API Documentation v2",
        description="Generate OpenAPI 3.1 spec from route decorators. Auto-publish to docs portal with versioning.",
        status="validating",
        tags=["docs"]
    )

    create_task(
        "CI/CD pipeline hardening",
        description="Add parallel test stages, container scanning, and automated rollback on failure.",
        status="complete",
        tags=["devops"]
    )

    create_task(
        "Mobile responsive layouts",
        description="Audit and fix all views for mobile breakpoints. Target: full functionality on 375px+ screens.",
        status="not_started",
        tags=["frontend", "design"]
    )

    create_task(
        "WebSocket reconnection handling",
        description="Implement exponential backoff with jitter for WebSocket reconnections. Add connection quality indicator.",
        status="remediating",
        tags=["frontend", "reliability"]
    )

    create_task(
        "Database migration tooling",
        description="Build migration runner with up/down support, dry-run mode, and automatic backup before migration.",
        status="not_started",
        tags=["backend", "devops"]
    )

    create_task(
        "Telemetry & observability",
        description="Add structured logging, distributed tracing with OpenTelemetry, and custom Prometheus metrics.",
        status="complete",
        tags=["devops", "observability"]
    )

    print(f"\n  Total tasks staged: {len(created_ids)}")
    return auth_epic, perf_epic


def _dismiss_overlays(d):
    """Inject a persistent CSS rule that force-hides all overlays, then dismiss via JS."""
    d.execute_script('''
        // Persistent CSS override — survives any JS re-showing
        var s = document.getElementById("photoshoot-overrides");
        if (!s) {
            s = document.createElement("style");
            s.id = "photoshoot-overrides";
            s.textContent = [
                "#project-overlay", "#health-blocker", "#pm-overlay",
                "#compare-overlay", ".modal-overlay", "#extract-drawer",
                ".boot-splash", "#project-card"
            ].join(",") + "{display:none!important;visibility:hidden!important;opacity:0!important}";
            document.head.appendChild(s);
        }
        // Also JS-dismiss in case something checks classList
        ["project-overlay","health-blocker","pm-overlay","compare-overlay"].forEach(function(id) {
            var el = document.getElementById(id);
            if (el) { el.classList.remove("show"); el.style.display = "none"; }
        });
        document.querySelectorAll(".show").forEach(function(el) {
            var pos = getComputedStyle(el).position;
            if (pos === "fixed" || pos === "absolute") {
                el.classList.remove("show");
                el.style.display = "none";
            }
        });
    ''')





def init_browser(driver):
    """Navigate to app, set localStorage, dismiss overlays, and ensure app is ready."""
    # Step 1: Navigate to set localStorage BEFORE the app fully initializes
    driver.get(BASE)
    time.sleep(1)

    # Step 2: Set localStorage for project + theme + view mode, then reload
    driver.execute_script(f"""
        localStorage.setItem('activeProject', '{PROJECT_ENCODED}');
        localStorage.setItem('theme', 'dark');
        localStorage.setItem('viewMode', 'workforce');
        localStorage.setItem('sidebarCollapsed', 'false');
    """)

    # Step 3: Reload so the app boots with correct localStorage
    driver.get(BASE)
    time.sleep(4)

    # Step 4: Force dark theme
    driver.execute_script("""
        document.documentElement.setAttribute('data-theme', 'dark');
    """)

    # Step 5: Inject persistent overlay suppression (CSS !important + JS dismiss)
    _dismiss_overlays(driver)
    time.sleep(3)
    _dismiss_overlays(driver)
    time.sleep(1)

    # Step 6: Verify content is visible - check if sessions loaded
    session_count = driver.execute_script("""
        var cards = document.querySelectorAll('.card, .session-row, .kanban-card');
        return cards.length;
    """)
    print(f"  Browser init complete. Visible items: {session_count}")

    # If no sessions visible, try triggering loadSessions manually
    if session_count == 0:
        driver.execute_script("""
            if (typeof loadSessions === 'function') loadSessions();
        """)
        time.sleep(3)
        session_count = driver.execute_script("""
            return document.querySelectorAll('.card, .session-row, .kanban-card').length;
        """)
        print(f"  After manual load: {session_count} items")


def take_screenshots(auth_epic_id, perf_epic_id):
    """Launch headless Chrome and take marketing screenshots."""
    print("\n=== Taking screenshots ===\n")

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--force-device-scale-factor=1")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--hide-scrollbars")
    driver = webdriver.Chrome(options=opts)

    try:
        init_browser(driver)

        # ===================================================================
        # SHOT 1: Session Grid -- multiple sessions at a glance
        # ===================================================================
        print("  Shot 1: Session Grid View...")
        driver.execute_script("if(typeof setViewMode==='function') setViewMode('workforce');")
        time.sleep(2)

        # Hide the live panel to show full grid
        driver.execute_script("""
            var lp = document.querySelector('.live-panel');
            if(lp) lp.style.display = 'none';
            var main = document.querySelector('.main');
            if(main) main.style.flex = '1';
        """)
        time.sleep(1)
        _dismiss_overlays(driver)
        driver.save_screenshot(f"{SCREENSHOT_DIR}/session-grid.png")
        print("    OK session-grid.png")

        # ===================================================================
        # SHOT 2: Workflow Board -- the full kanban with tasks across columns
        # ===================================================================
        print("  Shot 2: Workflow Board...")
        driver.execute_script("if(typeof setViewMode==='function') setViewMode('kanban');")
        time.sleep(4)

        # Dismiss overlays again (view switch may trigger something)
        driver.execute_script("""
            document.querySelectorAll('[id$="-overlay"]').forEach(function(el) {
                el.classList.remove('show');
            });
        """)
        time.sleep(1)

        _dismiss_overlays(driver)
        driver.save_screenshot(f"{SCREENSHOT_DIR}/workflow-board.png")
        print("    OK workflow-board.png")

        # ===================================================================
        # SHOT 3: Task Hierarchy -- drill into the auth epic to show subtasks
        #          This is the KEY "branching" screenshot
        # ===================================================================
        print("  Shot 3: Task Hierarchy (branching into subtasks)...")
        driver.execute_script(f"""
            if(typeof drillDown==='function') drillDown('{auth_epic_id}');
        """)
        time.sleep(3)

        _dismiss_overlays(driver)
        driver.save_screenshot(f"{SCREENSHOT_DIR}/task-hierarchy.png")
        print("    OK task-hierarchy.png")

        # Go back to board view
        driver.execute_script("if(typeof initKanban==='function') initKanban(true);")
        time.sleep(1)

        # ===================================================================
        # SHOT 4: Live Panel -- select a session and show live panel
        # ===================================================================
        print("  Shot 4: Live Session Panel...")
        driver.execute_script("if(typeof setViewMode==='function') setViewMode('workforce');")
        time.sleep(2)

        # Restore the live panel
        driver.execute_script("""
            var lp = document.querySelector('.live-panel');
            if(lp) lp.style.display = '';
            var main = document.querySelector('.main');
            if(main) main.style.flex = '';
        """)
        time.sleep(1)

        # Click the first session with content
        driver.execute_script("""
            var cards = document.querySelectorAll('.card');
            for (var i = 0; i < cards.length; i++) {
                var card = cards[i];
                if (card.querySelector('.card-preview') &&
                    card.querySelector('.card-preview').textContent.trim().length > 10) {
                    card.click();
                    break;
                }
            }
        """)
        time.sleep(4)

        _dismiss_overlays(driver)
        driver.save_screenshot(f"{SCREENSHOT_DIR}/live-session.png")
        print("    OK live-session.png")

        # ===================================================================
        # SHOT 5: List View -- compact session list
        # ===================================================================
        print("  Shot 5: List View...")
        driver.execute_script("""
            var lp = document.querySelector('.live-panel');
            if(lp) lp.style.display = 'none';
        """)
        driver.execute_script("if(typeof setViewMode==='function') setViewMode('list');")
        time.sleep(2)

        _dismiss_overlays(driver)
        driver.save_screenshot(f"{SCREENSHOT_DIR}/session-list.png")
        print("    OK session-list.png")

        print(f"\n  All screenshots saved to {SCREENSHOT_DIR}/")

    finally:
        driver.quit()


def main():
    # Verify the app is running
    try:
        r = requests.get(f"{BASE}/api/sessions", timeout=5)
        r.raise_for_status()
    except Exception as e:
        print(f"ERROR: VibeNode is not running at {BASE}")
        print(f"  {e}")
        return

    auth_epic_id = perf_epic_id = None
    try:
        auth_epic_id, perf_epic_id = stage_demo_data()
        take_screenshots(auth_epic_id, perf_epic_id)
    finally:
        cleanup()

    print("\n=== Photo shoot complete! ===")


if __name__ == "__main__":
    main()
