"""5-hour pacing (src/web/pace_5h.py): the reading behind the 5-hour tile,
the slider, and the Reset button.

  GET  /api/pace-5h            what the tile shows
  POST /api/pace-5h/setting    {"mult": 0 | 1 | 1.5 | 2 | 3 | 5}   0 = Off
  POST /api/pace-5h/reset      ends the window now; a person only

Reset is a person's button.  It is taken only from the app's own page (the
owner, a same-site Origin, and the header the page sends), and every session
is refused a tool call that names it (src/engine/tool_guards).
"""

from __future__ import annotations

import time
from urllib.parse import urlparse

from flask import Blueprint, current_app, jsonify, request

from .. import pace_5h as pace
from ..mobile_auth import local_only

bp = Blueprint("pace_api", __name__)

HUMAN_HEADER = "X-VN-Human"


def _emit(event, data):
    try:
        from .. import socketio
        socketio.emit(event, data)
    except Exception:
        pass


def _reading():
    from engine import usage_limits as _ul
    from shared.config import get_kanban_config
    now = time.time()
    pct, week_resets = pace.weekly(_ul.load(), now)
    mult = pace.mult_of(get_kanban_config() or {})
    return pace.public(pace.load(), mult, pct, week_resets, now)


@bp.route("/api/pace-5h")
def pace_reading():
    return jsonify({"ok": True, **_reading()})


@bp.route("/api/pace-5h/setting", methods=["POST"])
def pace_setting():
    from shared.config import get_kanban_config, save_kanban_config
    data = request.get_json(silent=True) or {}
    try:
        mult = float(data.get("mult") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "mult must be a number"}), 400
    if mult not in pace.STEPS:
        return jsonify({"error": "mult must be one of the slider's steps"}), 400
    cfg = dict(get_kanban_config() or {})
    if mult:
        cfg[pace.SETTING_NAME] = mult
    else:
        cfg.pop(pace.SETTING_NAME, None)
    save_kanban_config(cfg)
    r = _reading()
    _emit("pace_5h", r)
    return jsonify({"ok": True, **r})


def _from_the_page() -> bool:
    if request.headers.get(HUMAN_HEADER) != "reset":
        return False
    origin = request.headers.get("Origin") or ""
    host = request.host or ""
    return bool(origin) and urlparse(origin).netloc == host


@bp.route("/api/pace-5h/reset", methods=["POST"])
@local_only
def pace_reset():
    if not _from_the_page():
        return jsonify({"error": "The 5-hour reset is a button for a person, on VibeNode's page."}), 403
    r = pace.reset(current_app.session_manager)
    out = _reading()
    _emit("pace_5h", out)
    return jsonify({"ok": True, **r, **out})
