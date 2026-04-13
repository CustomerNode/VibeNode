"""E2E test fixtures — spins up an isolated VibeNode server for Selenium tests.

SAFETY — READ THIS FIRST
=========================
These tests are SAFE.  They spin up a SEPARATE server stack on different
ports from your running VibeNode instance:

  - Test web server:  port 5099  (your instance runs on 5050)
  - Test daemon:      port 5098  (your instance runs on 5051)
  - Test config:      temp directory (your kanban_config.json is untouched)
  - Test database:    temp SQLite (your Supabase is untouched)

Nothing in your running instance is ever touched, queried, or modified.

RUNNING E2E TESTS
=================
Prerequisites:
  pip install -r requirements-test.txt
  Chrome browser installed

Run all E2E tests:
  pytest tests/e2e -m e2e -v --timeout=300

Run a single test file:
  pytest tests/e2e/test_selenium_rewind.py -m e2e -v

Skip E2E tests:
  SKIP_E2E=1 pytest tests/e2e -m e2e

ARTIFACTS
=========
  - Screenshots on failure: tests/screenshots/*.png  (gitignored)
  - Browser console logs:   tests/screenshots/*.log  (gitignored)
  - Test server/daemon logs: tests/screenshots/test_server.log (gitignored)

TROUBLESHOOTING
===============
  - ARM64 Windows: Selenium can't auto-download chromedriver.  Download
    manually from https://googlechromelabs.github.io/chrome-for-testing/
    and place in ~/chromedriver/chromedriver-win64/chromedriver.exe

  - "Daemon not running": Check tests/screenshots/test_daemon.log for
    startup errors.  Common cause: port 5098 already in use.

  - "Web server not starting": Check tests/screenshots/test_server.log.
    Common cause: port 5099 already in use.
"""

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import pytest
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.chrome.service import Service

# ---------------------------------------------------------------------------
# Test server ports — deliberately different from production (5050/5051)
# ---------------------------------------------------------------------------
TEST_PORT = 5099
TEST_DAEMON_PORT = 5098
TEST_BASE_URL = f"http://localhost:{TEST_PORT}"

# Where to save failure screenshots and logs
SCREENSHOT_DIR = Path(__file__).resolve().parent.parent / "screenshots"

# Process handles for cleanup
_test_server_proc = None
_test_daemon_proc = None
_test_tmpdir = None
_daemon_fh = None
_server_fh = None


# ===========================================================================
# Driver fixture — shared by ALL E2E test files
# ===========================================================================
# IMPORTANT: Individual test files must NOT define their own driver() fixture.
# This single fixture ensures consistent Chrome options across all tests.

@pytest.fixture(scope="class")
def driver():
    """Headless Chrome driver for E2E tests.

    Scope is 'class' — each test class gets a fresh browser instance.
    This prevents one class's failures from cascading to another while
    keeping tests within a class fast (shared browser state).
    """
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1400,900")

    # --- ARM64 Windows workaround ---
    # Selenium Manager doesn't support win32/arm64.  On ARM Windows, look
    # for a manually-installed chromedriver in well-known locations.
    service = None
    if sys.platform == "win32" and platform.machine().lower() in ("arm64", "aarch64"):
        _candidates = [
            Path.home() / "chromedriver" / "chromedriver-win64" / "chromedriver.exe",
            Path.home() / "chromedriver" / "chromedriver.exe",
            Path.home() / "chromedriver.exe",
        ]
        for p in _candidates:
            if p.is_file():
                service = Service(executable_path=str(p))
                break
        if service is None:
            pytest.skip(
                "ARM64 Windows detected but no chromedriver found. "
                "Download from https://googlechromelabs.github.io/chrome-for-testing/ "
                f"and place in one of: {[str(c) for c in _candidates]}"
            )

    if service:
        d = webdriver.Chrome(service=service, options=options)
    else:
        d = webdriver.Chrome(options=options)
    yield d
    d.quit()


# ===========================================================================
# Screenshot capture on test failure
# ===========================================================================

