"""Two tiers of access to the web server: local, and paired.

Why
---
VibeNode binds to 127.0.0.1, and Mobile Command exposes it to the user's
tailnet through ``tailscale serve``. Until this module, reaching the tailnet
was the only check: every tailnet device could call every route, including
ones that run commands on this machine (install a skill pack with a
caller-supplied setup command, open a file in its desktop program, shut the
server down). Five routes checked that the caller was a local process; the
other two hundred did not.

The model
---------
* **Local.** The request comes from this machine: a loopback address, none of
  the marks a proxy hop leaves (``X-Forwarded-*``, the ``Tailscale-User-Login``
  identity header ``tailscale serve`` adds, a ``.ts.net`` Host), and, when a
  browser page sent it, an ``Origin`` that is itself loopback. That last rule
  is what keeps a local-only route safe from a page on another site running in
  the user's own browser: it reaches 127.0.0.1 too, but sends its own Origin.
  Local browsers never pair.
* **Paired.** Local, or carrying the pairing cookie. The cookie is set once
  per browser by entering the 10-character code (``/pair``), or by opening the
  one-tap link the Mobile Command modal shows. Its value is an HMAC of the
  code, so rotating the code logs every device out.

Enforcement is central. ``install(app)`` registers ONE ``before_request`` that
requires the paired tier for every page and ``/api/*`` route, with a short
allowlist (the health probes, the pairing routes, static assets). Routes that
must stay on this machine add ``@local_only``, which applies whatever the
config says. ``socket_connection_allowed()`` is the same check for the
Socket.IO handshake, plus an Origin check that the Socket.IO server also runs
through ``origin_allowed`` (its ``cors_allowed_origins`` callable).

``mobile_command_auth: false`` in kanban_config.json is the emergency switch:
the before_request gate and the socket pairing check enforce nothing, but
``@local_only`` and the Origin check still hold.

The code lives in ``data/mobile_pair.json`` (gitignored), created on first
need. See ``tests/test_mobile_auth.py``.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

from flask import current_app, jsonify, redirect, request

from .config import _VIBENODE_DIR, get_kanban_config

log = logging.getLogger("app.mobile_auth")

# ── constants ────────────────────────────────────────────────────────────────

PAIR_FILE: Path = _VIBENODE_DIR / "data" / "mobile_pair.json"
COOKIE_NAME = "vn_pair"
COOKIE_MAX_AGE_S = 365 * 24 * 3600
CODE_LENGTH = 10
# No 0/O or 1/I: the code is read off one screen and typed on another.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CONFIG_KEY = "mobile_command_auth"
RATE_LIMIT_ATTEMPTS = 5
RATE_LIMIT_WINDOW_S = 60.0
MOBILE_HOST_TTL_S = 600.0

_LOOPBACK_ADDRS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# Never need pairing: the reachability probes the UI and the phone's Start
# page poll, the pairing routes themselves, the PWA manifest (device label
# only) and static assets.
OPEN_PATHS = frozenset({"/api/ping", "/api/health", "/pair", "/api/pair",
                        "/manifest.webmanifest", "/favicon.ico"})
OPEN_PREFIXES = ("/static/", "/speechnode/")

# Handlers that enforce their own, STRICTER rule inline (a local process only,
# browser pages included in the refusal) and answer a refused caller
# themselves: 403 "local callers only", or for the approval ping a 200 that
# watches nothing. The gate leaves them alone so those answers stand.
SELF_GUARDED_PREFIXES = ("/api/hook/", "/api/notify/")
SELF_GUARDED_PATHS = frozenset({"/api/jobs/run"})

# The preview proxy marks what it forwards with X-Forwarded-For, and nothing
# else. A request to the proxy that carries ONLY that header came through a
# local proxy hop; a phone through `tailscale serve` also carries the
# Tailscale identity header and a .ts.net Host and is still gated.
XFF_LENIENT_PATHS = frozenset({"/api/preview/proxy"})

_EXT_KEY = "vn_mobile_auth"

_lock = threading.Lock()


# ── who is calling ───────────────────────────────────────────────────────────

def _proxied(ignore_xff: bool = False) -> bool:
    """True when the request shows any mark of a proxy hop."""
    h = request.headers
    if h.get("Tailscale-User-Login") or h.get("X-Forwarded-Host") or h.get("X-Forwarded-Proto"):
        return True
    if not ignore_xff and h.get("X-Forwarded-For"):
        return True
    host = (request.host or "").lower()
    return host.endswith(".ts.net") or ".ts.net:" in host


def _origin_is_loopback(origin: str) -> bool:
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and (parts.hostname or "").lower() in _LOOPBACK_HOSTS


def request_is_local(ignore_xff: bool = False) -> bool:
    """The request comes from this machine: a process, or a page a local
    browser loaded from this server. False on any doubt."""
    try:
        if _proxied(ignore_xff):
            return False
        origin = request.headers.get("Origin")
        if origin and not _origin_is_loopback(origin):
            return False
        return (request.remote_addr or "") in _LOOPBACK_ADDRS
    except Exception:
        return False


def caller_is_local_script() -> bool:
    """Only a process on this machine, and not a browser page.

    The web server binds to 127.0.0.1, but that alone does not say "local":
    a phone, and every other device on the tailnet, arrives through
    ``tailscale serve``, which also connects from 127.0.0.1. The proxy's
    identity header and the host name tell those apart. A browser page is
    refused as well: it sends Origin, and a script does not. (A cross-site
    page cannot send application/json without a preflight either, and the
    endpoints that use this read nothing else.)

    This is the rule the message hook, the approval ping and Durable Jobs
    have always applied; it moved here from ``app/routes/live_api.py``, which
    still exports it as ``_hook_caller_is_local``.
    """
    try:
        if request.headers.get("Origin"):
            return False
        return request_is_local()
    except Exception:
        return False


# ── the pairing code and its cookie ──────────────────────────────────────────

_state_cache: dict = {"mtime": None, "state": {}}


def _read_state() -> dict:
    """The stored code and salt, or {} when none exists yet. Never creates."""
    try:
        mtime = PAIR_FILE.stat().st_mtime_ns
    except OSError:
        _state_cache.update(mtime=None, state={})
        return {}
    if _state_cache["mtime"] == mtime and _state_cache["state"]:
        return dict(_state_cache["state"])
    try:
        data = json.loads(PAIR_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not (isinstance(data, dict) and data.get("token") and data.get("salt")):
        _state_cache.update(mtime=None, state={})
        return {}
    _state_cache.update(mtime=mtime, state=dict(data))
    return dict(data)


def _write_state(state: dict) -> None:
    PAIR_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PAIR_FILE.parent / (PAIR_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, PAIR_FILE)
    _state_cache.update(mtime=None, state={})


def _new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def pairing_state() -> dict:
    """The stored code and salt, generated on first need."""
    with _lock:
        state = _read_state()
        if not state:
            state = {"token": _new_code(), "salt": secrets.token_hex(32),
                     "created": time.time(), "rotated": None}
            _write_state(state)
            log.info("Mobile pairing code created")
        return state


def pair_code() -> str:
    return pairing_state()["token"]


def display_code(code: str) -> str:
    """``ABCDE-FGHJK``: the 10 characters in two groups, easier to read off."""
    return code[:5] + "-" + code[5:] if len(code) == CODE_LENGTH else code


def rotate() -> str:
    """A new code and a new salt: every cookie set under the old code dies."""
    with _lock:
        previous = _read_state()
        state = {"token": _new_code(), "salt": secrets.token_hex(32),
                 "created": previous.get("created") or time.time(), "rotated": time.time()}
        _write_state(state)
        log.info("Mobile pairing code rotated")
        return state["token"]


def normalize_code(raw) -> str:
    """What the user typed, as the code is stored: upper case, letters and
    digits only (spaces and the display dash fall away)."""
    return "".join(ch for ch in str(raw or "").upper() if ch.isalnum())


def code_matches(raw) -> bool:
    return hmac.compare_digest(normalize_code(raw), pairing_state()["token"])


def cookie_value(state: dict | None = None) -> str:
    state = state or pairing_state()
    return hmac.new(state["salt"].encode(), state["token"].encode(), hashlib.sha256).hexdigest()


def has_valid_cookie() -> bool:
    try:
        presented = request.cookies.get(COOKIE_NAME) or ""
    except Exception:
        return False
    if not presented:
        return False
    state = _read_state()          # no code yet: no cookie can be valid
    if not state:
        return False
    return hmac.compare_digest(presented, cookie_value(state))


def _request_is_https() -> bool:
    try:
        if request.is_secure:
            return True
        if (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower() == "https":
            return True
        host = (request.host or "").lower()
        return host.endswith(".ts.net") or ".ts.net:" in host
    except Exception:
        return False


def set_pair_cookie(response):
    """Mark this browser as paired: HttpOnly, SameSite=Lax, a year, Secure
    whenever the request came over HTTPS (it always does through the tailnet)."""
    response.set_cookie(COOKIE_NAME, cookie_value(), max_age=COOKIE_MAX_AGE_S,
                        httponly=True, samesite="Lax", secure=_request_is_https(), path="/")
    return response


def pair_url(base_url: str | None, code: str | None = None) -> str | None:
    """The one-tap link: ``https://<mobile host>/pair?code=…``."""
    if not base_url:
        return None
    return base_url.rstrip("/") + "/pair?code=" + (code or pair_code())


