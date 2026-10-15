"""Durable Jobs API: launch-and-register, list, log tail, Relaunch, Leave it.

Thin HTTP layer over ``daemon/durable_jobs.py`` (spec:
docs/plans/durable-jobs-spec.md).  ``POST /api/jobs/run`` is the same step
as ``vibenode job run``: it launches the command detached on this machine and
writes its registry record together.  Because it runs an arbitrary command,
only a process on this machine may call it (the same test the message hook
uses: ``_hook_caller_is_local``); the phone reaches this server through the
tailnet proxy and is refused.

The list, the log tail and the two buttons are for people, so they are open to
every VibeNode client.  Relaunch only re-runs a command that is already
registered.  The supervisor in the Session Engine reads the same file; nothing
here talks to the daemon.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from daemon import durable_jobs as dj
from .live_api import _hook_caller_is_local

bp = Blueprint("jobs_api", __name__)


def _registry_error(exc):
    return jsonify(ok=False, error=str(exc), registry_error=True), 500


@bp.route("/api/jobs/run", methods=["POST"])
def api_jobs_run():
    """Launch a long job detached and register it.  Body: the fields of
    ``durable_jobs.new_record`` (``name``, ``command``, ``cwd``, ``done_test``
    required; ``done_test`` is a regex or ``{pattern, file, last_line}``)."""
    if not _hook_caller_is_local():
        return jsonify(ok=False, error="local callers only"), 403
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(ok=False, error="a JSON object is required"), 400
    allowed = {"name", "command", "cwd", "done_test", "job_id", "project", "session_id",
               "env_file", "running_match", "progress", "irreversible_after", "log_path",
               "resumable"}
    fields = {k: v for k, v in data.items() if k in allowed}
    try:
        rec = dj.run_job(**fields)
    except dj.JobError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    except dj.RegistryError as exc:
        return _registry_error(exc)
    return jsonify(ok=rec["state"] != "failed", job=rec)


@bp.route("/api/jobs")
def api_jobs_list():
    try:
        return jsonify(ok=True, jobs=dj.list_jobs())
    except dj.RegistryError as exc:
        return _registry_error(exc)


@bp.route("/api/jobs/<job_id>")
def api_job_get(job_id):
    try:
        rec = dj.get_job(job_id)
    except dj.RegistryError as exc:
        return _registry_error(exc)
    if rec is None:
        return jsonify(ok=False, error="no such job"), 404
    return jsonify(ok=True, job=rec)


@bp.route("/api/jobs/<job_id>/log")
def api_job_log(job_id):
    """The tail of the job's log (the one its done test reads)."""
    try:
        rec = dj.get_job(job_id)
    except dj.RegistryError as exc:
        return _registry_error(exc)
    if rec is None:
        return jsonify(ok=False, error="no such job"), 404
    try:
        limit = max(500, min(int(request.args.get("bytes", 8000)), 200000))
    except ValueError:
        limit = 8000
    path = dj.resolve_log(rec, rec.get("done_test"))
    return jsonify(ok=True, path=path, tail=dj.read_tail(path, limit) if path else "")


@bp.route("/api/jobs/<job_id>/relaunch", methods=["POST"])
def api_job_relaunch(job_id):
    """The Relaunch button.  A person decided, so the no-progress count and
    the acknowledgement reset."""
    try:
        rec, res = dj.relaunch_job(job_id, by="user")
    except dj.JobError as exc:
        return jsonify(ok=False, error=str(exc)), 404
    except dj.RegistryError as exc:
        return _registry_error(exc)
    return jsonify(ok=bool(res.get("ok")), job=rec, error=res.get("error") or "")


@bp.route("/api/jobs/<job_id>/leave", methods=["POST"])
def api_job_leave(job_id):
    """The Leave-it button: stop asking about this job."""
    try:
        rec = dj.leave_job(job_id)
    except dj.JobError as exc:
        return jsonify(ok=False, error=str(exc)), 404
    except dj.RegistryError as exc:
        return _registry_error(exc)
    return jsonify(ok=True, job=rec)
