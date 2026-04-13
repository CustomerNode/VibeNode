"""
Main route -- serves the single-page application.
"""

import os
import subprocess
import sys

from flask import Blueprint, jsonify, render_template, request, send_from_directory
from ..platform_utils import NO_WINDOW as _NO_WINDOW

bp = Blueprint('main', __name__)


@bp.route("/")
def index():
    return render_template('index.html')


@bp.route("/api/docs")
def api_docs():
    """Serve the API documentation page (Redoc)."""
    docs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'docs', 'api')
    return send_from_directory(docs_dir, 'index.html')


@bp.route("/api/docs/<path:filename>")
def api_docs_assets(filename):
    """Serve supporting API doc files (openapi.yaml, etc.)."""
    docs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'docs', 'api')
    return send_from_directory(docs_dir, filename)


@bp.route("/api/restart", methods=["POST"])
def restart_server():
    """Restart the web server, daemon, or both based on the `scope` param.

    Body JSON (optional):
      scope: "web" (default) | "daemon" | "both"

    "web"    — kills only port 5050, leaves daemon alive
    "daemon" — kills only port 5051, leaves web alive
    "both"   — kills both 5050 and 5051
    """
    try:
        data = request.get_json(silent=True) or {}
        scope = data.get("scope", "web")

        ports = []
        if scope in ("web", "both"):
            ports.append(5050)
        if scope in ("daemon", "both"):
            ports.append(5051)
        if not ports:
            ports = [5050]

        # Launch via session_manager.py (not run.py) so the boot splash shows
        entry_script = os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "session_manager.py")
        entry_script = os.path.abspath(entry_script)
        project_dir = os.path.dirname(entry_script)

        # When doing a web-only restart, tell run.py to preserve the daemon
        # so active sessions and their CLI subprocesses are not killed.
        preserve_daemon = scope == "web"

        if sys.platform == "win32":
            pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            if not os.path.exists(pythonw):
                pythonw = sys.executable

            port_list = ",".join(str(p) for p in ports)
            env_set = "$env:VIBENODE_PRESERVE_DAEMON='1'; " if preserve_daemon else ""
            restart_cmd = (
                "powershell -NoProfile -Command \""
                "$maxTries = 10; "
                "for ($i = 0; $i -lt $maxTries; $i++) { "
                f"  $pids = @(Get-NetTCPConnection -LocalPort {port_list} -ErrorAction SilentlyContinue | "
                "    Select-Object -ExpandProperty OwningProcess -Unique); "
                "  if ($pids.Count -eq 0) { break }; "
                "  $pids | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }; "
                "  Start-Sleep -Milliseconds 500 "
                "}; "
                f"Get-ChildItem -Path '{project_dir}' -Recurse -Directory -Filter '__pycache__' | "
                "Remove-Item -Recurse -Force -ErrorAction SilentlyContinue; "
                "Start-Sleep -Seconds 1; "
                f"{env_set}"
                f"Start-Process -FilePath '{pythonw}' -ArgumentList '\"{entry_script}\"' "
                f"-WorkingDirectory '{project_dir}'"
                "\""
            )
            subprocess.Popen(
                restart_cmd,
                shell=True,
                creationflags=_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            )

        elif sys.platform == "darwin":
            kill_cmds = " ".join(
                f"lsof -ti :{p} | xargs kill -9 2>/dev/null;" for p in ports
            )
            env_prefix = "VIBENODE_PRESERVE_DAEMON=1 " if preserve_daemon else ""
            restart_cmd = (
                f"bash -c '"
                f"for i in $(seq 1 10); do {kill_cmds} sleep 0.5; done; "
                f"find \"{project_dir}\" -type d -name __pycache__ -exec rm -rf {{}} + 2>/dev/null; "
                f"sleep 1; "
                f"nohup {env_prefix}\"{sys.executable}\" \"{entry_script}\" "
                f"> /dev/null 2>&1 &'"
            )
            subprocess.Popen(restart_cmd, shell=True)

        elif sys.platform == "linux":
            kill_cmds = " ".join(
                f"lsof -ti :{p} | xargs kill -9 2>/dev/null;" for p in ports
            )
            env_prefix = "VIBENODE_PRESERVE_DAEMON=1 " if preserve_daemon else ""
            restart_cmd = (
                f"bash -c '"
                f"for i in $(seq 1 10); do {kill_cmds} sleep 0.5; done; "
                f"find \"{project_dir}\" -type d -name __pycache__ -exec rm -rf {{}} + 2>/dev/null; "
                f"sleep 1; "
                f"nohup {env_prefix}\"{sys.executable}\" \"{entry_script}\" "
                f"> /dev/null 2>&1 &'"
            )
            subprocess.Popen(restart_cmd, shell=True)

        return jsonify({"ok": True, "message": f"Restarting ({scope})..."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/shutdown", methods=["POST"])
def shutdown_server():
    """Shut down both the web server and daemon without restarting.

    Sends the response first, then kills ports 5050 and 5051 after a
    short delay so the client receives the acknowledgement.
    """
    try:
        project_dir = os.path.abspath(
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "..")
        )
        ports = "5050,5051"

        if sys.platform == "win32":
            shutdown_cmd = (
                'powershell -NoProfile -Command "'
                "Start-Sleep -Seconds 2; "
                f"$pids = @(Get-NetTCPConnection -LocalPort {ports} -ErrorAction SilentlyContinue | "
                "Select-Object -ExpandProperty OwningProcess -Unique); "
                "$pids | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }"
                '"'
            )
            subprocess.Popen(
                shutdown_cmd,
                shell=True,
                creationflags=_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            shutdown_cmd = (
                "bash -c '"
                "sleep 2; "
                "lsof -ti :5050 | xargs kill -9 2>/dev/null; "
                "lsof -ti :5051 | xargs kill -9 2>/dev/null"
                "'"
            )
            subprocess.Popen(shutdown_cmd, shell=True)

        return jsonify({"ok": True, "message": "Server shutting down..."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
