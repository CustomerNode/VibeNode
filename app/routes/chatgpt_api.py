"""
ChatGPT bridge routes — a standalone page + API that relays a prompt you type
in VibeNode to your logged-in ChatGPT (via Playwright) and returns the reply.

Endpoints:
    GET  /chatgpt              -> the standalone bridge page
    GET  /api/chatgpt/status   -> {logged_in, busy}
    POST /api/chatgpt/login    -> opens a visible window for manual ChatGPT login
    POST /api/chatgpt/ask      -> {prompt, headless?} -> {result}

All browser work runs in the request thread (Flask is in async_mode='threading',
so Playwright's sync API is safe here) and is serialized by a lock in
``app.chatgpt_bridge``.
"""

import logging

from flask import Blueprint, jsonify, render_template, request

from .. import chatgpt_bridge

logger = logging.getLogger(__name__)

bp = Blueprint("chatgpt", __name__)


@bp.route("/chatgpt")
def chatgpt_page():
    return render_template("chatgpt.html")


@bp.route("/api/chatgpt/status")
def chatgpt_status():
    return jsonify(chatgpt_bridge.status())


@bp.route("/api/chatgpt/login", methods=["POST"])
def chatgpt_login():
    return jsonify(chatgpt_bridge.open_login())


@bp.route("/api/chatgpt/ask", methods=["POST"])
def chatgpt_ask():
    data = request.get_json(silent=True) or {}
    prompt = data.get("prompt", "")
    files = data.get("files") or []
    if not isinstance(files, list):
        return jsonify({"ok": False, "result": None,
                        "error": "'files' must be a list of file paths."}), 400
    result = chatgpt_bridge.ask(prompt, files=files)
    status_code = 200 if result.get("ok") else 502
    return jsonify(result), status_code
