"""E2E test fixtures — spins up an isolated VibeNode server for Selenium tests.

SAFETY:
- Tests spin up a SEPARATE server on port 5099 with its own sqlite DB.
- The user's running instance on :5050 is NEVER touched.
- The user's kanban_config.json is NEVER touched.
- The user's Supabase is NEVER touched.
- The user's tasks are NEVER touched.
- Test cleanup only deletes task IDs the tests created.

Running E2E Tests
=================
Prerequisites:
  pip install -r requirements-test.txt
  Chrome browser installed (chromedriver managed automatically by webdriver-manager)

Run all E2E tests:
  pytest tests/e2e -m e2e -v --timeout=300

Run a single test file:
  pytest tests/e2e/test_selenium_kanban.py -m e2e -v

Test server:
  The E2E conftest automatically starts an isolated VibeNode server on port 5099
  with its own SQLite DB in a temp directory. Your running instance on :5050 is
  never touched.

Artifacts:
  Screenshots on failure are saved to tests/screenshots/ (gitignored).
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
import pytest
from pathlib import Path
from selenium import webdriver

TEST_PORT = 5099
TEST_DAEMON_PORT = 5098
TEST_BASE_URL = f"http://localhost:{TEST_PORT}"
SCREENSHOT_DIR = Path(__file__).resolve().parent.parent / "screenshots"

_test_server_proc = None
_test_tmpdir = None


# ------------------------------------------------------------------
# Shared driver fixture — all E2E test files use this (no duplicates)
# ------------------------------------------------------------------

@pytest.fixture(scope="class")
def driver():
    """Shared headless Chrome driver for E2E tests.

    Scope is 'class' so each test class gets a fresh browser — one class
    failing cannot cascade to another.  Individual test files should NOT
    define their own driver() fixture.
    """
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1400,900")
    d = webdriver.Chrome(options=options)
    yield d
    d.quit()


# ------------------------------------------------------------------
# Screenshot capture on failure
# ------------------------------------------------------------------

@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Capture screenshot and browser console logs on test failure."""
    outcome = yield
    report = outcome.get_result()
    if report.when == "call" and report.failed:
        driver = item.funcargs.get("driver")
        if driver:
            SCREENSHOT_DIR.mkdir(exist_ok=True)
            name = item.nodeid.replace("::", "_").replace("/", "_").replace("\\", "_")
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


def pytest_configure(config):
    """Spin up an isolated test server. Touches NOTHING on the user's instance."""
    global _test_server_proc, _test_tmpdir

    # Create temp dir with its own config
    _test_tmpdir = tempfile.mkdtemp(prefix="vibenode_test_")
    test_config = Path(_test_tmpdir) / "kanban_config.json"
    test_config.write_text(json.dumps({
        "kanban_backend": "sqlite",
        "kanban_depth_limit": 5,
    }, indent=2), encoding="utf-8")

    # Start test server on a separate port with its own config
    repo_root = Path(__file__).resolve().parent.parent.parent
    env = os.environ.copy()
    env["VIBENODE_CONFIG"] = str(test_config)
    env["VIBENODE_TEST_PORT"] = str(TEST_PORT)
    env["VIBENODE_DAEMON_PORT"] = str(TEST_DAEMON_PORT)

    _test_server_proc = subprocess.Popen(
        ["python", str(repo_root / "run.py")],
        cwd=str(repo_root),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )

    # Wait for it
    import urllib.request
    for _ in range(30):
        try:
            urllib.request.urlopen(f"http://localhost:{TEST_PORT}/", timeout=2)
            return
        except Exception:
            time.sleep(1)
    pytest.exit("Test server failed to start on port %d" % TEST_PORT)


def pytest_unconfigure(config):
    """Kill test server, test daemon, and clean up temp files."""
    global _test_server_proc, _test_tmpdir
    if _test_server_proc:
        _test_server_proc.terminate()
        try:
            _test_server_proc.wait(timeout=5)
        except Exception:
            _test_server_proc.kill()
        _test_server_proc = None
    # Kill the test daemon on TEST_DAEMON_PORT
    try:
        import socket as _sock
        s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
        s.settimeout(1)
        s.connect(("127.0.0.1", TEST_DAEMON_PORT))
        s.sendall(b'{"method":"shutdown"}\n')
        s.close()
    except Exception:
        pass
    if _test_tmpdir:
        shutil.rmtree(_test_tmpdir, ignore_errors=True)
        _test_tmpdir = None
