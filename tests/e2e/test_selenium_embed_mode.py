"""Selenium E2E tests for embed mode (`?embed=1&project=...`).

Embed mode exists so another application can host VibeNode in an iframe as a
sessions-only panel for one project. The riskiest part of that is not the
hiding of chrome — it is isolation. `localStorage` is shared by every tab on
the origin, so a careless embed would repoint the user's own VibeNode window to
whatever project the host app happened to be showing.

`test_embed_does_not_clobber_host_project` is the test that matters: it proves a
write to `activeProject` from inside embed mode is dropped, and that a normal
page load afterwards still sees the user's original project.

These run against the isolated e2e stack on port 5099, never the user's
instance — see tests/e2e/conftest.py.
"""

import time

import pytest
from selenium.webdriver.support.ui import WebDriverWait

from tests.e2e.conftest import TEST_BASE_URL as BASE_URL

pytestmark = pytest.mark.e2e

LONG_WAIT = 90
PINNED = "__embed_test_project__"
HOST_PROJECT = "__host_window_project__"


def _wait_for_app(driver):
    """Block until the front-end has booted far enough to assert against."""
    WebDriverWait(driver, LONG_WAIT).until(
        lambda d: d.execute_script("return document.readyState === 'complete'")
    )
    WebDriverWait(driver, LONG_WAIT).until(
        lambda d: d.execute_script("return typeof window.VN_EMBED !== 'undefined' "
                                   "|| typeof setViewMode === 'function'")
    )
    time.sleep(1.5)


