"""
Admin / maintenance routes — phantom-session scrubber, etc.

These endpoints are user-initiated maintenance actions, not part of any
automatic flow. They are registered under ``/api/admin/...`` and gated by
the same localhost-only binding as ``/api/restart``.

POST /api/admin/scrub-phantoms
==============================

Removes "phantom" entries from ``_session_names.json``: rows whose session
ID has no on-disk ``.jsonl`` AND is not an in-flight daemon session AND is
not tracked as a utility session.

Background — see docs/plans/phantom-sessions-fix-spec.md. On 2026-05-15 the
user manually scrubbed 169 phantom rows across five projects. The fixes in
``app/titling.py`` and ``app/routes/sessions_api.py`` close the leak going
forward; this route cleans residue left from before those fixes shipped.

Algorithm (per project)
-----------------------
1. Load ``_session_names.json``.
2. Build the **live-session set** as the union of:
   - ``{s["id"] for s in all_sessions(summary_only=True, project=p)}`` —
     JSONL-on-disk sessions, after tombstone / utility / remap filtering.
   - ``{s["session_id"] for s in sm.get_all_states() if cwd matches project}``
     — covers in-flight daemon sessions that haven't flushed a .jsonl yet.
3. Build the **protected set** as ``_get_utility_ids(p)`` — utility sids
   must never be reported as phantoms.
4. Any entry in ``_session_names.json`` that is not in (live ∪ protected)
   is a phantom.
5. If ``dry_run`` (default true): return the per-project counts. No
   mutations.
6. If ``dry_run=false``: write a backup
   (``_session_names.json.backup-YYYYMMDD-HHMMSS``, UTC, filesystem-safe —
   no colons), then remove the phantom keys and persist the new file.
   Evict matching entries from ``_summary_cache``. Do NOT mutate
   ``_remapped_sessions.json`` — its TTL handles itself. Emit
   ``sessions_refresh`` over WebSocket so other tabs update.

Concurrency
-----------
A process-local ``threading.Lock`` serialises scrubs. A second concurrent
call returns HTTP 429.
"""

import json
import logging
import threading
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, request

from .. import socketio
from ..config import (
    _CLAUDE_PROJECTS,
    _SYSTEM_UTILITY_CWD,
    _encode_cwd,
    _summary_cache,
    cwd_matches_active_project,
)
from ..session_store import (
    _get_utility_ids,
    _load_names,
    _names_file,
)
from ..sessions import all_sessions

bp = Blueprint('admin_api', __name__)
log = logging.getLogger(__name__)

# Serialises scrub runs across the whole process — second caller gets 429.
_scrub_lock = threading.Lock()


def _list_user_projects() -> list[str]:
    """Return all encoded project names visible to the user.

    Mirrors the filter in ``project_api.api_projects`` — excludes the
    system-utility project and the ``subagents/`` directories. We don't
    apply the home-directory filter here because the admin route is a
    full-installation scrub: any project under ``_CLAUDE_PROJECTS`` that
    holds a ``_session_names.json`` is fair game.
    """
    if not _CLAUDE_PROJECTS.is_dir():
        return []
    system_utility_encoded = _encode_cwd(_SYSTEM_UTILITY_CWD)
    results: list[str] = []
    for d in _CLAUDE_PROJECTS.iterdir():
        if not d.is_dir():
            continue
        name = d.name
        if name.startswith("subagents") or name.startswith("_"):
            continue
        if name == system_utility_encoded:
            continue
        results.append(name)
    return results


def _compute_phantoms_for_project(project: str, sm) -> tuple[list[str], dict]:
    """Return (phantom_ids, names_dict) for *project*.

    *phantom_ids* is the list of session IDs in ``_session_names.json``
    that are not in the live-session set nor the protected set.
    *names_dict* is the loaded names file (for the caller to mutate).
    """
    names = _load_names(project)
    if not names:
        return [], names

    # Live set #1: JSONL-on-disk sessions, after tombstone/utility/remap filter
    on_disk = {s["id"] for s in all_sessions(summary_only=True, project=project)}

    # Live set #2: in-flight daemon sessions that haven't flushed a .jsonl yet
    in_flight: set[str] = set()
    try:
        states = sm.get_all_states() if sm is not None else []
    except Exception as e:
        log.debug("scrub: sm.get_all_states failed: %s", e)
        states = []
    for state in (states or []):
        sid = state.get("session_id", "") if isinstance(state, dict) else ""
        if not sid:
            continue
        # Only count this state if its cwd belongs to *project*. Without this
        # we'd treat sessions from other projects as live and miss real
        # phantoms.
        state_cwd = state.get("cwd", "") if isinstance(state, dict) else ""
        if state_cwd and not cwd_matches_active_project(state_cwd, project=project):
            continue
        in_flight.add(sid)

    # Protected set: utility sessions (title gen, planner). Never phantom.
    protected = _get_utility_ids(project)

    live = on_disk | in_flight | protected

    phantoms = [sid for sid in names.keys() if sid not in live]
    return phantoms, names


