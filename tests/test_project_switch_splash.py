"""The project-switch splash must be opaque and must outlast the load.

Switching projects tears the old project down and builds the new one in
several steps: an empty dashboard, sessions known from the live snapshot before
the list arrives, the list, then the restored session's thread.  The old loader
was a translucent blur lifted on a 250ms timer, so each of those showed through
as stale data (2026-10-06).  The splash is now the opaque ``.vn-splash`` and is
held until the session list has loaded.

Source-level guards: the two regressions below are both silent in the UI.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = APP_JS.index(f"function {name}(")
    i = APP_JS.index("{", start)
    depth = 0
    while True:
        depth += {"{": 1, "}": -1}.get(APP_JS[i], 0)
        if depth == 0:
            return APP_JS[start:i + 1]
        i += 1


def test_no_stylesheet_styles_the_loader_by_id():
    """An ``#project-switch-loader`` rule outranks the splash's class rules.

    The old loader's leftover id rules (``opacity: 0``, a transparent
    background) made the new splash completely invisible while every class and
    timer looked correct.
    """
    for css in list((ROOT / "static").glob("*.css")) + list((ROOT / "static" / "css").glob("*.css")):
        assert "#project-switch-loader" not in css.read_text(encoding="utf-8"), (
            f"{css.name} styles #project-switch-loader by id; that overrides "
            f".vn-splash.vn-switch and hides the splash"
        )


def test_loader_is_the_shared_opaque_splash():
    body = _function("_showProjectSwitchLoader")
    assert "vn-splash vn-switch" in body
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    assert ".vn-splash.vn-switch" in html, "switch splash styles left index.html"


def test_splash_is_held_until_the_session_list_has_loaded():
    body = _function("selectProjectFromOverlay")
    show = body.index("_showProjectSwitchLoader(")
    switch = body.index("await setProject(")
    load = body.index("await _projectSwitchLoad")
    settled = body.index("await _projectSwitchSettled(")
    hide = body.index("_hideProjectSwitchLoader(")
    assert show < switch < load < settled < hide, (
        "the splash must go up before the switch and come down only after the "
        "session list load and the settle check"
    )
    # setProject must hand the load promise over rather than drop it.
    assert re.search(r"_projectSwitchLoad\s*=\s*loadSessions\(\)", _function("setProject"))


def test_dashboard_is_rebuilt_after_the_old_project_is_cleared():
    """Built before the switch it showed the OLD project's name and counts."""
    body = _function("setProject")
    assert body.index("localStorage.setItem('activeProject', encoded)") < body.index("_buildDashboard()")
    assert body.index("allSessions = [];") < body.index("_buildDashboard()")
