"""
Main route -- serves the single-page application.
"""

import os
import subprocess
import sys

from flask import Blueprint, jsonify, render_template, request

bp = Blueprint('main', __name__)


@bp.route("/")
def index():
    return render_template('index.html')


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

        run_py = os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "run.py")
        run_py = os.path.abspath(run_py)
        project_dir = os.path.dirname(run_py)

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
                f"Start-Process -FilePath '{pythonw}' -ArgumentList '\"{run_py}\"' "
                f"-WorkingDirectory '{project_dir}'"
                "\""
            )
            subprocess.Popen(
                restart_cmd,
                shell=True,
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
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
                f"nohup {env_prefix}\"{sys.executable}\" \"{run_py}\" "
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
                f"nohup {env_prefix}\"{sys.executable}\" \"{run_py}\" "
                f"> /dev/null 2>&1 &'"
            )
            subprocess.Popen(restart_cmd, shell=True)

        return jsonify({"ok": True, "message": f"Restarting ({scope})..."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