class TestEmbedMode:

    def test_normal_load_is_untouched(self, driver):
        """Without ?embed the feature must be completely inert."""
        driver.get(BASE_URL)
        _wait_for_app(driver)

        assert driver.execute_script("return typeof window.VN_EMBED") == "undefined"
        assert driver.execute_script(
            "return document.body.classList.contains('vn-embed')") is False
        # The chrome embed mode hides must still be present.
        assert driver.execute_script(
            "return !!document.getElementById('btn-project')") is True
        assert driver.execute_script(
            "return !!document.getElementById('sidebar-nav')") is True

    def test_embed_pins_project(self, driver):
        driver.get(f"{BASE_URL}/?embed=1&project={PINNED}")
        _wait_for_app(driver)

        assert driver.execute_script("return window.VN_EMBED.enabled") is True
        assert driver.execute_script("return window.VN_EMBED.project") == PINNED
        # Every existing caller reads through localStorage; they must all see
        # the pinned value without being modified.
        assert driver.execute_script(
            "return localStorage.getItem('activeProject')") == PINNED

    def test_embed_accepts_a_plain_path(self, driver):
        """A host app should not have to know VibeNode's encoding scheme."""
        driver.get(f"{BASE_URL}/?embed=1&project=C:/Users/tester/code/Thing")
        _wait_for_app(driver)
        assert driver.execute_script(
            "return window.VN_EMBED.project") == "C--Users-tester-code-Thing"

    def test_embed_encoding_matches_encode_cwd(self, driver):
        """Underscores and dots must become dashes, exactly like _encode_cwd().

        This is the rule that is easy to miss and fails silently: a project
        containing "_" or "." would encode differently from the directory Claude
        Code actually created, so the pinned project would match nothing and the
        panel would render empty with no error anywhere.
        """
        from app.config import _encode_cwd

        for path in (
            r"C:\Users\tester\code\customerNode_root",
            r"C:\Users\tester\.claude\_system",
            r"C:\Users\tester\code\my.app_v2",
        ):
            driver.get(f"{BASE_URL}/?embed=1&project={path}")
            _wait_for_app(driver)
            js_encoded = driver.execute_script("return window.VN_EMBED.project")
            assert js_encoded == _encode_cwd(path), (
                f"embed.js encoded {path!r} as {js_encoded!r}, "
                f"but _encode_cwd gives {_encode_cwd(path)!r}"
            )

    def test_embed_hides_navigation_chrome(self, driver):
        driver.get(f"{BASE_URL}/?embed=1&project={PINNED}")
        _wait_for_app(driver)

        assert driver.execute_script(
            "return document.body.classList.contains('vn-embed')") is True

        for element_id in ("btn-project", "sidebar-nav",
                           "btn-git-update", "btn-git-publish", "btn-git-sync"):
            hidden = driver.execute_script(
                "var el = document.getElementById(arguments[0]);"
                "if (!el) return true;"                      # absent is fine
                "return getComputedStyle(el).display === 'none';",
                element_id,
            )
            assert hidden, f"{element_id} should be hidden in embed mode"

    def test_chrome_param_restores_navigation(self, driver):
        """?chrome=1 is the escape hatch for debugging an embed."""
        driver.get(f"{BASE_URL}/?embed=1&project={PINNED}&chrome=1")
        _wait_for_app(driver)

        visible = driver.execute_script(
            "var el = document.getElementById('btn-project');"
            "return !!el && getComputedStyle(el).display !== 'none';"
        )
        assert visible, "?chrome=1 should keep the project switcher visible"

    def test_view_is_locked(self, driver):
        """Calling setViewMode with anything else must resolve back to sessions."""
        driver.get(f"{BASE_URL}/?embed=1&project={PINNED}")
        _wait_for_app(driver)

        driver.execute_script("setViewMode('kanban');")
        time.sleep(1.5)
        assert driver.execute_script("return viewMode") == "sessions"

        driver.execute_script("setViewMode('compose');")
        time.sleep(1.5)
        assert driver.execute_script("return viewMode") == "sessions"

    def test_project_overlay_is_disabled(self, driver):
        driver.get(f"{BASE_URL}/?embed=1&project={PINNED}")
        _wait_for_app(driver)

        driver.execute_script("openProjectOverlay();")
        time.sleep(1)
        shown = driver.execute_script(
            "var el = document.getElementById('project-overlay');"
            "return !!el && getComputedStyle(el).display !== 'none' "
            "&& el.classList.contains('show');"
        )
        assert not shown, "the project overlay must not open in embed mode"

    def test_embed_does_not_clobber_host_project(self, driver):
        """The isolation guarantee, and the whole reason for the shim.

        localStorage is shared across tabs on an origin. If an embed persisted
        its pinned project, the user's own VibeNode window would silently jump
        to whatever the host app was showing.
        """
        # Arrange: a normal window picks a project.
        driver.get(BASE_URL)
        _wait_for_app(driver)
        driver.execute_script(
            "localStorage.setItem('activeProject', arguments[0]);", HOST_PROJECT)
        assert driver.execute_script(
            "return localStorage.getItem('activeProject')") == HOST_PROJECT

        # Act: load embed mode pinned elsewhere, and have it try to persist.
        driver.get(f"{BASE_URL}/?embed=1&project={PINNED}")
        _wait_for_app(driver)
        assert driver.execute_script(
            "return localStorage.getItem('activeProject')") == PINNED
        driver.execute_script(
            "localStorage.setItem('activeProject', 'something-else');")
        driver.execute_script("setViewMode('kanban');")   # would persist a view
        time.sleep(1)

        # Assert: back in a normal window, the user's project survived intact.
        driver.get(BASE_URL)
        _wait_for_app(driver)
        assert driver.execute_script(
            "return localStorage.getItem('activeProject')") == HOST_PROJECT, (
            "embed mode leaked its project into the host window's localStorage")

    def test_unrelated_storage_still_writes(self, driver):
        """The shim must only intercept host-owned keys, not all of storage."""
        driver.get(f"{BASE_URL}/?embed=1&project={PINNED}")
        _wait_for_app(driver)

        driver.execute_script("localStorage.setItem('__embed_probe__', 'kept');")
        assert driver.execute_script(
            "return localStorage.getItem('__embed_probe__')") == "kept"
        driver.execute_script("localStorage.removeItem('__embed_probe__');")