def pairing_view(code: str | None = None, base_url: str | None = None) -> dict:
    """What the Mobile Command modal shows. Callers hand this to LOCAL
    requests only: the code pairs further devices."""
    code = code or pair_code()
    return {
        "auth_enabled": auth_enabled(),
        "pair_code": code,
        "pair_code_display": display_code(code),
        "pair_url": pair_url(base_url, code),
    }


# ── rate limit on code attempts ──────────────────────────────────────────────

_attempts: dict[str, deque] = {}


def _client_key() -> str:
    xff = request.headers.get("X-Forwarded-For") or ""
    if xff:
        return xff.split(",")[0].strip() or "?"
    return request.remote_addr or "?"


def rate_limited() -> bool:
    """Record one code attempt by this caller; True when it is over the
    limit (5 a minute). Through ``tailscale serve`` the caller is the first
    X-Forwarded-For address, the phone's tailnet IP."""
    now = time.monotonic()
    key = _client_key()
    with _lock:
        if len(_attempts) > 1000:
            for k in [k for k, q in _attempts.items() if not q or now - q[-1] > RATE_LIMIT_WINDOW_S]:
                _attempts.pop(k, None)
        q = _attempts.setdefault(key, deque())
        while q and now - q[0] > RATE_LIMIT_WINDOW_S:
            q.popleft()
        if len(q) >= RATE_LIMIT_ATTEMPTS:
            return True
        q.append(now)
        return False


