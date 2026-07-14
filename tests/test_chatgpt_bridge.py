"""Safe-failure tests for app.chatgpt_bridge.

The browser automation itself can't be unit-tested without launching real
Chrome (Cloudflare blocks headless), and we deliberately do NOT attempt it
here.  What we CAN pin — and what the hardening goal "the browser bridge
must fail safely" demands — is the contract that every public entry point
returns a structured ``{"ok": False, "error": ...}`` dict instead of raising,
and that all input validation / busy-state guards fire BEFORE any Playwright
call is made.

Every test below stops at a guard that runs before ``sync_playwright`` is
imported, so none of them touch a browser.  The pure helpers
(``_has_session_cookie``, ``_cloudflare_present``, ``_wait_cloudflare_clear``)
are exercised with fake page/context objects.
"""

import threading

import pytest

from app import chatgpt_bridge as cg


# ---------------------------------------------------------------------------
# Global-state isolation
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_module_state(monkeypatch):
    """Give every test a clean profile lock and a cleared login flag.

    ``_PROFILE_LOCK`` and ``_login_active`` are module globals shared across
    the whole process.  A test that leaves either set would corrupt the next
    one, so we swap in fresh instances per test (monkeypatch restores them).
    """
    monkeypatch.setattr(cg, "_PROFILE_LOCK", threading.Lock())
    monkeypatch.setattr(cg, "_login_active", threading.Event())
    yield


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeLocator:
    def __init__(self, count=0):
        self._count = count

    def count(self):
        return self._count


class FakePage:
    """Minimal page stub for the Cloudflare helpers."""

    def __init__(self, url="https://chatgpt.com/", hint_counts=None):
        self.url = url
        # Map selector → count for locator() lookups.
        self._hits = hint_counts or {}

    def locator(self, sel):
        return FakeLocator(self._hits.get(sel, 0))


class FakeContext:
    """Stub for a Playwright browser context — only cookies() is used."""

    def __init__(self, cookies=None, raises=False):
        self._cookies = cookies or []
        self._raises = raises

    def cookies(self, url):
        if self._raises:
            raise RuntimeError("cookie read boom")
        return self._cookies


# ---------------------------------------------------------------------------
# ask() — input validation (all return safe dicts, no browser)
# ---------------------------------------------------------------------------

class TestAskValidation:

    def test_empty_prompt(self):
        r = cg.ask("")
        assert r == {"ok": False, "result": None, "error": "Empty prompt."}

    def test_whitespace_prompt(self):
        r = cg.ask("   \n\t ")
        assert r["ok"] is False
        assert r["error"] == "Empty prompt."

    def test_too_many_files(self):
        files = [f"/no/such/f{i}.txt" for i in range(cg.MAX_FILES + 1)]
        r = cg.ask("hello", files=files)
        assert r["ok"] is False
        assert "Too many files" in r["error"]
        # The count guard must fire before the existence check.
        assert str(cg.MAX_FILES) in r["error"]

    def test_missing_files(self):
        r = cg.ask("hello", files=["/definitely/not/here.txt"])
        assert r["ok"] is False
        assert "not found" in r["error"]
        assert "here.txt" in r["error"]

    def test_existing_file_passes_validation_then_blocks_on_login(self, tmp_path):
        # A real file clears the existence guard; with a login window flagged
        # open, ask must refuse BEFORE launching a browser.
        f = tmp_path / "real.txt"
        f.write_text("data", encoding="utf-8")
        cg._login_active.set()
        r = cg.ask("hello", files=[str(f)])
        assert r["ok"] is False
        assert "login" in r["error"].lower()


class TestAskBusyGuards:

    def test_login_active_blocks(self):
        cg._login_active.set()
        r = cg.ask("hello")
        assert r["ok"] is False
        assert "login" in r["error"].lower()

    def test_profile_busy_returns_safe_dict(self, monkeypatch):
        # Hold the lock and shrink the acquire timeout so ask() reports busy
        # quickly instead of blocking for the full 300s.
        monkeypatch.setattr(cg, "_LOCK_ACQUIRE_TIMEOUT", 0.05)
        acquired = cg._PROFILE_LOCK.acquire()
        assert acquired
        try:
            r = cg.ask("hello")
        finally:
            cg._PROFILE_LOCK.release()
        assert r["ok"] is False
        assert "busy" in r["error"].lower()


# ---------------------------------------------------------------------------
# status() — busy reporting without a browser
# ---------------------------------------------------------------------------

class TestStatus:

    def test_login_active_reports_busy(self):
        cg._login_active.set()
        r = cg.status()
        assert r == {"ok": True, "logged_in": False, "busy": True, "error": None}

    def test_profile_locked_reports_busy(self, monkeypatch):
        # A fake lock that refuses acquisition exercises the lock-contention
        # branch without the real 10s wait or a Playwright launch.
        class Locked:
            def acquire(self, timeout=0):
                return False

            def release(self):
                pass

        monkeypatch.setattr(cg, "_PROFILE_LOCK", Locked())
        r = cg.status()
        assert r["ok"] is True
        assert r["busy"] is True
        assert r["logged_in"] is False


# ---------------------------------------------------------------------------
# open_login() — idempotent when already open
# ---------------------------------------------------------------------------

class TestOpenLogin:

    def test_returns_message_when_already_open(self):
        cg._login_active.set()
        r = cg.open_login()
        assert r["ok"] is True
        assert "already open" in r["message"].lower()


# ---------------------------------------------------------------------------
# _has_session_cookie
# ---------------------------------------------------------------------------

class TestSessionCookie:

    def test_detects_session_token(self):
        ctx = FakeContext(cookies=[{"name": "__Secure-next-auth.session-token"}])
        assert cg._has_session_cookie(ctx) is True

    def test_absent_when_no_auth_cookie(self):
        ctx = FakeContext(cookies=[{"name": "cf_clearance"}])
        assert cg._has_session_cookie(ctx) is False

    def test_empty_cookies(self):
        assert cg._has_session_cookie(FakeContext(cookies=[])) is False

    def test_cookie_read_error_is_safe(self):
        # A throwing context must not propagate — treated as logged out.
        assert cg._has_session_cookie(FakeContext(raises=True)) is False


# ---------------------------------------------------------------------------
# _cloudflare_present
# ---------------------------------------------------------------------------

class TestCloudflarePresent:

    def test_detected_via_challenge_url(self):
        page = FakePage(url="https://chatgpt.com/?__cf_chl_tk=abc")
        assert cg._cloudflare_present(page) is True

    def test_detected_via_hint_selector(self):
        # First hint selector matches one element.
        page = FakePage(hint_counts={cg.SEL_CLOUDFLARE_HINTS[0]: 1})
        assert cg._cloudflare_present(page) is True

    def test_absent_when_clean(self):
        page = FakePage(url="https://chatgpt.com/")
        assert cg._cloudflare_present(page) is False


# ---------------------------------------------------------------------------
# _wait_cloudflare_clear
# ---------------------------------------------------------------------------

class TestWaitCloudflareClear:

    def test_returns_true_when_already_clear(self):
        page = FakePage(url="https://chatgpt.com/")
        assert cg._wait_cloudflare_clear(page) is True

    def test_returns_false_when_blocked(self, monkeypatch):
        # Zero the clear-timeout so the poll loop exits immediately on a page
        # that never clears — no real waiting.
        monkeypatch.setattr(cg, "_CF_CLEAR_TIMEOUT", 0)
        page = FakePage(url="https://chatgpt.com/?__cf_chl_tk=stuck")
        assert cg._wait_cloudflare_clear(page) is False
