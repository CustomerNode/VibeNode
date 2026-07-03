"""
Search API — full-text search across session transcripts.

Thin HTTP layer over ``app.search_index``: resolves the target project the
same way other session routes do (explicit ``?project=`` param, else the
active project, else auto-detect), triggers a debounced incremental index
pass, and returns ranked results.  All heavy lifting and all safety
decisions (query sanitization, tombstone filtering, corrupted-DB recovery)
live in ``app.search_index``.
"""

import logging

from flask import Blueprint, jsonify, request

from ..config import _sessions_dir, get_active_project
from ..search_index import ensure_index, search

logger = logging.getLogger("app.search_api")

bp = Blueprint("search_api", __name__)


@bp.route("/api/search")
def api_search():
    """Search the active project's session history.

    Query params:
        q:       free-text query (min 2 chars unless ``file`` is given).
        file:    touched-file path fragment (case/slash-insensitive).
        project: encoded project dir name; defaults to the active project.
        limit:   max sessions returned (1-100, default 20).

    Returns 200 with the ``app.search_index.search()`` payload, 400 for
    missing/invalid queries, 500 for unexpected failures.
    """
    q = (request.args.get("q") or "").strip()
    file_filter = (request.args.get("file") or "").strip()
    project = (request.args.get("project") or "").strip()
    try:
        limit = max(1, min(100, int(request.args.get("limit", "20"))))
    except ValueError:
        limit = 20

    if not q and not file_filter:
        return jsonify({"error": "missing query — pass q and/or file"}), 400
    if q and len(q) < 2 and not file_filter:
        return jsonify({"error": "query too short (min 2 characters)"}), 400
    # Encoded project names never contain path separators or '..' — reject
    # anything that could traverse outside ~/.claude/projects (the indexer
    # would otherwise happily index and persist a foreign directory's
    # JSONLs into the search DB).
    if "/" in project or "\\" in project or ".." in project:
        return jsonify({"error": "invalid project name"}), 400

    if not project:
        # Same fallback chain the session routes use: active project first,
        # then auto-detect from the server's own repo path.  When a project
        # IS passed explicitly we deliberately do NOT fall back — searching
        # a different project than requested would be silently wrong.
        project = get_active_project() or _sessions_dir().name

    try:
        ensure_index(project)
        result = search(project, q=q, file_filter=file_filter, limit=limit)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        logger.exception("search failed (project=%s)", project)
        return jsonify({"error": "search failed — see server log"}), 500
    return jsonify(result)
