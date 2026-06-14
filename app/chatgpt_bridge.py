"""
ChatGPT bridge — drives a logged-in ChatGPT web session via Playwright.

This lets VibeNode reuse your existing ChatGPT subscription (no API key /
per-token billing) by automating the chatgpt.com web UI in a Chromium browser.

How it works
------------
A *persistent* browser profile is kept on disk at ``data/chatgpt-profile/``
(gitignored, like the launcher's ``data/chrome-profile/``).  You log in to
ChatGPT once via :func:`open_login` (a visible window) and the cookies persist,
so subsequent :func:`ask` calls run headless and reuse that login.

Concurrency
-----------
A persistent profile directory can only be opened by one Chromium instance at a
time.  ``_PROFILE_LOCK`` serializes every browser launch in this process.  The
login window holds the lock for its whole (background) lifetime; ``ask`` and
``status`` block on it with a timeout and report "busy" rather than crashing on
the profile lock.

FRAGILITY WARNING
-----------------
This automates ChatGPT's web DOM, which OpenAI changes without notice, and they
run bot detection that can block a scripted browser.  The CSS selectors below
are the single most likely thing to break.  They are centralized in the
``SEL_*`` constants so they're trivial to update.  On any failure during
:func:`ask` a screenshot + HTML snapshot is written to ``data/chatgpt-debug/``
to make re-discovering the current selectors fast.
"""

import logging
import threading
import time
from pathlib import Path

from .config import _VIBENODE_DIR

logger = logging.getLogger(__name__)

# --- Paths -----------------------------------------------------------------
_PROFILE_DIR = _VIBENODE_DIR / "data" / "chatgpt-profile"
_DEBUG_DIR = _VIBENODE_DIR / "data" / "chatgpt-debug"
CHATGPT_URL = "https://chatgpt.com/"

# --- Selectors (UPDATE HERE if ChatGPT changes its DOM) --------------------
# The composer is a contenteditable ProseMirror div, not a real <textarea>.
SEL_COMPOSER = "#prompt-textarea"
SEL_SEND_BUTTON = 'button[data-testid="send-button"]'
SEL_STOP_BUTTON = 'button[data-testid="stop-button"]'
SEL_ASSISTANT_MSG = 'div[data-message-author-role="assistant"]'
# Heuristics for the logged-out state (any one present => not logged in).
SEL_LOGIN_HINTS = [
    'button[data-testid="login-button"]',
    'a[href*="auth/login"]',
    'button:has-text("Log in")',
]

# --- Timeouts (ms unless noted) --------------------------------------------
_NAV_TIMEOUT = 30_000
_COMPOSER_TIMEOUT = 20_000      # how long to wait for the composer after nav
_GEN_START_TIMEOUT = 20_000     # wait for generation to *start* (stop btn appears)
_GEN_FINISH_TIMEOUT = 180_000   # wait for generation to *finish* (stop btn gone)
_LOGIN_WAIT_SECONDS = 300       # how long the login window stays open
_LOCK_ACQUIRE_TIMEOUT = 150     # seconds ask() will wait for the profile lock

# Serializes all access to the persistent profile across threads.
_PROFILE_LOCK = threading.Lock()
# True while a headed login window is open (so status/ask can report nicely).
_login_active = threading.Event()


def _ensure_dirs() -> None:
    _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    _DEBUG_DIR.mkdir(parents=True, exist_ok=True)


def _is_logged_out(page) -> bool:
    """Best-effort: are we sitting on a login/landing page rather than chat?"""
    for sel in SEL_LOGIN_HINTS:
        try:
            if page.locator(sel).first.is_visible(timeout=500):
                return True
        except Exception:
            continue
    return False


