"""
Test runner API — runs pytest and streams results via SSE.

Two modes, both running the SAME files (pytest.ini's addopts carries
``--ignore=tests/e2e``, so the Selenium e2e suite never runs here; it runs in
CI, .github/workflows/e2e-tests.yml).  Unit, API and headless-browser
(Playwright) tests are all in tests/ and run in both modes.  What differs:
  - fast: 60s per-test timeout
  - full: 120s per-test timeout, so a slow browser test is not cut short
The honest labels are in MODES below and reach the browser in the "run" and
"done" events (corrected 2026-10-23; the docstring used to claim full mode
included e2e).

The run belongs to the SERVER, not to the request that streams it (changed
2026-10-18).  It used to live inside the SSE generator, so a dropped
connection ended it: a phone that locked its screen 41s into a Publish killed
the run, and the dialog, which never got its "done" event, showed "running"
for the next 20 minutes.  Now a background thread runs pytest and collects its
output; a stream attaches to the run and can REATTACH after a dropout with the
run id and the number of lines it already has, receiving the rest including
the final result.  Only /api/cancel-tests stops a run.  The browser side is
_streamTestRun() in static/js/test-runner.js.  Tests:
tests/test_publish_test_runner.py.
"""

import json
import logging
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from flask import Blueprint, Response, jsonify, request

bp = Blueprint('test_api', __name__)
log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
from ..platform_utils import NO_WINDOW as _NO_WINDOW

# Track running test process so we can cancel
_test_proc = None
_test_lock = threading.Lock()

# The current or most recent run: {"id", "mode", "cmd", "lines", "done",
# "cancelled"}.  Only the latest run is kept, so a stream can reattach to it
# (and fetch its result) until the next run starts.
_run = None
_run_cond = threading.Condition()
KEEPALIVE_S = 15  # an SSE comment this often: dead connections are noticed


LAST_RUN_LOG = _REPO_ROOT / "logs" / "last_test_run.log"  # logs/ is gitignored
LAST_RUN_JUNIT = _REPO_ROOT / "logs" / "last_test_run.xml"

# Code Pipeline is a separate tool that lives in this repository; it is not
# part of what an Update publishes, so its tests do not gate one (the owner,
# 2026-10-28: "Code pipeline should have nothing to do with pushing vibenode
# updates").  They were 61% of a local run's time.  CI still runs them on
# every push (.github/workflows/unit-tests.yml runs the whole suite).
CODE_PIPELINE_TESTS = "--ignore-glob=tests/test_code_pipeline*.py"

# What each mode really runs.  Shown in the Publish dialog (static/js/git-sync.js)
# and recorded in the commit message's "Publish gate" line.
MODES = {
    "fast": {
        "label": "Fast: unit + browser tests, 60s timeout (Code Pipeline tests run in CI)",
        "pytest_args": ["--ignore=tests/e2e", CODE_PIPELINE_TESTS, "--timeout=60"],
    },
    "full": {
        "label": "Full: unit + browser tests, 120s timeout (e2e and Code Pipeline tests run in CI)",
        "pytest_args": [CODE_PIPELINE_TESTS, "--timeout=120"],
    },
}


def mode_label(mode) -> str:
    return MODES.get(mode, MODES["fast"])["label"]


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


def _sse(obj):
    return f"data: {json.dumps(obj)}\n\n"


def _pump(run, proc):
    """Read the run's output to the end and record its result.  Runs in its
    own thread, so nothing a client does can stop it early."""
    global _test_proc
    passed = failed = errors = skipped = 0
    try:
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n\r")
            # Count from progress dots (pytest -q output); exact counts come
            # from the JUnit report below when it exists.
            for ch in line:
                if ch == '.':
                    passed += 1
                elif ch == 'F':
                    failed += 1
                elif ch == 'E':
                    errors += 1
                elif ch == 's':
                    skipped += 1
            with _run_cond:
                run["lines"].append(line)
                _run_cond.notify_all()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    except Exception as e:
        log.error("Test run error: %s", e)
        with _run_cond:
            run["lines"].append(f"[test runner error: {e}]")
    finally:
        rc = proc.poll()
        exact = _junit_counts()
        if exact:
            passed, failed = exact["passed"], exact["failed"]
            errors, skipped = exact["errors"], exact["skipped"]
        log_path = _save_run_log(run["mode"], run["cmd"], run["lines"],
                                 rc if rc is not None else -1)
        summary = {
            "type": "done",
            "run_id": run["id"],
            "mode": run["mode"],
            "mode_label": mode_label(run["mode"]),
            "seconds": int(time.time() - run.get("started_at", time.time())),
            "exit_code": rc if rc is not None else -1,
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "skipped": skipped,
            # A cancelled run never passes the publish gate, whatever its
            # exit code says.
            "ok": rc == 0 and not run.get("cancelled", False),
            "cancelled": run.get("cancelled", False),
            "log_path": log_path,
        }
        log.info("Test run complete: %s", summary)
        with _test_lock:
            if _test_proc is proc:
                _test_proc = None
        with _run_cond:
            run["done"] = summary
            _run_cond.notify_all()