def reset_rate_limit() -> None:
    with _lock:
        _attempts.clear()


# ── config ───────────────────────────────────────────────────────────────────

def auth_enabled() -> bool:
    """``mobile_command_auth`` in kanban_config.json, default OFF.

    Off by the owner's decision (2026-10-23): every device on the tailnet is
    the owner's, and pairing each phone was friction with no benefit to them.
    With it off, any tailnet device reaches the app without a code, exactly
    as before pairing existed. What stays on regardless: ``@local_only``
    routes (anything that runs a command or opens a desktop program answers
    only this machine) and the Socket.IO Origin check (a web page on another
    site cannot drive sessions). Set ``"mobile_command_auth": true`` to
    require pairing.
    """
    try:
        return bool(get_kanban_config().get(CONFIG_KEY, False))
    except Exception:
        return False


# ── the gate ─────────────────────────────────────────────────────────────────

def request_is_authorized(lenient_xff: bool = False) -> bool:
    """The paired tier: local, or a valid cookie."""
    return request_is_local(ignore_xff=lenient_xff) or has_valid_cookie()


def local_only(view):
    """Only this machine may call the route, whatever the config says.

    For anything that runs a command or starts a desktop program. Refuses
    with 403 ``local_only``. The gate skips routes carrying this mark, so a
    phone gets this answer and not the pairing one.
    """
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        denied = require_local()
        if denied is not None:
            return denied
        return view(*args, **kwargs)
    wrapped._vn_local_only = True
    return wrapped


def require_local():
    """Inline form of ``local_only`` for a handler that is local-only on some
    inputs only (``/api/restart`` with a scope other than ``web``). Returns
    the 403 response to send, or None when the caller is local."""
    if request_is_local():
        return None
    return jsonify(ok=False, error="local_only",
                   message="This action is only available from the computer running VibeNode."), 403


