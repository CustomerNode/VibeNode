"""
SpeechNode HTTP API — ``/api/speechnode/...``

User-initiated, localhost-bound endpoints for VibeNode's opt-in local voice
engine. Nothing here imports the heavy ASR dependency at module load — the
``..speechnode`` package keeps that lazy — so registering this blueprint adds
no startup cost.

Endpoints
---------
GET  /api/speechnode/status      -> engine status (deps, phase, progress, ready)
POST /api/speechnode/install     -> start (or resume) install in the background
POST /api/speechnode/transcribe  -> multipart audio -> biased, cleaned transcript
"""

from __future__ import annotations

import logging
import os
import re
import tempfile

from flask import Blueprint, jsonify, request

from ..speechnode import engine, knowledge, postprocess

logger = logging.getLogger("app.speechnode")
bp = Blueprint("speechnode", __name__)


@bp.route("/api/speechnode/status", methods=["GET"])
def status():
    return jsonify(engine.get_status())


@bp.route("/api/speechnode/syscheck", methods=["GET"])
def syscheck():
    return jsonify(engine.system_check())


@bp.route("/api/speechnode/install", methods=["POST"])
def install():
    data = request.get_json(silent=True) or {}
    model = data.get("model")
    return jsonify(engine.start_install(model))


@bp.route("/api/speechnode/transcribe", methods=["POST"])
def transcribe():
    """
    Accept a recorded audio blob and return a biased, cleaned transcript.

    Form fields:
        audio  (file)  — the recording (webm/ogg/mp4/wav; decoded server-side)
        cwd    (str)   — optional project dir to bias vocabulary toward
        extra  (str)   — optional extra bias terms (comma/space separated)
    """
    if "audio" not in request.files:
        return jsonify({"ok": False, "error": "No audio uploaded."}), 400

    f = request.files["audio"]
    cwd = request.form.get("cwd") or None
    extra_raw = request.form.get("extra") or ""
    extra_terms = [t for t in re.split(r"[,\s]+", extra_raw) if t][:30] or None
    fast = request.form.get("fast") in ("1", "true", "True")  # low-latency streaming partial

    suffix = os.path.splitext(f.filename or "")[1] or ".webm"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        f.save(tmp.name)
        tmp.close()
        bias = knowledge.build_bias_prompt(cwd=cwd, extra_terms=extra_terms)
        result = engine.transcribe(tmp.name, initial_prompt=bias, fast=fast)
        text = postprocess.clean(result["text"])
        return jsonify({"ok": True, "text": text, "raw": result["text"], "gap": result["gap"]})
    except Exception as e:  # noqa: BLE001 — report, let the client fall back
        logger.warning("SpeechNode transcribe failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass
