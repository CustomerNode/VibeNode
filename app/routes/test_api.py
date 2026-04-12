"""
Test runner API — runs pytest and streams results via SSE.

Two modes:
  - fast: unit/mock tests only (ignores tests/e2e/)
  - full: everything including e2e/Selenium tests
"""

import json
import logging
import subprocess
import sys
import threading
from pathlib import Path

from flask import Blueprint, Response, jsonify, request

bp = Blueprint('test_api', __name__)
log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
from ..platform_utils import NO_WINDOW as _NO_WINDOW

# Track running test process so we can cancel
_test_proc = None
_test_lock = threading.Lock()


@bp.route("/api/run-tests", methods=["POST"])
def api_run_tests():
    """Run tests and stream results via SSE.

    POST body: {"mode": "fast"|"full"}
    Returns SSE stream with line-by-line pytest output and a final summary.
    """
    mode = (request.get_json() or {}).get("mode", "fast")
    if mode not in ("fast", "full"):
        mode = "fast"

    cmd = [sys.executable, "-m", "pytest", "--tb=short", "-q", "--no-header"]

    if mode == "fast":
        cmd += ["--ignore=tests/e2e", "--timeout=60"]
    else:
        cmd += ["--timeout=120"]

    cmd.append("tests/")

    def generate():
        global _test_proc
        proc = None

        # Acquire lock to check/start — hold it through Popen to prevent races
        with _test_lock:
            if _test_proc and _test_proc.poll() is None:
                yield f"data: {json.dumps({'type': 'error', 'line': 'Tests already running'})}\n\n"
                return
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(_REPO_ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    creationflags=_NO_WINDOW,
                )
                _test_proc = proc
            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'line': str(e)})}\n\n"
                return

        log.info("Test run started: mode=%s, pid=%s", mode, proc.pid)

        passed = 0
        failed = 0
        errors = 0
        skipped = 0

        try:
            for raw_line in proc.stdout:
                line = raw_line.rstrip("\n\r")
                # Count from progress dots (pytest -q output)
                for ch in line:
                    if ch == '.':
                        passed += 1
                    elif ch == 'F':
                        failed += 1
                    elif ch == 'E':
                        errors += 1
                    elif ch == 's':
                        skipped += 1

                yield f"data: {json.dumps({'type': 'line', 'line': line})}\n\n"

            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

            summary = {
                "type": "done",
                "exit_code": proc.returncode if proc else -1,
                "passed": passed,
                "failed": failed,
                "errors": errors,
                "skipped": skipped,
                "ok": proc.returncode == 0 if proc else False,
            }
            log.info("Test run complete: %s", summary)
            yield f"data: {json.dumps(summary)}\n\n"

        except GeneratorExit:
            # Client disconnected — clean up the subprocess
            log.warning("Test client disconnected, terminating test process")
        except Exception as e:
            log.error("Test run error: %s", e)
            yield f"data: {json.dumps({'type': 'error', 'line': str(e)})}\n\n"
        finally:
            # Always clean up the subprocess on any exit path
            with _test_lock:
                if proc and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        proc.kill()
                _test_proc = None

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@bp.route("/api/cancel-tests", methods=["POST"])
def api_cancel_tests():
    """Cancel a running test process."""
    global _test_proc
    with _test_lock:
        if _test_proc and _test_proc.poll() is None:
            _test_proc.terminate()
            try:
                _test_proc.wait(timeout=5)
            except Exception:
                _test_proc.kill()
            _test_proc = None
            log.info("Test run cancelled by user")
            return jsonify({"ok": True, "message": "Tests cancelled"})
    return jsonify({"ok": False, "message": "No tests running"})