def _stream(run, since):
    """SSE for one run from line ``since`` on: the run id, each line, then
    the result.  A client that goes away only ends this stream."""
    def generate():
        i = max(0, since)
        yield _sse({"type": "run", "run_id": run["id"], "mode": run["mode"],
                    "mode_label": mode_label(run["mode"])})
        try:
            while True:
                with _run_cond:
                    if i >= len(run["lines"]) and run["done"] is None:
                        _run_cond.wait(timeout=KEEPALIVE_S)
                    new = run["lines"][i:]
                    done = run["done"]
                for line in new:
                    yield _sse({"type": "line", "line": line})
                i += len(new)
                if done is not None and i >= len(run["lines"]):
                    yield _sse(done)
                    return
                if not new:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            log.info("Test stream for run %s detached at line %d; the run continues",
                     run["id"], i)
            raise

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


def _error_stream(message, **extra):
    def generate():
        yield _sse({"type": "error", "line": message, **extra})
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@bp.route("/api/run-tests", methods=["POST"])
def api_run_tests():
    """Start a test run (or attach to one) and stream it via SSE.

    POST body: {"mode": "fast"|"full"} starts a run, or attaches to the run
    already in progress when it has the same mode.
    {"run_id": ..., "since": N} reattaches to that run from line N, e.g.
    after the connection dropped; the final result is included if the run
    finished meanwhile.
    """
    global _test_proc, _run
    body = request.get_json(silent=True) or {}
    mode = body.get("mode", "fast")
    if mode not in ("fast", "full"):
        mode = "fast"

    run_id = body.get("run_id")
    if run_id:
        try:
            since = int(body.get("since") or 0)
        except (TypeError, ValueError):
            since = 0
        with _run_cond:
            run = _run if (_run is not None and _run["id"] == run_id) else None
        if run is None:
            return _error_stream("That test run is no longer available.", lost=True)
        return _stream(run, since)

    with _test_lock:
        if _test_proc and _test_proc.poll() is None:
            with _run_cond:
                active = _run
            # Join the run in progress when it covers what was asked for: a
            # full run is a fast run with a longer timeout, so a Sync asking
            # for fast tests waits on it instead of being refused.  Refusing
            # left "Tests already running" with nothing on screen to watch or
            # cancel when the run was started from another tab or device.
            if (active is not None and active["done"] is None
                    and (active["mode"] == mode or active["mode"] == "full")):
                return _stream(active, 0)
            running = active["mode"] if active is not None else "a"
            return _error_stream(f"A {running} test run is already in progress. "
                                 "Wait for it to finish, or cancel it.", busy=True)

        cmd = [_console_python(), "-m", "pytest", "--tb=short", "-q", "--no-header"]
        cmd += MODES[mode]["pytest_args"]
        cmd += _parallel_args()
        try:
            LAST_RUN_JUNIT.unlink()
        except OSError:
            pass
        cmd += ["--junitxml", str(LAST_RUN_JUNIT)]
        cmd.append("tests/")

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
        except Exception as e:
            return _error_stream(str(e))
        _test_proc = proc
        run = {"id": uuid.uuid4().hex[:12], "mode": mode, "cmd": cmd,
               "lines": [], "done": None, "cancelled": False,
               "started_at": time.time()}
        with _run_cond:
            _run = run

    log.info("Test run started: mode=%s, pid=%s, run=%s", mode, proc.pid, run["id"])
    threading.Thread(target=_pump, args=(run, proc), name="test-run-pump",
                     daemon=True).start()
    return _stream(run, 0)


@bp.route("/api/test-modes", methods=["GET"])
def api_test_modes():
    """What each Publish-gate mode really runs (labels for the dialog)."""
    return jsonify({"modes": {k: {"label": v["label"], "pytest_args": v["pytest_args"]}
                              for k, v in MODES.items()}})


@bp.route("/api/cancel-tests", methods=["POST"])
def api_cancel_tests():
    """Cancel a running test process."""
    global _test_proc
    with _test_lock:
        if _test_proc and _test_proc.poll() is None:
            with _run_cond:
                if _run is not None and _run["done"] is None:
                    _run["cancelled"] = True
            _test_proc.terminate()
            try:
                _test_proc.wait(timeout=5)
            except Exception:
                _test_proc.kill()
            _test_proc = None
            log.info("Test run cancelled by user")
            return jsonify({"ok": True, "message": "Tests cancelled"})
    return jsonify({"ok": False, "message": "No tests running"})