def _gate():
    if not auth_enabled():
        return None
    path = request.path or "/"
    if request.endpoint == "static" or path in OPEN_PATHS or path.startswith(OPEN_PREFIXES):
        return None
    if path in SELF_GUARDED_PATHS or path.startswith(SELF_GUARDED_PREFIXES):
        return None
    view = current_app.view_functions.get(request.endpoint) if request.endpoint else None
    if getattr(view, "_vn_local_only", False):
        return None
    if request_is_authorized(lenient_xff=path in XFF_LENIENT_PATHS):
        return None
    if path.startswith("/api/"):
        return jsonify(ok=False, error="pairing_required"), 401
    return redirect("/pair", code=302)


def install(app) -> None:
    """Register the paired-tier gate on every request of this app."""
    app.extensions[_EXT_KEY] = True
    app.before_request(_gate)


def installed(app) -> bool:
    try:
        return bool(app.extensions.get(_EXT_KEY))
    except Exception:
        return False


# ── Socket.IO: Origin, then pairing ──────────────────────────────────────────

_mobile_host: dict = {"host": "", "checked": 0.0, "refreshing": False}


def _refresh_mobile_host() -> None:
    try:
        from . import mobile_command
        host = ""
        if mobile_command.is_flag_enabled() and mobile_command.tailscale_bin():
            host = mobile_command._dns_from_status(mobile_command._status_json()).lower()
        _mobile_host.update(host=host, checked=time.monotonic())
    except Exception:
        _mobile_host.update(checked=time.monotonic())
    finally:
        _mobile_host["refreshing"] = False


def known_mobile_host() -> str:
    """This node's MagicDNS name, cached for 10 minutes. ``tailscale status``
    is a subprocess, so a stale value is refreshed in the background and the
    current value answers now; the same-origin rule in ``origin_allowed``
    does not need it at all."""
    if time.monotonic() - _mobile_host["checked"] > MOBILE_HOST_TTL_S and not _mobile_host["refreshing"]:
        _mobile_host["refreshing"] = True
        threading.Thread(target=_refresh_mobile_host, name="mobile-auth-host", daemon=True).start()
    return _mobile_host["host"]


def set_known_mobile_host(host: str) -> None:
    """Mobile Command tells us the name as soon as it knows it."""
    _mobile_host.update(host=(host or "").lower().rstrip("."), checked=time.monotonic())


def _request_host(environ) -> str:
    raw = ""
    if environ is not None:
        raw = environ.get("HTTP_X_FORWARDED_HOST") or environ.get("HTTP_HOST") or ""
    else:
        try:
            raw = request.headers.get("X-Forwarded-Host") or request.host or ""
        except Exception:
            raw = ""
    raw = raw.split(",")[0].strip().lower()
    if raw.startswith("["):                      # [::1]:5050
        return raw.split("]")[0][1:]
    return raw.rsplit(":", 1)[0] if raw.count(":") == 1 else raw


def origin_allowed(origin, environ=None) -> bool:
    """The Origin check for the Socket.IO handshake, and for every HTTP
    request the Socket.IO server sees (``cors_allowed_origins``).

    Accepted: no Origin (a non-browser client), a loopback origin on any port
    (the user's own local pages), and the Mobile Command host: a ``.ts.net``
    origin that is the request's own Host, or the node's known MagicDNS name.
    Everything else, including another node on the tailnet serving a page,
    is refused.
    """
    if not origin:
        return True
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        return False
    if host in _LOOPBACK_HOSTS:
        return True
    if host.endswith(".ts.net"):
        if host == _request_host(environ):
            return True
        if host == known_mobile_host():
            return True
    return False


def socket_connection_allowed(app=None) -> tuple[bool, str]:
    """``(allowed, reason)`` for a Socket.IO ``connect``. The Origin rule
    always applies. The pairing rule applies when the app has the gate
    installed (every ``create_app``) and the config switch is on."""
    try:
        if not origin_allowed(request.headers.get("Origin"), request.environ):
            return False, "origin"
        if (app is None or installed(app)) and auth_enabled() and not request_is_authorized():
            return False, "pairing_required"
        return True, ""
    except Exception:
        log.exception("socket connection check failed; refusing")
        return False, "error"