def _launch_context(p, headless: bool):
    """Open the persistent Chromium context.  Caller must hold _PROFILE_LOCK."""
    _ensure_dirs()
    return p.chromium.launch_persistent_context(
        user_data_dir=str(_PROFILE_DIR),
        headless=headless,
        args=["--no-first-run", "--no-default-browser-check"],
        viewport={"width": 1280, "height": 900},
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def status() -> dict:
    """Quick check of whether we're logged in to ChatGPT.

    Returns ``{"ok": bool, "logged_in": bool, "busy": bool, "error": str|None}``.
    Cheap-ish: launches a short-lived headless context.  Returns ``busy`` if a
    login window currently holds the profile.
    """
    if _login_active.is_set():
        return {"ok": True, "logged_in": False, "busy": True, "error": None}
    if not _PROFILE_LOCK.acquire(timeout=10):
        return {"ok": True, "logged_in": False, "busy": True, "error": None}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            ctx = _launch_context(p, headless=True)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(CHATGPT_URL, timeout=_NAV_TIMEOUT, wait_until="domcontentloaded")
                # Logged in if the composer shows up; otherwise treat as out.
                try:
                    page.wait_for_selector(SEL_COMPOSER, timeout=8_000)
                    logged_in = True
                except Exception:
                    logged_in = not _is_logged_out(page) and False
                return {"ok": True, "logged_in": logged_in, "busy": False, "error": None}
            finally:
                ctx.close()
    except Exception as e:
        logger.warning("chatgpt status check failed: %s", e)
        return {"ok": False, "logged_in": False, "busy": False, "error": str(e)}
    finally:
        _PROFILE_LOCK.release()


def open_login() -> dict:
    """Open a *visible* ChatGPT window in the background for manual login.

    Returns immediately.  A daemon thread holds the profile open (headed) for up
    to ``_LOGIN_WAIT_SECONDS``, auto-closing once you're logged in so the cookies
    are flushed to disk.  While it's open, ``ask``/``status`` report ``busy``.
    """
    if _login_active.is_set():
        return {"ok": True, "message": "Login window is already open."}

    def _worker():
        if not _PROFILE_LOCK.acquire(timeout=5):
            logger.warning("chatgpt login: profile busy, aborting")
            return
        _login_active.set()
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                ctx = _launch_context(p, headless=False)
                try:
                    page = ctx.pages[0] if ctx.pages else ctx.new_page()
                    page.goto(CHATGPT_URL, timeout=_NAV_TIMEOUT,
                              wait_until="domcontentloaded")
                    deadline = time.monotonic() + _LOGIN_WAIT_SECONDS
                    while time.monotonic() < deadline:
                        try:
                            if page.locator(SEL_COMPOSER).first.is_visible(timeout=1000):
                                # Logged in — give cookies a moment to persist.
                                time.sleep(2)
                                break
                        except Exception:
                            pass
                        time.sleep(1.5)
                finally:
                    ctx.close()
        except Exception as e:
            logger.warning("chatgpt login window error: %s", e)
        finally:
            _login_active.clear()
            _PROFILE_LOCK.release()

    threading.Thread(target=_worker, name="chatgpt-login", daemon=True).start()
    return {
        "ok": True,
        "message": "A Chrome window is opening. Log in to ChatGPT; it closes "
                   "automatically once you're in.",
    }


def ask(prompt: str, headless: bool = True) -> dict:
    """Send ``prompt`` to ChatGPT in a fresh chat and return the reply text.

    Returns ``{"ok": bool, "result": str|None, "error": str|None}``.
    Each call uses a brand-new chat (no cross-prompt context bleed).
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return {"ok": False, "result": None, "error": "Empty prompt."}
    if _login_active.is_set():
        return {"ok": False, "result": None,
                "error": "A login window is open — finish logging in first."}
    if not _PROFILE_LOCK.acquire(timeout=_LOCK_ACQUIRE_TIMEOUT):
        return {"ok": False, "result": None,
                "error": "ChatGPT browser is busy with another request."}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            ctx = _launch_context(p, headless=headless)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.goto(CHATGPT_URL, timeout=_NAV_TIMEOUT,
                          wait_until="domcontentloaded")
                # Confirm we're logged in.
                try:
                    page.wait_for_selector(SEL_COMPOSER, timeout=_COMPOSER_TIMEOUT)
                except Exception:
                    if _is_logged_out(page):
                        return {"ok": False, "result": None,
                                "error": "Not logged in to ChatGPT. Click "
                                         "'Log in to ChatGPT' first."}
                    raise

                # Type the prompt.  insert_text avoids accidentally sending on
                # newlines and is far faster than per-key typing.
                composer = page.locator(SEL_COMPOSER).first
                composer.click()
                page.keyboard.insert_text(prompt)

                # How many assistant turns existed before we sent (should be 0
                # in a fresh chat, but be defensive).
                before = page.locator(SEL_ASSISTANT_MSG).count()

                # Send.
                try:
                    page.locator(SEL_SEND_BUTTON).first.click(timeout=5_000)
                except Exception:
                    page.keyboard.press("Enter")

                # Wait for generation to start then finish, tracked via the
                # stop button.  If it never appears, the reply may have been
                # instant — fall through to the message-count wait.
                try:
                    page.wait_for_selector(SEL_STOP_BUTTON, timeout=_GEN_START_TIMEOUT)
                    page.wait_for_selector(SEL_STOP_BUTTON, state="hidden",
                                           timeout=_GEN_FINISH_TIMEOUT)
                except Exception:
                    pass

                # Wait until a new assistant turn exists, then read it.
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if page.locator(SEL_ASSISTANT_MSG).count() > before:
                        break
                    time.sleep(0.5)

                msgs = page.locator(SEL_ASSISTANT_MSG)
                if msgs.count() == 0:
                    raise RuntimeError("No assistant reply found on the page.")
                text = msgs.last.inner_text().strip()
                if not text:
                    raise RuntimeError("Assistant reply was empty.")
                return {"ok": True, "result": text, "error": None}
            except Exception as e:
                _dump_debug(page, "ask-error")
                logger.warning("chatgpt ask failed: %s", e)
                return {"ok": False, "result": None, "error": str(e)}
            finally:
                ctx.close()
    except Exception as e:
        logger.exception("chatgpt ask launch failed")
        return {"ok": False, "result": None, "error": str(e)}
    finally:
        _PROFILE_LOCK.release()


def _dump_debug(page, tag: str) -> None:
    """Save a screenshot + HTML so selectors can be re-discovered after a break."""
    try:
        _ensure_dirs()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        page.screenshot(path=str(_DEBUG_DIR / f"{tag}-{stamp}.png"), full_page=True)
        (_DEBUG_DIR / f"{tag}-{stamp}.html").write_text(
            page.content(), encoding="utf-8", errors="replace")
    except Exception:
        pass
