"""System → Settings → "Recovery & Watchdog" (added 2026-10-23).

A read-only list of the ``VIBENODE_*`` switches from ``GET /api/engine/knobs``
(app/engine_knobs.py), grouped by ``group``: the name in monospace, the
current value, a "set" badge when it comes from the environment, the
description; internal knobs collapsed under "Internal".  Against the real page
(``tests/_fake_app.py``) with the route faked.
"""

from __future__ import annotations

import pytest

from tests._browser import shared_browser_scope
from tests._fake_app import FakeApp, fake_sessions

XDIST_SPLITTABLE = True

KNOBS = {"ok": True, "knobs": [
    {"name": "VIBENODE_STALL_MINUTES", "group": "Stall watchdog", "description": "Minutes of zero output.",
     "default": "10", "value": "10", "source": "default", "internal": False},
    {"name": "VIBENODE_ORPHAN_RECOVERY", "group": "Stranded workers (job 4)", "description": "Set to 0 to stop.",
     "default": "1", "value": "0", "source": "env", "internal": False},
    {"name": "VIBENODE_KEEP_AWAKE", "group": "Stall watchdog", "description": "Let Windows sleep.",
     "default": "1", "value": "1", "source": "default", "internal": False},
    {"name": "_VIBENODE_DAEMON_PATH_AUGMENTED", "group": "Launcher", "description": "Set by the launcher.",
     "default": "", "value": "", "source": "default", "internal": True},
]}


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


@pytest.mark.slow
def test_recovery_watchdog_lists_knobs_read_only(browser):
    app = FakeApp(browser, fake_sessions(3), extra_routes={"/api/engine/knobs": lambda req: (200, KNOBS)})
    try:
        # It is a section of the Settings window (built from the System dropdown).
        labels = app.eval("() => _hdrSysBuildGroups().flat().map(r => r.label)")
        assert "Recovery & Watchdog" in labels
        app.eval("() => openSettings('Recovery & Watchdog')")
        app.page.wait_for_function("document.querySelectorAll('#rw-list .rw-row').length === 4", timeout=5000)
        assert app.eval("() => document.querySelector('#pm-overlay .pm-title').textContent") == "Recovery & Watchdog"
        assert app.eval("() => document.querySelector('#pm-overlay .rw-intro').textContent.trim()") == (
            "These are set with environment variables before VibeNode starts. "
            "Changing one needs a restart of the part that reads it.")
        # Grouped by `group`, in the server's order; the internal knob is in a collapsed <details>.
        groups = app.eval("() => Array.from(document.querySelectorAll('#rw-list > .rw-group:not(.rw-internal) > .rw-group-label')).map(e => e.textContent)")
        assert groups == ["Stall watchdog", "Stranded workers (job 4)"]
        stall = app.eval("() => Array.from(document.querySelectorAll('#rw-list > .rw-group')[0].querySelectorAll('.rw-name')).map(e => e.textContent)")
        assert stall == ["VIBENODE_STALL_MINUTES", "VIBENODE_KEEP_AWAKE"]
        assert app.eval("() => !document.querySelector('#rw-list details.rw-internal').open")
        assert app.eval("() => document.querySelector('#rw-list details.rw-internal .rw-name').textContent") == \
            "_VIBENODE_DAEMON_PATH_AUGMENTED"
        # Monospace name, the value, and "set" only on the env-sourced one.
        assert "mono" in app.eval("() => getComputedStyle(document.querySelector('.rw-name')).fontFamily").lower() or \
            app.eval("() => document.querySelector('.rw-name').tagName") == "CODE"
        row = "document.querySelector('.rw-row[data-knob=\"VIBENODE_ORPHAN_RECOVERY\"]')"
        assert app.eval(f"() => {row}.querySelector('.rw-value').textContent") == "0"
        assert app.eval(f"() => !!{row}.querySelector('.rw-set')")
        assert app.eval("() => document.querySelectorAll('#rw-list .rw-set').length") == 1
        assert app.eval(f"() => {row}.querySelector('.rw-desc').textContent") == "Set to 0 to stop."
        # Read-only: nothing to type into or toggle.
        assert app.eval("() => document.querySelectorAll('#rw-list input, #rw-list select, #rw-list textarea').length") == 0
        assert not app.errors, app.errors
    finally:
        app.close()


@pytest.mark.slow
def test_recovery_watchdog_reports_a_failed_read(browser):
    app = FakeApp(browser, fake_sessions(3), extra_routes={"/api/engine/knobs": lambda req: (500, {"ok": False})})
    try:
        app.eval("() => openRecoveryWatchdog()")
        app.page.wait_for_function("document.querySelector('#rw-list .rw-empty') && "
                                   "document.querySelector('#rw-list .rw-empty').textContent === 'Could not read the settings.'",
                                   timeout=5000)
    finally:
        app.close()
