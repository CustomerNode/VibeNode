"""[subsessions phase -1] Selenium smoke test — Compose board DOM skeleton survives a project switch.

Catches CLAUDE.md Compose fix #4 (DOM-skeleton preservation): the
project-switch cleanup code in ``static/js/app.js`` must NOT do
``compose-board.innerHTML = ''``.  That parent container holds three
static child elements declared in ``templates/index.html``:

    - #compose-root-header
    - #compose-input-target
    - #compose-sections-board

``initCompose()`` writes into those by ID.  If the parent's innerHTML
is wiped during cleanup, the IDs vanish, ``getElementById(...)`` returns
null, and the Compose panel renders blank even when the API returns
valid data.

This test:

1. Opens the e2e-harness UI at TEST_BASE_URL (port 5099 — isolated from
   the user's production VibeNode on 5050).
2. Switches into Compose view via ``localStorage`` + a navigation hint
   (the existing view-toggle script reads ``viewMode`` and shows the
   board on next render).
3. Asserts the three load-bearing children are present in the DOM.
4. Drives ``setProject(currentEncoded)`` — a same-project re-set is
   enough to walk the exact code path that wiped the panel in
   production before the 2026-04-14 fix.
5. Re-asserts the three children remain present.

The harness on TEST_PORT (5099) is started by ``tests/e2e/conftest.py``
during ``pytest_configure``.  It's a separate process from the user's
running VibeNode and touches nothing in ``~/.claude/`` outside its
sandboxed config.
"""

import time
import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

from tests.e2e.conftest import TEST_BASE_URL as BASE_URL


pytestmark = pytest.mark.e2e


# The three load-bearing children that initCompose() looks up by ID.
# If any of these vanishes after a project switch, the Compose panel
# silently renders blank — that's the regression we're guarding.
COMPOSE_BOARD_CHILDREN = (
    "compose-root-header",
    "compose-input-target",
    "compose-sections-board",
)


def _wait_for_app_ready(driver, timeout=20):
    """Wait for the Compose board container to be present in the DOM.

    The element is hidden by default (``display:none``) until the user
    switches into Compose view, but it exists in the static skeleton
    from page load.  Its presence is the cheapest signal that ``index.html``
    finished rendering and the JS bundles loaded without exploding.
    """
    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.ID, "compose-board"))
    )


def _switch_to_compose_view(driver):
    """Force the app into Compose view.

    The view-mode is controlled by ``localStorage.viewMode``.  The inline
    script at the top of ``templates/index.html`` reads it on load and
    shows ``#compose-board`` accordingly.  Setting it and reloading is
    the most stable way to enter Compose view from a clean session
    without depending on a particular sidebar button's selector.
    """
    driver.execute_script("localStorage.setItem('viewMode', 'compose');")
    driver.execute_script(
        # Reveal the board synchronously even if no in-app navigation fires.
        # The inline script in templates/index.html does this on load when
        # viewMode==='compose', but to be robust across page lifecycle we
        # also poke the element directly.
        "var b = document.getElementById('compose-board');"
        "if (b) b.style.display = '';"
    )
    driver.refresh()
    _wait_for_app_ready(driver)
    # Re-assert visibility post-reload (the inline script should have
    # already done this, but we're not testing that script — we're
    # testing the DOM children).
    driver.execute_script(
        "var b = document.getElementById('compose-board');"
        "if (b) b.style.display = '';"
    )


def _assert_compose_skeleton_intact(driver, when):
    """Assert all three load-bearing children exist under #compose-board.

    ``when`` is a short context string ('before switch' / 'after switch')
    surfaced in the failure message to make the regression direction
    obvious in CI output.
    """
    board = driver.find_element(By.ID, "compose-board")
    assert board is not None, f"#compose-board missing {when}"
    missing = []
    for child_id in COMPOSE_BOARD_CHILDREN:
        children = board.find_elements(By.ID, child_id)
        if not children:
            missing.append(child_id)
    assert not missing, (
        f"CLAUDE.md Compose fix #4 regression ({when}): "
        f"#compose-board lost its static children {missing}. "
        f"The cleanup code in static/js/app.js likely reverted to "
        f"`compose-board.innerHTML = ''` — only #compose-sections-board "
        f"may be cleared.  initCompose() will now silently write to null "
        f"and the panel will render blank in production."
    )


def _current_active_project(driver):
    """Return the active project's encoded id, or None if unset."""
    return driver.execute_script(
        "return localStorage.getItem('activeProject');"
    )


def _drive_same_project_reset(driver):
    """Re-run setProject() with the current project's encoded id.

    A same-project re-set walks the exact cleanup branch in
    ``static/js/app.js`` (around line 231, ``if (viewMode === 'compose')``)
    that wipes Compose state before re-rendering.  That branch is what
    used to call ``compose-board.innerHTML = ''`` and blank the panel.
    Triggering it without needing two projects on disk is what makes
    this smoke test reliable in a fresh harness.
    """
    encoded = _current_active_project(driver)
    if not encoded:
        # The harness boots without an active project — pick the first
        # one out of the API and use it.  This is the same shape the UI
        # itself uses on cold boot.
        encoded = driver.execute_script("""
            var done = arguments[arguments.length - 1];
            fetch('/api/projects').then(function(r){ return r.json(); })
                .then(function(j){
                    var p = (j && j.projects && j.projects[0]) || null;
                    done(p ? p.encoded : null);
                })
                .catch(function(){ done(null); });
        """)
    if not encoded:
        pytest.skip("no projects available in the test harness to switch to")
    # Call setProject with reload=false so we exercise the cleanup
    # branch without losing the page (which would discard the DOM we
    # need to inspect anyway).
    driver.execute_async_script("""
        var encoded = arguments[0];
        var done = arguments[arguments.length - 1];
        try {
            // setProject is async; await it so the cleanup branch finishes
            // before we re-assert.
            var p = setProject(encoded, false);
            if (p && typeof p.then === 'function') {
                p.then(function(){ done(true); }, function(){ done(false); });
            } else {
                done(true);
            }
        } catch (e) {
            done(String(e));
        }
    """, encoded)
    # Tiny settle for any microtasks scheduled by setProject().
    time.sleep(0.2)


def test_compose_board_dom_skeleton_survives_project_switch(driver):
    """The three static children of #compose-board must remain present
    after a project-switch cleanup pass.

    Catches the exact regression fixed on 2026-04-14: the cleanup code
    reverting to ``compose-board.innerHTML = ''`` and silently blanking
    the panel.
    """
    driver.get(BASE_URL)
    _wait_for_app_ready(driver)

    _switch_to_compose_view(driver)
    _assert_compose_skeleton_intact(driver, when="before project switch")

    _drive_same_project_reset(driver)
    _assert_compose_skeleton_intact(driver, when="after project switch")
