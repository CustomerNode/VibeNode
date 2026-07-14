"""
Mobile Command API — enable/disable/status for private phone access.

Thin HTTP layer over app/mobile_command.py. All the Tailscale logic lives there;
these routes just expose it to the System -> Mobile Command modal.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from .. import mobile_command

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


@bp.route("/api/mobile/status")
def api_mobile_status():
    return jsonify(mobile_command.status(port=_port_from_request()))


@bp.route("/api/mobile/enable", methods=["POST"])
def api_mobile_enable():
    return jsonify(mobile_command.enable(port=_port_from_request()))


@bp.route("/api/mobile/disable", methods=["POST"])
def api_mobile_disable():
    return jsonify(mobile_command.disable())


@bp.route("/api/mobile/name", methods=["POST"])
def api_mobile_name():
    """Set this computer's Home-Screen label (so multiple machines are
    distinguishable on the phone). Empty resets to the hostname default."""
    data = request.get_json(silent=True) or {}
    name = mobile_command.set_device_name(data.get("name", ""))
    return jsonify({"device_name": name})