def _backup_filename() -> str:
    """``_session_names.json.backup-YYYYMMDD-HHMMSS`` in UTC.

    Filesystem-safe (no colons — Windows rejects them). Matches the manual
    cleanup convention from 2026-05-15.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"_session_names.json.backup-{ts}"


@bp.route("/api/admin/scrub-phantoms", methods=["POST"])
def api_scrub_phantoms():
    """Scrub phantom entries from ``_session_names.json``.

    Request body (all optional):
      - ``project``: encoded project name. Default: all visible projects.
      - ``dry_run``: bool. Default ``True``. When true, never mutate.

    Response (200):
      {
        "ok": true,
        "dry_run": bool,
        "per_project": [
          {"project": "...", "removed": N, "backup": "..." | null,
           "error": "..." (only on failure)},
          ...
        ],
        "total_removed": N
      }

    Concurrent callers get HTTP 429.
    """
    data = request.get_json(silent=True) or {}
    dry_run = bool(data.get("dry_run", True))
    project_param = (data.get("project") or "").strip()

    # Validate project param if provided
    if project_param and not (_CLAUDE_PROJECTS / project_param).is_dir():
        return jsonify({
            "ok": False,
            "error": f"Unknown project: {project_param}",
            "valid_projects": _list_user_projects(),
        }), 400

    # Serialise across the process — second caller gets 429.
    if not _scrub_lock.acquire(blocking=False):
        return jsonify({
            "ok": False,
            "error": "Another scrub is already in progress.",
        }), 429

    try:
        sm = current_app.session_manager

        if project_param:
            projects = [project_param]
        else:
            projects = _list_user_projects()

        per_project: list[dict] = []
        total_removed = 0
        any_mutation = False

        for project in projects:
            try:
                phantoms, names = _compute_phantoms_for_project(project, sm)
            except Exception as e:
                log.warning("scrub: failed to scan project %s: %s", project, e)
                per_project.append({
                    "project": project,
                    "removed": 0,
                    "backup": None,
                    "error": str(e),
                })
                continue

            if dry_run:
                per_project.append({
                    "project": project,
                    "removed": len(phantoms),
                    "backup": None,
                    "phantoms": phantoms,
                })
                total_removed += len(phantoms)
                continue

            if not phantoms:
                per_project.append({
                    "project": project,
                    "removed": 0,
                    "backup": None,
                })
                continue

            nf = _names_file(project)
            backup_name = _backup_filename()
            backup_path = nf.parent / backup_name

            # Write the backup first. If this fails we abort for this
            # project — never mutate the live file without a backup.
            try:
                backup_path.write_text(
                    json.dumps(names, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception as e:
                log.warning("scrub: backup failed for %s: %s", project, e)
                per_project.append({
                    "project": project,
                    "removed": 0,
                    "backup": None,
                    "error": f"backup failed: {e}",
                })
                continue

            # Remove phantoms in-memory then write the live file. If the
            # write fails we attempt to restore from the just-written
            # backup so the live file is never half-mutated.
            new_names = {k: v for k, v in names.items() if k not in set(phantoms)}
            try:
                nf.write_text(
                    json.dumps(new_names, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception as e:
                log.warning("scrub: write failed for %s: %s", project, e)
                # Try to restore the live file from the backup so we
                # never leave the user with a half-written names file.
                try:
                    nf.write_text(
                        backup_path.read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
                except Exception as restore_err:
                    log.error("scrub: restore failed for %s: %s",
                              project, restore_err)
                per_project.append({
                    "project": project,
                    "removed": 0,
                    "backup": backup_name,
                    "error": f"write failed: {e}",
                })
                continue

            # Evict summary-cache entries that point at any phantom path.
            # Keys are (path_str, mtime, size); the path component carries
            # the session id, so a substring match is the simplest filter.
            stale_keys = [
                k for k in _summary_cache
                if any(f"{sid}.jsonl" in k[0] for sid in phantoms)
            ]
            for k in stale_keys:
                _summary_cache.pop(k, None)

            per_project.append({
                "project": project,
                "removed": len(phantoms),
                "backup": backup_name,
            })
            total_removed += len(phantoms)
            any_mutation = True

        # Emit a sessions_refresh so any open tab updates its sidebar.
        if any_mutation:
            try:
                socketio.emit("sessions_refresh", {"reason": "scrub-phantoms"})
            except Exception as e:
                log.debug("scrub: emit sessions_refresh failed: %s", e)

        return jsonify({
            "ok": True,
            "dry_run": dry_run,
            "per_project": per_project,
            "total_removed": total_removed,
        })

    finally:
        _scrub_lock.release()
