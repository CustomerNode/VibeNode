"""
ChatGPT bridge — drives a logged-in ChatGPT web session via Playwright.

This lets VibeNode reuse your existing ChatGPT subscription (no API key /
per-token billing) by automating the chatgpt.com web UI in a Chromium browser.

Why it works the way it does (learned the hard way, 2026-06-14)
--------------------------------------------------------------
ChatGPT fronts every request with Cloudflare bot detection. Empirically:

  * Bundled Playwright Chromium  -> BLOCKED (stuck on Cloudflare "Verifying").
  * Real Chrome, headless        -> BLOCKED (headless is the tell).
  * Real Chrome, HEADED, with --disable-blink-features=AutomationControlled
                                 -> PASSES cleanly.

So the bridge uses your real installed Chrome (``channel="chrome"``), runs
**headed** (a visible window — there is no working hidden mode against
Cloudflare), and sets the anti-automation blink flag.  A persistent profile at
``data/chatgpt-profile/`` (gitignored) keeps you logged in between calls.

Login detection reads the auth COOKIE, not the DOM.  The logged-out landing
page shows a chat composer too, so "composer visible" is NOT a login signal.
Reading cookies needs no navigation, so it never trips Cloudflare and is fast.

Concurrency
-----------
A persistent profile dir can be opened by only one browser at a time.
``_PROFILE_LOCK`` serializes every launch in this process.  The login window
holds the lock for its whole lifetime; ``ask``/``status`` block with a timeout
and report "busy" rather than crashing on the profile lock.

FRAGILITY WARNING
-----------------
This automates ChatGPT's web DOM, which OpenAI changes without notice.  The CSS
selectors are centralized in the ``SEL_*`` constants for easy updating.  On any
failure during :func:`ask` a screenshot + HTML snapshot is written to
``data/chatgpt-debug/`` to make re-discovering current selectors fast.
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

# Real Chrome passes Cloudflare; bundled Chromium does not.  We try this channel
# first and fall back to bundled Chromium only if Chrome isn't installed.
_PREFERRED_CHANNEL = "chrome"

# --- Selectors (UPDATE HERE if ChatGPT changes its DOM) --------------------
# The composer is a contenteditable ProseMirror div, not a real <textarea>.
SEL_COMPOSER = "#prompt-textarea"
SEL_SEND_BUTTON = 'button[data-testid="send-button"]'
SEL_STOP_BUTTON = 'button[data-testid="stop-button"]'
SEL_ASSISTANT_MSG = 'div[data-message-author-role="assistant"]'
# A Cloudflare interstitial is present if any of these match.
SEL_CLOUDFLARE_HINTS = ["text=Verifying", "text=Just a moment", "#challenge-running"]

# --- Timeouts (ms unless noted) --------------------------------------------
_NAV_TIMEOUT = 40_000
_COMPOSER_TIMEOUT = 25_000      # wait for composer after Cloudflare clears
_CF_CLEAR_TIMEOUT = 30          # seconds to let Cloudflare auto-clear (headed)
_GEN_START_TIMEOUT = 20_000     # wait for generation to start (stop btn appears)
_GEN_FINISH_TIMEOUT = 180_000   # wait for generation to finish (stop btn gone)
_LOGIN_WAIT_SECONDS = 600       # max lifetime of the login window
_LOCK_ACQUIRE_TIMEOUT = 300     # seconds ask() will wait for the profile lock

_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
]

# Serializes all access to the persistent profile across threads.
_PROFILE_LOCK = threading.Lock()
# True while a headed login window is open (so status/ask can report nicely).
_login_active = threading.Event()


def _ensure_dirs() -> None:
    _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    _DEBUG_DIR.mkdir(parents=True, exist_ok=True)


def _launch_context(p, headless: bool):
    """Open the persistent context with real Chrome, falling back to bundled
    Chromium.  Caller must hold ``_PROFILE_LOCK``."""
    _ensure_dirs()
    common = dict(
        user_data_dir=str(_PROFILE_DIR),
        headless=headless,
        args=_LAUNCH_ARGS,
        viewport={"width": 1280, "height": 900},
    )
    try:
        return p.chromium.launch_persistent_context(channel=_PREFERRED_CHANNEL, **common)
    except Exception as e:
        logger.warning("chatgpt: real Chrome unavailable (%s); using bundled Chromium "
                       "(Cloudflare may block it)", e)
        return p.chromium.launch_persistent_context(**common)


def _has_session_cookie(ctx) -> bool:
    """Logged in iff a ChatGPT auth session-token cookie is present.
    Reads from the profile without navigating, so Cloudflare is never involved."""
    try:
        for c in ctx.cookies("https://chatgpt.com"):
            if "session-token" in c.get("name", ""):
                return True
    except Exception as e:
        logger.warning("chatgpt: cookie read failed: %s", e)
    return False


def _cloudflare_present(page) -> bool:
    if "__cf_chl" in (page.url or ""):
        return True
    for sel in SEL_CLOUDFLARE_HINTS:
        try:
            if page.locator(sel).count() > 0:
                return True
        except Exception:
            continue
    return False


def _wait_cloudflare_clear(page) -> bool:
    """Poll until the Cloudflare interstitial clears.  Returns True if cleared."""
    deadline = time.monotonic() + _CF_CLEAR_TIMEOUT
    while time.monotonic() < deadline:
        if not _cloudflare_present(page):
            return True
        time.sleep(1.0)
    return not _cloudflare_present(page)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def status() -> dict:
    """Check login state by reading the auth cookie (no navigation, headless).

    Returns ``{"ok", "logged_in", "busy", "error"}``.
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
                return {"ok": True, "logged_in": _has_session_cookie(ctx),
                        "busy": False, "error": None}
            finally:
                ctx.close()
    except Exception as e:
        logger.warning("chatgpt status check failed: %s", e)
        return {"ok": False, "logged_in": False, "busy": False, "error": str(e)}
    finally:
        _PROFILE_LOCK.release()


