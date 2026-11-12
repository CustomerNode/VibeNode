"""
Test runner API — runs pytest and streams results via SSE.

Two modes, both running the SAME files: unit, API and headless-browser
(Playwright) tests are all in tests/ and run in both modes.  What differs:
  - fast: 60s per-test timeout
  - full: 120s per-test timeout, so a slow browser test is not cut short
The honest labels are in MODES below and reach the browser in the "run" and
"done" events.

The run belongs to the SERVER, not to the request that streams it, so a
dropped connection (a phone locking its screen mid-Publish) never ends it and
never leaves the dialog stuck on "running".  A background thread runs pytest and collects its
output; a stream attaches to the run and can REATTACH after a dropout with the
run id and the number of lines it already has, receiving the rest including
the final result.  Only /api/cancel-tests stops a run.  The browser side is
_streamTestRun() in src/web/static/js/test-runner.js.  Tests:
tests/test_publish_test_runner.py.
"""

import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from flask import Blueprint, Response, jsonify, request

bp = Blueprint('test_api', __name__)
log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
from .. import gate_reuse
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
# part of what an Update publishes, so its tests do not gate one.  They were 61% of a local run's time.  A plain `pytest` of the
# whole suite still runs them.
CODE_PIPELINE_TESTS = "--ignore-glob=tests/test_code_pipeline*.py"
PUBLISH_LOCK_WAIT_S = 600  # how long the app's run waits for another session's parallel run

# What each mode really runs.  Shown in the Publish dialog (src/web/static/js/git-sync.js)
# and recorded in the commit message's "Publish gate" line.
MODES = {
    "fast": {
        "label": "Fast: unit + browser tests, 60s timeout (Code Pipeline tests not included)",
        "pytest_args": [CODE_PIPELINE_TESTS, "--timeout=60"],
    },
    "full": {
        "label": "Full: unit + browser tests, 120s timeout (Code Pipeline tests not included)",
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
    starve the live VibeNode it is testing (``src/shared/test_workers.py``, shared
    with a plain ``pytest``).  Without xdist the run is serial, exactly as
    before.
    """
    try:
        import xdist  # noqa: F401
    except Exception:
        return []
    # On Linux the suite's own conftest forks its workers from one collected
    # process (tests/_fork_runner.py), which a whole-suite run gets by default.
    if sys.platform.startswith("linux"):
        return []
    from shared.test_workers import worker_count
    return ["-n", str(worker_count()), "--dist", "loadfile"]


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
            "source": run.get("source", "publish"),
            # The A/B tests were left out because nothing they depend on
            # changed since this passing run (src/web/gate_reuse.py); the
            # commit's gate line says so.
            "ab_reused_from": run.get("ab_reused_from") or "",
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

    res = start_run(mode)
    if "active" in res:
        return _stream(res["active"], 0)
    if "error" in res:
        return _error_stream(res["error"], busy=bool(res.get("busy")))
    return _stream(res["run"], 0)


def run_in_progress() -> bool:
    with _test_lock:
        return _test_proc is not None and _test_proc.poll() is None


def _reuse_plan():
    """What src/web/gate_reuse.py says may be reused for the tree as it is now;
    {} when reuse is off or the check itself fails (then everything runs)."""
    if not gate_reuse.enabled():
        return {}
    try:
        return gate_reuse.plan(_REPO_ROOT)
    except Exception as e:
        log.warning("Test reuse check failed; running everything: %s", e)
        return {}


def _reused_run(mode, rec, source):
    """A run that is done before it starts: a recorded whole-suite run passed
    on files identical to the working tree (src/web/gate_reuse.py)."""
    label = gate_reuse.when_label(float(rec["finished"]))
    run = {"id": uuid.uuid4().hex[:12], "mode": mode, "cmd": [], "lines": [],
           "done": None, "cancelled": False, "started_at": time.time(), "source": source}
    run["lines"].append(
        "Reused the full run that passed at %s on identical files (%d passed, %d skipped)."
        % (label, int(rec.get("passed") or 0), int(rec.get("skipped") or 0)))
    run["done"] = {
        "type": "done", "run_id": run["id"], "mode": mode, "mode_label": mode_label(mode),
        "seconds": 0, "exit_code": 0,
        "passed": int(rec.get("passed") or 0), "failed": 0, "errors": 0,
        "skipped": int(rec.get("skipped") or 0),
        "ok": True, "cancelled": False, "log_path": "", "source": source,
        "reused_from": label, "reused_run_at": rec["finished"], "ab_reused_from": "",
    }
    return run


def start_run(mode, source="publish"):
    """Start a test run, reuse a recorded one, or join the run in progress.

    Returns ``{"run": run}`` for a run this call started (already finished
    when it reused a recorded run), ``{"active": run}`` when a run already in
    progress covers the request, or ``{"error": message, "busy": bool}``.
    ``source`` is "publish" (the dialog) or "prewarm" (src/web/test_prewarm.py)
    and is written into the run's record.
    """
    global _test_proc, _run
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
                return {"active": active}
            running = active["mode"] if active is not None else "a"
            return {"error": f"A {running} test run is already in progress. "
                             "Wait for it to finish, or cancel it.", "busy": True}

        plan = _reuse_plan()
        if plan.get("whole"):
            run = _reused_run(mode, plan["whole"], source)
            with _run_cond:
                _run = run
            log.info("Test run reused: mode=%s, run=%s, from %s", mode, run["id"],
                     run["done"]["reused_from"])
            return {"run": run}

        cmd = [_console_python(), "-m", "pytest", "--tb=short", "-q", "--no-header"]
        cmd += MODES[mode]["pytest_args"]
        cmd += _parallel_args()
        env = dict(os.environ, VIBENODE_TEST_SOURCE=source)
        # The app's own run waits for another parallel run on this machine
        # (src/shared/test_run_lock.py), but not the lock's default hour, so
        # a Publish never sits behind other runs with only a spinner.  After this it
        # runs alongside them (slower, but moving), and the dialog shows the
        # wait meanwhile.
        env.setdefault("VIBENODE_TEST_RUN_LOCK_WAIT", str(PUBLISH_LOCK_WAIT_S))
        ab_label = ""
        first_lines = []
        if plan.get("ab"):
            # Nothing the A/B tests depend on changed since that passing run:
            # leave them out, and let the run's own record say they were reused.
            ab_label = gate_reuse.when_label(float(plan["ab"]["finished"]))
            cmd += ["--ignore=" + f for f in gate_reuse.AB_TESTS]
            env["VIBENODE_TEST_AB_REUSED"] = ab_label
            first_lines.append("A/B tests reused from %s: nothing they depend on changed." % ab_label)
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
                env=env,
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
            return {"error": str(e)}
        _test_proc = proc
        run = {"id": uuid.uuid4().hex[:12], "mode": mode, "cmd": cmd,
               "lines": first_lines, "done": None, "cancelled": False,
               "started_at": time.time(), "source": source,
               "ab_reused_from": ab_label}
        with _run_cond:
            _run = run

    log.info("Test run started: mode=%s, pid=%s, run=%s, source=%s%s", mode, proc.pid,
             run["id"], source, (", A/B tests reused from " + ab_label) if ab_label else "")
    threading.Thread(target=_pump, args=(run, proc), name="test-run-pump",
                     daemon=True).start()
    return {"run": run}


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
