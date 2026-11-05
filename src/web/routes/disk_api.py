"""Disk: the reading behind the Disk gauge in the usage panel, the cap
("VibeNode may use"), and deleting sessions and emptying the Trash.

Every action runs inside the web server with no Claude session, and frees
space before it writes anything, so it works when the disk is full.  The
logic lives in src/web/disk_cleanup.py.

  GET  /api/disk[?light=1]   reading (light: the drive only, for the bar)
  POST /api/disk/cap         {"bytes": N}   0 or the drive's size = no cap
  POST /api/disk/delete      {"ids": [{"id", "project"}]} or
                             {"created_days": X, "inactive_days": Y}, "dry_run"
  POST /api/disk/empty-trash
"""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from .. import disk_cleanup as dc
from ..config import _CLAUDE_PROJECTS

bp = Blueprint("disk_api", __name__)

_REPO = Path(__file__).resolve().parents[3]
_EXTRA = (_REPO / "data", _REPO / "logs")


def _emit(event, data):
    try:
        from .. import socketio
        socketio.emit(event, data)
    except Exception:
        pass


def _num(v, lo=0.0, hi=36500.0):
    if v is None or v == "":
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, v))


def _cap(total):
    from shared.config import get_kanban_config
    return dc.cap_of(get_kanban_config() or {}, total)


@bp.route("/api/disk")
def disk_reading():
    if request.args.get("light"):
        # The bar shows VibeNode's use of its allotment, so it needs the share
        # too (cached briefly in disk_cleanup.share).
        d = dc.disk_reading(_CLAUDE_PROJECTS)
        return jsonify({"ok": True, "disk": d, "share": dc.share(_CLAUDE_PROJECTS, _EXTRA),
                        "cap": _cap(d.get("total") or 0)})
    r = dc.reading(current_app.session_manager, _CLAUDE_PROJECTS, _EXTRA)
    r["cap"] = _cap(r["disk"].get("total") or 0)
    return jsonify(r)


@bp.route("/api/disk/cap", methods=["POST"])
def disk_cap():
    from shared.config import get_kanban_config, save_kanban_config
    data = request.get_json(silent=True) or {}
    total = dc.disk_reading(_CLAUDE_PROJECTS).get("total") or 0
    try:
        want = int(data.get("bytes") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "bytes must be a number"}), 400
    cfg = dict(get_kanban_config() or {})
    if want <= 0 or want >= total:
        cfg.pop(dc.CAP_KEY, None)
        want = 0
    else:
        want = cfg[dc.CAP_KEY] = max(dc.CAP_MIN, want)
    try:
        save_kanban_config(cfg)
    except OSError:
        return jsonify({"error": "Could not save: the disk may be full"}), 507
    return jsonify({"ok": True, "cap": want})


@bp.route("/api/disk/delete", methods=["POST"])
def disk_delete():
    data = request.get_json(silent=True) or {}
    running = dc.running_ids(current_app.session_manager)
    ids = data.get("ids")
    if isinstance(ids, list) and ids:
        want = {(str(i.get("id")), str(i.get("project"))) for i in ids if isinstance(i, dict)}
        items = [i for i in dc.delete_candidates(_CLAUDE_PROJECTS, inactive_days=0, running=running)
                 if (i["id"], i["project"]) in want]
    else:
        items = dc.delete_candidates(
            _CLAUDE_PROJECTS, created_days=_num(data.get("created_days")),
            inactive_days=_num(data.get("inactive_days")), running=running)
    if data.get("dry_run"):
        return jsonify({"ok": True, "dry_run": True, "count": len(items),
                        "bytes": sum(i["bytes"] for i in items)})
    r = dc.delete_sessions(items, _CLAUDE_PROJECTS)
    try:
        current_app.session_manager._save_registry_now()
    except Exception:
        pass
    _emit("sessions_refresh", {"reason": "disk-delete"})
    return jsonify({"ok": True, **r})


@bp.route("/api/disk/empty-trash", methods=["POST"])
def disk_empty_trash():
    return jsonify({"ok": True, "freed": dc.empty_trash(_CLAUDE_PROJECTS)})