@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Automatically capture screenshot + browser logs when a test fails.

    Screenshots are saved to tests/screenshots/ (gitignored) with the test
    node ID as filename.  This is invaluable for debugging headless failures.
    """
    outcome = yield
    report = outcome.get_result()
    if report.when == "call" and report.failed:
        driver = item.funcargs.get("driver")
        if driver:
            SCREENSHOT_DIR.mkdir(exist_ok=True)
            name = (
                item.nodeid
                .replace("::", "_")
                .replace("/", "_")
                .replace("\\", "_")
            )
            path = SCREENSHOT_DIR / f"{name}.png"
            try:
                driver.save_screenshot(str(path))
            except Exception:
                pass  # Don't fail the test because screenshot failed
            try:
                log_path = path.with_suffix(".log")
                logs = driver.get_log("browser")
                log_path.write_text(
                    "\n".join(f"[{e['level']}] {e['message']}" for e in logs),
                    encoding="utf-8",
                )
            except Exception:
                pass


# ===========================================================================
# Test server & daemon lifecycle
# ===========================================================================

def _kill_port(port):
    """Kill any process listening on a port.  Needed to clean up stale test
    servers/daemons from a previous run that didn't shut down cleanly.

    Only kills processes on the TEST ports (5098/5099) — never touches
    production ports (5050/5051).
    """
    assert port in (TEST_PORT, TEST_DAEMON_PORT), (
        f"Refusing to kill process on non-test port {port}"
    )
    try:
        if sys.platform == "win32":
            r = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, text=True, timeout=5,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            for line in r.stdout.splitlines():
                if f":{port} " in line and "LISTENING" in line:
                    parts = line.split()
                    pid = int(parts[-1])
                    if pid > 0 and pid != os.getpid():
                        subprocess.run(
                            ["taskkill", "/PID", str(pid), "/F"],
                            capture_output=True, timeout=5,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                        )
        else:
            # macOS / Linux
            r = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True, timeout=5,
            )
            for pid_str in r.stdout.split():
                try:
                    pid = int(pid_str)
                    if pid > 0 and pid != os.getpid():
                        subprocess.run(
                            ["kill", "-9", str(pid)],
                            capture_output=True, timeout=5,
                        )
                except ValueError:
                    continue
    except Exception:
        pass  # Best-effort cleanup


def pytest_configure(config):
    """Start an isolated test server + daemon.  Touches NOTHING on the user's instance.

    This runs once at the start of the pytest session.  It:
    1. Kills any stale test processes from a previous run
    2. Creates a temp directory with its own kanban config
    3. Starts the test daemon on port 5098
    4. Starts the test web server on port 5099
    5. Waits for both to be ready before letting tests run
    """
    global _test_server_proc, _test_daemon_proc, _test_tmpdir, _daemon_fh, _server_fh
    import socket as _sock

    SCREENSHOT_DIR.mkdir(exist_ok=True)
    repo_root = Path(__file__).resolve().parent.parent.parent

    # Kill any stale test processes from a previous run
    _kill_port(TEST_DAEMON_PORT)
    _kill_port(TEST_PORT)
    time.sleep(0.5)  # Brief pause for ports to release

    # --- Create temp directory with isolated config ---
    _test_tmpdir = tempfile.mkdtemp(prefix="vibenode_test_")
    test_config = Path(_test_tmpdir) / "kanban_config.json"
    test_config.write_text(json.dumps({
        "kanban_backend": "sqlite",
        "kanban_depth_limit": 5,
    }, indent=2), encoding="utf-8")

    # Environment variables that tell run.py and daemon_server.py to use
    # test ports and the temp config
    env = os.environ.copy()
    env["VIBENODE_CONFIG"] = str(test_config)
    env["VIBENODE_TEST_PORT"] = str(TEST_PORT)
    env["VIBENODE_DAEMON_PORT"] = str(TEST_DAEMON_PORT)

    _creation_flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

    # --- Start test daemon on TEST_DAEMON_PORT (5098) ---
    daemon_script = repo_root / "daemon" / "daemon_server.py"
    daemon_log = SCREENSHOT_DIR / "test_daemon.log"
    _daemon_fh = open(daemon_log, "w", encoding="utf-8")
    _test_daemon_proc = subprocess.Popen(
        [sys.executable, str(daemon_script)],
        cwd=str(repo_root),
        env=env,
        stdout=_daemon_fh,
        stderr=_daemon_fh,
        creationflags=_creation_flags,
    )

    # Wait for daemon to accept connections (up to 15 seconds)
    for _ in range(30):
        try:
            s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
            s.settimeout(1)
            s.connect(("127.0.0.1", TEST_DAEMON_PORT))
            s.close()
            break
        except (ConnectionRefusedError, OSError):
            time.sleep(0.5)
    else:
        pytest.exit(
            f"Test daemon failed to start on port {TEST_DAEMON_PORT}. "
            f"Check {daemon_log} for errors."
        )

    # --- Start test web server on TEST_PORT (5099) ---
    server_log = SCREENSHOT_DIR / "test_server.log"
    _server_fh = open(server_log, "w", encoding="utf-8")
    _test_server_proc = subprocess.Popen(
        [sys.executable, str(repo_root / "run.py")],
        cwd=str(repo_root),
        env=env,
        stdout=_server_fh,
        stderr=_server_fh,
        creationflags=_creation_flags,
    )

    # Wait for web server to respond (up to 30 seconds)
    import urllib.request
    for _ in range(30):
        try:
            urllib.request.urlopen(f"http://localhost:{TEST_PORT}/", timeout=2)
            return  # Ready!
        except Exception:
            time.sleep(1)
    pytest.exit(
        f"Test web server failed to start on port {TEST_PORT}. "
        f"Check {server_log} for errors."
    )


def pytest_unconfigure(config):
    """Kill test server + daemon and clean up temp files.

    This runs after ALL tests finish (pass or fail).  We terminate
    gracefully first, then force-kill if needed.
    """
    global _test_server_proc, _test_daemon_proc, _test_tmpdir, _daemon_fh, _server_fh

    for label, proc in [("web", _test_server_proc), ("daemon", _test_daemon_proc)]:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    _test_server_proc = None
    _test_daemon_proc = None

    # Close log file handles so we don't leak descriptors
    for fh in (_server_fh, _daemon_fh):
        if fh:
            try:
                fh.close()
            except Exception:
                pass
    _server_fh = None
    _daemon_fh = None

    if _test_tmpdir:
        shutil.rmtree(_test_tmpdir, ignore_errors=True)
        _test_tmpdir = None