def open_login() -> dict:
    """Open a visible Chrome window for manual ChatGPT login.

    Returns immediately.  A daemon thread holds the window open until you close
    it yourself (or ``_LOGIN_WAIT_SECONDS`` elapses).  Closing the window flushes
    cookies to the profile.  While open, ``ask``/``status`` report ``busy``.
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
                    # Stay open until the user closes every window, or until we
                    # detect a saved session cookie, or the cap elapses.
                    #
                    # Robustness notes (learned 2026-06-14):
                    #  * "Login with Google" opens a POPUP — a second page in
                    #    ctx.pages.  We must keep running while ANY page is open,
                    #    not just pages[0], or we tear the popup down mid-OAuth.
                    #  * During the OAuth redirect chain the page navigates
                    #    constantly; calls like title() throw transient errors.
                    #    We use is_closed() (no navigation, never throws on a live
                    #    page) and only exit when all pages report closed.
                    deadline = time.monotonic() + _LOGIN_WAIT_SECONDS
                    while time.monotonic() < deadline:
                        try:
                            open_pages = [pg for pg in ctx.pages if not pg.is_closed()]
                        except Exception:
                            open_pages = []
                        if not open_pages:
                            break  # user closed the window(s) — done
                        if _has_session_cookie(ctx):
                            # Logged in.  Give cookies a beat to flush, then keep
                            # the window open so the user can confirm/close it.
                            time.sleep(2)
                            break
                        time.sleep(1.0)
                finally:
                    try:
                        ctx.close()
                    except Exception:
                        pass
        except Exception as e:
            logger.warning("chatgpt login window error: %s", e)
        finally:
            _login_active.clear()
            _PROFILE_LOCK.release()

    threading.Thread(target=_worker, name="chatgpt-login", daemon=True).start()
    return {
        "ok": True,
        "message": "A Chrome window is opening. Log in to ChatGPT, then CLOSE "
                   "that window to save your login.",
    }


def ask(prompt: str, headless: bool = False) -> dict:
    """Send ``prompt`` to ChatGPT in a fresh chat and return the reply text.

    Runs HEADED (visible) — Cloudflare blocks headless, so ``headless`` is
    accepted for API compatibility but ignored.  Returns
    ``{"ok", "result", "error"}``.  Each call uses a brand-new chat.
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
            # Headed always: Cloudflare blocks headless regardless of channel.
            ctx = _launch_context(p, headless=False)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                if not _has_session_cookie(ctx):
                    return {"ok": False, "result": None,
                            "error": "Not logged in to ChatGPT. Click "
                                     "'Log in to ChatGPT' first."}

                page.goto(CHATGPT_URL, timeout=_NAV_TIMEOUT,
                          wait_until="domcontentloaded")
                if not _wait_cloudflare_clear(page):
                    _dump_debug(page, "cloudflare-blocked")
                    return {"ok": False, "result": None,
                            "error": "Cloudflare blocked the request. Try again, "
                                     "or re-run the login to refresh clearance."}

                page.wait_for_selector(SEL_COMPOSER, timeout=_COMPOSER_TIMEOUT)

                # Type the prompt.  insert_text avoids accidental newline-sends
                # and is far faster than per-key typing.
                composer = page.locator(SEL_COMPOSER).first
                composer.click()
                page.keyboard.insert_text(prompt)

                before = page.locator(SEL_ASSISTANT_MSG).count()

                try:
                    page.locator(SEL_SEND_BUTTON).first.click(timeout=5_000)
                except Exception:
                    page.keyboard.press("Enter")

                # Track generation via the stop button (appears, then disappears).
                try:
                    page.wait_for_selector(SEL_STOP_BUTTON, timeout=_GEN_START_TIMEOUT)
                    page.wait_for_selector(SEL_STOP_BUTTON, state="hidden",
                                           timeout=_GEN_FINISH_TIMEOUT)
                except Exception:
                    pass

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
                try:
                    ctx.close()
                except Exception:
                    pass
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
