"""
Mobile Command API — enable/disable/status for private phone access, and the
pairing routes that let a phone in (app/mobile_auth.py).

Thin HTTP layer over app/mobile_command.py. All the Tailscale logic lives there;
these routes just expose it to the System -> Mobile Command modal.

Pairing
-------
Every page and ``/api/*`` route needs the paired tier: a local caller, or the
``vn_pair`` cookie. ``GET /pair`` is the page a phone lands on, ``POST
/api/pair`` checks the code and sets the cookie, ``GET /pair?code=…`` does both
in one tap (the link and QR the modal shows), and ``POST /api/pair/rotate``
(local-only) issues a new code and logs every paired device out.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, redirect, render_template, request

from .. import mobile_auth, mobile_command

bp = Blueprint("mobile_api", __name__)


def _port_from_request() -> int:
    """Derive the web-server port from the incoming request host header.

    VibeNode binds to 127.0.0.1:<port>; the port the browser used to reach us is
    exactly the port Tailscale should serve. Falls back to the configured/default.
    """
    host = request.host or ""
    if ":" in host:
        try:
            return int(host.rsplit(":", 1)[1])
        except (ValueError, IndexError):
            pass
    return mobile_command.configured_port()


def _with_pairing(st: dict) -> dict:
    """Add the pairing code and one-tap link for a LOCAL caller, and only when
    pairing is switched on (``mobile_command_auth``; off by default): the code
    pairs further devices, so a paired phone does not get to read it."""
    if mobile_auth.auth_enabled() and mobile_auth.request_is_local():
        st.update(mobile_auth.pairing_view(base_url=st.get("url")))
    else:
        st["auth_enabled"] = mobile_auth.auth_enabled()
    return st


@bp.route("/api/mobile/status")
def api_mobile_status():
    return jsonify(_with_pairing(mobile_command.status(port=_port_from_request())))


@bp.route("/api/mobile/enable", methods=["POST"])
def api_mobile_enable():
    return jsonify(_with_pairing(mobile_command.enable(port=_port_from_request())))


@bp.route("/api/mobile/disable", methods=["POST"])
def api_mobile_disable():
    return jsonify(_with_pairing(mobile_command.disable()))


@bp.route("/api/mobile/name", methods=["POST"])
def api_mobile_name():
    """Set this computer's Home-Screen label (so multiple machines are
    distinguishable on the phone). Empty resets to the hostname default."""
    data = request.get_json(silent=True) or {}
    name = mobile_command.set_device_name(data.get("name", ""))
    return jsonify({"device_name": name})


# ── pairing ──────────────────────────────────────────────────────────────────

@bp.route("/pair")
def pair_page():
    """The pairing page, or the one-tap pair when ``?code=`` is given.

    A caller that is already in (local, paired, or the gate switched off) is
    sent to the app. A wrong code comes back here with ``?error=1``; too many
    attempts (5 a minute per address) get a 429.
    """
    code = request.args.get("code")
    if code is not None:
        if mobile_auth.rate_limited():
            return render_template("pair.html", error="rate_limited",
                                   device_name=mobile_command.device_name()), 429
        if mobile_auth.code_matches(code):
            return mobile_auth.set_pair_cookie(redirect("/", code=302))
        return redirect("/pair?error=1", code=302)
    if not mobile_auth.auth_enabled() or mobile_auth.request_is_authorized():
        return redirect("/", code=302)
    error = "bad_code" if request.args.get("error") else ""
    return render_template("pair.html", error=error, device_name=mobile_command.device_name())


@bp.route("/api/pair", methods=["POST"])
def api_pair():
    """Check a code and set the pairing cookie.

    Body: JSON ``{"code": str}`` or a form field ``code``. Reply: ``{"ok":
    true}`` with the cookie, 403 ``bad_code``, or 429 ``rate_limited``. A form
    post is answered with a redirect instead (no-JS fallback of the page).
    """
    form_post = not request.is_json and "code" in request.form
    if mobile_auth.rate_limited():
        if form_post:
            return redirect("/pair?error=1", code=302)
        return jsonify(ok=False, error="rate_limited", retry_after=int(mobile_auth.RATE_LIMIT_WINDOW_S)), 429
    data = request.get_json(silent=True)
    code = data.get("code") if isinstance(data, dict) else request.form.get("code")
    if not mobile_auth.code_matches(code):
        if form_post:
            return redirect("/pair?error=1", code=302)
        return jsonify(ok=False, error="bad_code"), 403
    if form_post:
        return mobile_auth.set_pair_cookie(redirect("/", code=302))
    return mobile_auth.set_pair_cookie(jsonify(ok=True))


@bp.route("/api/pair/rotate", methods=["POST"])
@mobile_auth.local_only
def api_pair_rotate():
    """Issue a new pairing code. Every cookie set under the old one stops
    working at once. Local-only."""
    code = mobile_auth.rotate()
    base = None
    try:
        base = mobile_command.tailnet_url()
    except Exception:
        base = None
    return jsonify(ok=True, **mobile_auth.pairing_view(code, base_url=base))
