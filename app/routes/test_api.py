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


LAST_RUN_LOG = _REPO_ROOT / "logs" / "last_test_run.log"  # logs/ is gitignored
LAST_RUN_JUNIT = _REPO_ROOT / "logs" / "last_test_run.xml"


def _junit_counts():
    """Exact counts from pytest's JUnit report, or None if unavailable.

    The progress-dot count below is approximate: daemon log lines print in the
    middle of the dot rows (worse with parallel workers), so it undercounts.
    """
    try:
        import xml.etree.ElementTree as ET
        root = ET.parse(str(LAST_RUN_JUNIT)).getroot()
        suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
        tot = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
        for s in suites:
            for k in tot:
                tot[k] += int(s.get(k, 0) or 0)
        return {
            "passed": tot["tests"] - tot["failures"] - tot["errors"] - tot["skipped"],
            "failed": tot["failures"],
            "errors": tot["errors"],
            "skipped": tot["skipped"],
        }
    except Exception:
        return None


def _console_python():
    """The interpreter to run tests with: python.exe, never pythonw.exe.

    The web server runs under pythonw (no console).  pythonw's standard
    streams are unusable, which breaks pytest-xdist workers (they talk to the
    parent over stdio and die with "couldn't load message header") and any
    test that inherits them.  Use the same environment's python.exe instead;
    the NO_WINDOW creation flag still keeps a console from appearing.
    """
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        console = exe.with_name("python.exe")
        if console.exists():
            return str(console)
    return sys.executable


def _parallel_args():
    """Run the suite across CPU cores when pytest-xdist is installed.

    ``--dist loadfile`` keeps every test file on one worker: many daemon test
    files share module-level state inside a file (fake-SDK reloads, one
    SessionManager per fixture), so splitting a file across workers is where
    parallel runs get flaky.  Workers are capped so a big machine does not
    starve the live VibeNode it is testing.  Without xdist the run is serial,
    exactly as before.
    """
    try:
        import xdist  # noqa: F401
    except Exception:
        return []
    import os
    workers = max(2, min(8, (os.cpu_count() or 2) - 2))
    return ["-n", str(workers), "--dist", "loadfile"]


def _save_run_log(mode, cmd, lines, exit_code):
    """Write the complete output of the last run to logs/last_test_run.log.

    The publish dialog only shows FAILED lines; the "Fix errors" session
    needs the full tracebacks, which can be far larger than a prompt should
    carry.  Returns the absolute path, or "" if the write failed.
    """
    try:
        LAST_RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
        header = [
            "# VibeNode test run (mode=%s, exit=%s)" % (mode, exit_code),
            "# command: " + " ".join(str(c) for c in cmd),
            "",
        ]
        LAST_RUN_LOG.write_text("\n".join(header + list(lines)) + "\n", encoding="utf-8")
        return str(LAST_RUN_LOG)
    except OSError as e:
        log.warning("Could not save test run log: %s", e)
        return ""


@bp.route("/api/run-tests", methods=["POST"])
def api_run_tests():
    """Run tests and stream results via SSE.

    POST body: {"mode": "fast"|"full"}
    Returns SSE stream with line-by-line pytest output and a final summary.
    """
    mode = (request.get_json() or {}).get("mode", "fast")
    if mode not in ("fast", "full"):
        mode = "fast"

    cmd = [_console_python(), "-m", "pytest", "--tb=short", "-q", "--no-header"]

    if mode == "fast":
        cmd += ["--ignore=tests/e2e", "--timeout=60"]
    else:
        cmd += ["--timeout=120"]
    cmd += _parallel_args()
    try:
        LAST_RUN_JUNIT.unlink()
    except OSError:
        pass
    cmd += ["--junitxml", str(LAST_RUN_JUNIT)]

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
                    # REQUIRED on Windows: the web server runs under pythonw,
                    # which has no console and therefore no valid stdin.
                    # Inherited, that invalid handle reaches every subprocess
                    # a test starts (git, the security scanner, ...) and
                    # Windows refuses them with "[WinError 6] The handle is
                    # invalid".  That made 9 tests fail ONLY via Publish while
                    # passing from a terminal.  Do not remove.
                    stdin=subprocess.DEVNULL,
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
        all_lines = []  # full output, saved for the "Fix errors" session

        try:
            for raw_line in proc.stdout:
                line = raw_line.rstrip("\n\r")
                all_lines.append(line)
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

            exact = _junit_counts()
            if exact:
                passed, failed = exact["passed"], exact["failed"]
                errors, skipped = exact["errors"], exact["skipped"]
            log_path = _save_run_log(mode, cmd, all_lines, proc.returncode if proc else -1)
            summary = {
                "type": "done",
                "exit_code": proc.returncode if proc else -1,
                "passed": passed,
                "failed": failed,
                "errors": errors,
                "skipped": skipped,
                "ok": proc.returncode == 0 if proc else False,
                "log_path": log_path,
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
