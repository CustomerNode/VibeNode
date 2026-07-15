"""
Mobile Visual Channel — capture + serving backend.

Turns a URL into something a phone on the tailnet can actually see:

  POST /api/preview/render      {url,name,width,height} -> headless-Chrome PNG,
                                 returns {ok,id,src,name,type:"image"}
  GET  /api/preview/asset/<id>  serve a rendered PNG (opaque id, dir-scoped)
  GET  /api/preview/thumb/<id>  downscaled JPEG of a render, for gallery tiles
                                 (falls back to the full PNG without Pillow)
  GET  /api/preview/proxy?u=..  best-effort reverse proxy so a live iframe can
                                 load a localhost dev server through the tailnet
                                 origin (relative asset URLs rewritten back
                                 through the proxy).

WHY A CAPTURE BACKEND
---------------------
The agent runs on the dev machine; the phone is remote over Tailscale. The phone
cannot reach the machine's localhost, and the agent has no other way to "show" a
screen. Server-side headless render solves the image case reliably (the phone
just fetches a PNG); the proxy is the best-effort convenience for live pages.

SELF-CONTAINED CHROME DISCOVERY
-------------------------------
This module deliberately does NOT import run.py: importing run.py executes its
module-level boot (it starts servers). We duplicate the small finder logic here
so importing this blueprint has zero side effects.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from flask import Blueprint, Response, abort, jsonify, request, send_file

bp = Blueprint("preview_api", __name__)
_log = logging.getLogger("app")

_ROOT = Path(__file__).resolve().parents[2]
_PREVIEW_DIR = _ROOT / "data" / "previews"
_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


# ---------------------------------------------------------------------------
# Chrome discovery (self-contained — see module docstring)
# ---------------------------------------------------------------------------

def _find_chrome() -> str | None:
    if sys.platform == "win32":
        try:
            import winreg
            for hive, key in [
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
                (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
            ]:
                try:
                    with winreg.OpenKey(hive, key) as k:
                        p = winreg.QueryValue(k, None)
                        if p and os.path.isfile(p):
                            return p
                except OSError:
                    pass
        except Exception:  # noqa: BLE001
            pass
        for base in (os.environ.get("PROGRAMFILES", ""), os.environ.get("PROGRAMFILES(X86)", ""),
                     os.path.expandvars(r"%LOCALAPPDATA%")):
            if base:
                p = os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
                if os.path.isfile(p):
                    return p
        return None
    if sys.platform == "darwin":
        for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  "/Applications/Chromium.app/Contents/MacOS/Chromium"):
            if os.path.isfile(p):
                return p
        return shutil.which("google-chrome") or shutil.which("chromium")
    # Linux
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    for p in ("/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
              "/snap/bin/chromium", "/opt/google/chrome/google-chrome"):
        if os.path.isfile(p):
            return p
    return None


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def _clamp(v, lo, hi, default):
    try:
        return max(lo, min(int(v), hi))
    except (TypeError, ValueError):
        return default


@bp.route("/api/preview/render", methods=["POST"])
def render():
    """Headless-render a URL to a PNG the phone can fetch."""
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    name = (data.get("name") or "Preview").strip()[:80] or "Preview"
    if not (url.startswith("http://") or url.startswith("https://") or url.startswith("data:")):
        return jsonify({"ok": False, "error": "an http(s) or data: URL is required"}), 400
    width = _clamp(data.get("width"), 200, 2000, 1024)
    height = _clamp(data.get("height"), 200, 4000, 1400)
    # How long to let the page's JS/network/animations settle BEFORE capturing.
    # Without this, headless Chrome shoots at the load event and misses anything
    # a single-page app paints afterward (the "VibeNode chrome but empty inside"
    # bug). Caller can raise it for slow/heavy pages. Default 3s; capped at 20s.
    wait_ms = _clamp(data.get("wait_ms"), 0, 20000, 3000)

    chrome = _find_chrome()
    if not chrome:
        return jsonify({"ok": False, "error": "Chrome/Chromium not found on this machine"}), 500

    _PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    pid = uuid.uuid4().hex
    out = _PREVIEW_DIR / (pid + ".png")
    cmd = [
        chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--no-first-run", "--no-default-browser-check",
        # Let the SPA actually render before the shot: advance virtual time so
        # timers/fetches/frameworks run, and force a full compositor paint.
        "--virtual-time-budget=%d" % wait_ms,
        "--run-all-compositor-stages-before-draw",
        "--force-device-scale-factor=1",
        # Opaque white base so a transparent/late-painting body isn't a blank frame.
        "--default-background-color=FFFFFFFF",
        "--screenshot=" + str(out), "--window-size=%d,%d" % (width, height), url,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=45,
                           creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "render timed out (45s)"}), 504
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)[:300]}), 500
    if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        return jsonify({"ok": False, "error": (r.stderr or "render failed")[:300]}), 500
    # `file` is the absolute path so the agent can OPEN the PNG and visually verify
    # it captured real content before emitting a preview marker (see mobile preamble).
    return jsonify({"ok": True, "id": pid, "src": "/api/preview/asset/" + pid,
                    "file": str(out), "bytes": out.stat().st_size,
                    "name": name, "type": "image"})


@bp.route("/api/preview/asset/<pid>")
def asset(pid):
    """Serve a rendered PNG. Opaque hex id, resolved only inside the previews dir."""
    if not _ID_RE.match(pid or ""):
        abort(404)
    f = _PREVIEW_DIR / (pid + ".png")
    if not f.exists():
        abort(404)
    return send_file(str(f), mimetype="image/png", max_age=0)


# ---------------------------------------------------------------------------
# Thumbnails
# ---------------------------------------------------------------------------

_THUMB_SUFFIX = ".thumb.jpg"
_THUMB_W = 480          # ~2x the widest gallery tile, so it stays sharp on a phone
_THUMB_MAX_ASPECT = 1.4  # crop taller-than-this captures (see _make_thumb)


def _make_thumb(src: Path, dst: Path) -> bool:
    """Write a downscaled JPEG of `src` to `dst`. False if we can't (caller falls back).

    Pillow is NOT in requirements.txt — it's only ever present transitively, so
    this must degrade rather than 500 when the import fails.
    """
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 — Pillow absent is an expected, handled state
        return False
    try:
        with Image.open(src) as im:
            im = im.convert("RGB")   # renders are opaque; drops any alpha for JPEG
            # A page capture is tall (up to 4000px). Squeezing a whole scroll into
            # a 110px tile is unreadable mush, so keep the top — that's the part
            # that identifies the page. The CSS crops the same way (object-fit:
            # cover + top), this just avoids shipping pixels nobody will see.
            w, h = im.size
            max_h = int(w * _THUMB_MAX_ASPECT)
            if h > max_h:
                im = im.crop((0, 0, w, max_h))
            im.thumbnail((_THUMB_W, int(_THUMB_W * _THUMB_MAX_ASPECT)), Image.LANCZOS)
            im.save(dst, "JPEG", quality=78, optimize=True)
        return True
    except Exception as e:  # noqa: BLE001
        _log.debug("preview thumb failed for %s: %s", src.name, e)
        return False


@bp.route("/api/preview/thumb/<pid>")
def thumb(pid):
    """Serve a small copy of a render, for gallery tiles and in-chat cards.

    WHY THIS EXISTS: the gallery shows real screenshots rather than a synthetic
    placeholder, and the client is a phone on a cellular tailnet. A dozen
    full-size renders (each up to 2000x4000) to paint 110px tiles is the one
    genuine cost of showing real pixels — so downscale once and cache on disk.
    Falls back to the full-size asset if we can't thumbnail: a heavy thumbnail is
    still infinitely more useful than a generic mockup.
    """
    if not _ID_RE.match(pid or ""):
        abort(404)
    src = _PREVIEW_DIR / (pid + ".png")
    if not src.exists():
        abort(404)
    dst = _PREVIEW_DIR / (pid + _THUMB_SUFFIX)
    # Re-render if missing or older than the source (↻ can overwrite a render).
    if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
        if not _make_thumb(src, dst):
            return send_file(str(src), mimetype="image/png", max_age=0)
    # Content is immutable per id, so let the phone cache it.
    return send_file(str(dst), mimetype="image/jpeg", max_age=3600)


# ---------------------------------------------------------------------------
# Proxy — best-effort live-iframe reachability for localhost dev servers
# ---------------------------------------------------------------------------

_ATTR_RE = re.compile(r"""(\b(?:src|href|action|poster)\s*=\s*)(["'])(.*?)\2""", re.IGNORECASE)


def _proxy_url(target: str) -> str:
    return "/api/preview/proxy?u=" + urllib.parse.quote(target, safe="")


def _rewrite_html(body: str, base: str) -> str:
    """Rewrite resource URLs so the phone fetches them back through the proxy
    (it cannot reach the machine's localhost directly). Best-effort: handles
    static markup; JS-injected URLs are out of scope (use render/screenshot)."""
    def repl(m):
        pre, q, val = m.group(1), m.group(2), m.group(3)
        if not val or val.startswith(("data:", "mailto:", "#", "javascript:")):
            return m.group(0)
        absolute = urllib.parse.urljoin(base, val)
        if not absolute.startswith(("http://", "https://")):
            return m.group(0)
        return pre + q + _proxy_url(absolute) + q
    return _ATTR_RE.sub(repl, body)


@bp.route("/api/preview/proxy")
def proxy():
    u = (request.args.get("u") or "").strip()
    if not (u.startswith("http://") or u.startswith("https://")):
        abort(400)
    try:
        req = urllib.request.Request(u, headers={"User-Agent": "VibeNode-Preview"})
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 (user-driven, tailnet-gated)
            ctype = resp.headers.get("Content-Type", "application/octet-stream")
            raw = resp.read()
    except Exception as e:  # noqa: BLE001
        return Response("Preview proxy could not reach %s: %s" % (u, str(e)[:160]),
                        status=502, mimetype="text/plain")
    if "text/html" in ctype.lower():
        try:
            html = raw.decode("utf-8", "replace")
            html = _rewrite_html(html, u)
            raw = html.encode("utf-8")
        except Exception:  # noqa: BLE001
            pass
    return Response(raw, mimetype=ctype.split(";")[0].strip() or "text/html")
