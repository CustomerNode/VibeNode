"""
Entry point for the VibeNode Flask application.
Run with: python run.py
Then open: http://localhost:5050

Architecture: The Web UI (this process) connects to a separate Session Daemon
that manages Claude Code SDK sessions. The daemon survives Web UI restarts,
so running sessions continue uninterrupted when you restart the server.
"""

# ---------------------------------------------------------------------------
# Boot hardening.  MUST be the first import — before any third-party import —
# so platform.uname() is WMI-free before aiohttp's import-time
# platform.system() call.  Also arms a faulthandler autopsy: if boot wedges,
# the log gets a full stack dump automatically.  See _early_boot.py.
# ---------------------------------------------------------------------------
import _early_boot
_early_boot.arm_hang_dump(120, "web-boot")

import importlib.util
import logging
import os
import shutil
import socket
import subprocess

# ---------------------------------------------------------------------------
# Boot-splash status helper (must be defined early — before any boot phases)
# ---------------------------------------------------------------------------
# Boot watchdog state (read by _boot_watchdog).  _boot_step is the phase
# currently executing; _boot_step_ts is when it started (monotonic seconds).
_boot_step = "start"
_boot_step_ts = None

# Per-step time budgets (seconds).  Exceeding a budget means "something is
# wedged" (not a perf target), so these are deliberately generous.  ``deps`` is
# high because a cold pip install of missing packages can take ~2 min.
_BOOT_STEP_BUDGET = {
    "cache": 20, "loading": 75, "ports": 30, "deps": 150, "update": 300,
    "daemon": 45, "server": 60,
}


def _update_boot_status(msg):
    """Write a status line for the boot splash window (if running).

    The splash subprocess polls the status file for lines like:
        STEP:cache      — activate a named step
        DONE            — all steps complete, close splash
        ERROR:<text>    — show error, keep splash open with dismiss button

    Also records the current step + timestamp so the boot watchdog can turn a
    stalled step (e.g. a wedged WMI query mid-import) into a visible ERROR on
    the splash instead of an infinite silent freeze.
    """
    if msg.startswith("STEP:"):
        global _boot_step, _boot_step_ts
        _boot_step = msg[5:]
        import time as _t
        _boot_step_ts = _t.monotonic()
    _sf = os.environ.get("VIBENODE_BOOT_STATUS_FILE")
    if not _sf:
        return
    try:
        with open(_sf, "a", encoding="utf-8") as fh:
            fh.write(msg + "\n")
            fh.flush()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Purge __pycache__ on every boot so code changes always take effect
# ---------------------------------------------------------------------------
_update_boot_status("STEP:cache")
for _cache_dir in __import__('pathlib').Path(__file__).parent.rglob('__pycache__'):
    shutil.rmtree(_cache_dir, ignore_errors=True)
import sys
import threading
import time
import webbrowser
from pathlib import Path

# Test mode: VIBENODE_TEST_PORT=5099 starts a separate instance on that port
# with no port killing, no singleton, no browser, no daemon dependency.
_TEST_PORT = int(os.environ.get("VIBENODE_TEST_PORT", 0))

# Production-mode port override: VIBENODE_WEB_PORT=7050 binds web to 7050
# but performs ALL normal production setup (port killing, singleton, daemon
# spawn, browser launch).  Distinct from VIBENODE_TEST_PORT which skips
# every production setup step.  Used for side-by-side production-equivalent
# instances (e.g. running a second VibeNode on 7050/7051 to exercise the
# real ``/api/restart`` path without touching the user's main 5050/5051).
# Defaults to 5050 when unset, preserving existing behavior.
_WEB_PORT = int(os.environ.get("VIBENODE_WEB_PORT", 0)) or 5050

DAEMON_PORT = int(os.environ.get("VIBENODE_DAEMON_PORT", 5051))

# ---------------------------------------------------------------------------
# Boot watchdog.  If any step overruns its budget, convert the silent freeze
# into (a) an immediate stack dump in the log and (b) a visible ERROR on the
# splash.  Started here — right before the heavy ``app`` import, which is the
# historical freeze point — and stood down once the Flask app is built.
# ---------------------------------------------------------------------------
_boot_done = threading.Event()


def _boot_watchdog():
    while not _boot_done.wait(3):
        ts = _boot_step_ts
        if ts is None:
            continue
        elapsed = time.monotonic() - ts
        if elapsed > _BOOT_STEP_BUDGET.get(_boot_step, 45):
            print("BOOT WATCHDOG: stalled at step '%s' for %ds — dumping stacks"
                  % (_boot_step, int(elapsed)), flush=True)
            _early_boot.dump_stacks_now()
            _update_boot_status(
                "ERROR:Boot stalled at '%s' (%ds). A wedged Windows service "
                "(commonly WMI/Winmgmt) or an unreachable dependency is the "
                "usual cause — a reboot clears it. See logs/_server.log."
                % (_boot_step, int(elapsed))
            )
            return


threading.Thread(target=_boot_watchdog, name="boot-watchdog", daemon=True).start()

# STEP:loading — names the heavy import phase so the splash reflects where the
# code actually is (this gap used to display the previous step, "cache",
# which is why a freeze here looked like a stuck cache purge).
_update_boot_status("STEP:loading")
from app.singleton import acquire_web_singleton


def ensure_daemon():
    """Make sure the session daemon is running. Start it if not.

    Failure modes are LOUD on purpose.  The historic Linux bug was that
    ``Restart Server → Session Daemon`` would fail here silently: the new
    web came up without a daemon, the user saw "everything looks normal"
    in the UI, then tool calls failed mysteriously a minute later.  We
    now:

      • Print which port we're targeting and where the daemon log goes.
      • Capture the spawn's PID so we can check whether the daemon
        process actually exists (vs. died silently before bind).
      • Distinguish three failure modes in the final timeout warning:
        process-dead-no-bind, process-alive-no-bind, and spawn-never-fired.
      • Print the tail of the daemon log when timeout fires so the
        user can see the real error without hunting for log files.
    """
    # Try to connect to an existing daemon
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        sock.connect(("127.0.0.1", DAEMON_PORT))
        sock.close()
        print("  Session daemon already running on port %d" % DAEMON_PORT, flush=True)
        return
    except (ConnectionRefusedError, OSError):
        pass

    # Start daemon as a detached subprocess (daemon enforces its own singleton)
    daemon_script = Path(__file__).parent / "daemon" / "daemon_server.py"
    if not daemon_script.exists():
        print("  WARNING: daemon_server.py not found at %s" % daemon_script, flush=True)
        return

    daemon_log = Path(__file__).parent / "logs" / "daemon_debug.log"
    print("  Spawning daemon for port %d (log: %s)" % (DAEMON_PORT, daemon_log),
          flush=True)
    proc = None
    try:
        daemon_log.parent.mkdir(exist_ok=True)
        _daemon_fh = open(daemon_log, "a")
        # Mark the spawn boundary in the shared daemon log so a user reading
        # it after the fact can find "this restart's daemon" vs prior runs.
        try:
            _daemon_fh.write(
                f"\n===== ensure_daemon() spawning daemon at "
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} for port {DAEMON_PORT} "
                f"=====\n"
            )
            _daemon_fh.flush()
        except Exception:
            pass
        # Fingerprint the daemon's environment so it — and the claude CLI
        # children it spawns — carry a discoverable "managed by VibeNode,
        # do not kill" marker (see process_label.py).
        _daemon_env = dict(os.environ)
        try:
            import process_label
            _daemon_env.update(process_label.marker_env("session-daemon"))
        except Exception:
            pass
        popen_kwargs = {
            "cwd": str(daemon_script.parent.parent),
            "stdout": _daemon_fh,
            "stderr": _daemon_fh,
            # Explicit env propagation: the daemon needs VIBENODE_DAEMON_PORT
            # to bind the right port.  Inheriting parent env normally works,
            # but when this function runs from a Flask-handled /api/restart
            # path, the env is filtered by Werkzeug's WSGI environ — be
            # explicit to avoid surprises.
            "env": _daemon_env,
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            # start_new_session=True calls setsid() in the child, placing it in
            # a new process group and session independent of the launcher's TTY.
            popen_kwargs["start_new_session"] = True
        # Append an inert marker token to the daemon's command line so anyone
        # (human or AI) inspecting the process list sees a "do not kill" label.
        # The daemon never parses argv, so this is purely informational.
        _daemon_cmd = [sys.executable, str(daemon_script)]
        try:
            import process_label
            _daemon_cmd += process_label.daemon_spawn_marker_args("session-daemon")
        except Exception:
            pass
        proc = subprocess.Popen(_daemon_cmd, **popen_kwargs)
        print("  Daemon subprocess launched (pid=%d)" % proc.pid, flush=True)
    except Exception as e:
        print("  ERROR: Could not start daemon subprocess: %s" % e, flush=True)
        return

    # Wait for it to be ready.  Bumped from 50 attempts (5 s) to 100 (10 s)
    # because cold daemon startup on slower machines can take 5-7 s once SDK
    # patches + session-registry recovery are factored in.
    for _ in range(100):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            sock.connect(("127.0.0.1", DAEMON_PORT))
            sock.close()
            print("  Session daemon started on port %d (pid=%d)"
                  % (DAEMON_PORT, proc.pid if proc else -1), flush=True)
            return
        except (ConnectionRefusedError, OSError):
            # Short-circuit: if the daemon process is already dead, no
            # amount of polling will help — fail fast instead of waiting
            # the full 10 s.
            if proc is not None and proc.poll() is not None:
                print("  ERROR: Daemon process %d died before binding port "
                      "%d (exit code %s)" % (proc.pid, DAEMON_PORT, proc.returncode),
                      flush=True)
                break
            time.sleep(0.1)
    else:
        # Loop completed without binding — process is alive but stuck.
        print("  ERROR: Daemon process %d is alive but did not bind port %d "
              "within 10 s" % (proc.pid if proc else -1, DAEMON_PORT), flush=True)

    # On any failure path, dump the tail of the daemon log so the user
    # doesn't have to hunt for it.  Without this they see a useless
    # "Could not connect" message and have no idea why.
    try:
        with open(daemon_log) as fh:
            lines = fh.readlines()
        tail = lines[-30:] if len(lines) > 30 else lines
        print("  ----- last %d lines of %s -----"
              % (len(tail), daemon_log), flush=True)
        for line in tail:
            print("  | " + line.rstrip(), flush=True)
        print("  ---------------------------------", flush=True)
    except Exception as log_err:
        print("  (could not read daemon log: %s)" % log_err, flush=True)


# ---- Kill any stale processes on our ports before starting ----
def _kill_port(port):
    """Kill ALL processes listening on a port. Retries until clear."""
    import subprocess as _sp
    for attempt in range(5):
        try:
            killed_any = False
            seen_pids = set()

            if sys.platform == "win32":
                r = _sp.run(
                    ["netstat", "-ano"],
                    capture_output=True, text=True, timeout=5,
                    creationflags=_sp.CREATE_NO_WINDOW,
                )
                for line in r.stdout.splitlines():
                    if (":%d " % port) in line and "LISTENING" in line:
                        parts = line.split()
                        pid = int(parts[-1])
                        if pid > 0 and pid != os.getpid() and pid not in seen_pids:
                            seen_pids.add(pid)
                            _sp.run(["taskkill", "/PID", str(pid), "/F"],
                                    capture_output=True, timeout=5,
                                    creationflags=_sp.CREATE_NO_WINDOW)
                            print("  Killed stale process %d on port %d" % (pid, port), flush=True)
                            killed_any = True

            elif sys.platform == "darwin":
                r = _sp.run(
                    ["lsof", "-ti", ":%d" % port],
                    capture_output=True, text=True, timeout=5,
                )
                for pid_str in r.stdout.split():
                    try:
                        pid = int(pid_str)
                    except ValueError:
                        continue
                    if pid > 0 and pid != os.getpid() and pid not in seen_pids:
                        seen_pids.add(pid)
                        _sp.run(["kill", "-9", str(pid)],
                                capture_output=True, timeout=5)
                        print("  Killed stale process %d on port %d" % (pid, port), flush=True)
                        killed_any = True

            elif sys.platform == "linux":
                try:
                    r = _sp.run(
                        ["lsof", "-ti", ":%d" % port],
                        capture_output=True, text=True, timeout=5,
                    )
                except FileNotFoundError:
                    # lsof not installed — fall back to ss (iproute2, always present)
                    try:
                        r2 = _sp.run(
                            ["ss", "-tlnp", "sport", "= :%d" % port],
                            capture_output=True, text=True, timeout=5,
                        )
                        pids_from_ss = set()
                        for line in r2.stdout.splitlines():
                            for part in line.split(","):
                                part = part.strip()
                                if part.startswith("pid="):
                                    try:
                                        pids_from_ss.add(int(part[4:]))
                                    except ValueError:
                                        pass
                        for pid in pids_from_ss:
                            if pid > 0 and pid != os.getpid() and pid not in seen_pids:
                                seen_pids.add(pid)
                                _sp.run(["kill", "-9", str(pid)],
                                        capture_output=True, timeout=5)
                                print("  Killed stale process %d on port %d" % (pid, port), flush=True)
                                killed_any = True
                    except Exception:
                        pass
                    break  # No further attempts without lsof
                else:
                    for pid_str in r.stdout.split():
                        try:
                            pid = int(pid_str)
                        except ValueError:
                            continue
                        if pid > 0 and pid != os.getpid() and pid not in seen_pids:
                            seen_pids.add(pid)
                            _sp.run(["kill", "-9", str(pid)],
                                    capture_output=True, timeout=5)
                            print("  Killed stale process %d on port %d" % (pid, port), flush=True)
                            killed_any = True

            if not killed_any:
                break
            time.sleep(0.5)
        except Exception:
            break

if not _TEST_PORT:
    _update_boot_status("STEP:ports")
    _kill_port(_WEB_PORT)
    # Only kill the daemon port on a cold start.  When the web server is
    # restarted via /api/restart with scope="web", it sets
    # VIBENODE_PRESERVE_DAEMON=1 so the living daemon (and all its active
    # sessions/CLI subprocesses) survive the restart.
    if os.environ.get("VIBENODE_PRESERVE_DAEMON") != "1":
        _kill_port(DAEMON_PORT)
    else:
        print("  VIBENODE_PRESERVE_DAEMON=1 -- skipping daemon port kill", flush=True)
    time.sleep(0.05)  # brief pause for ports to release

    # ---- Singleton gate: only one web server allowed ----
    if not acquire_web_singleton():
        # Mutex held but we just killed the ports — stale mutex. Proceed.
        print("  Stale singleton detected. Starting anyway.", flush=True)

# ---------------------------------------------------------------------------
# Self-healing desktop shortcut.
# After a git pull the shortcut may still point to py.exe (console flash) or
# python.exe instead of pythonw.exe (windowless).  Fix it on every launch so
# the user never has to think about it.
# ---------------------------------------------------------------------------
def _fix_shortcut():
    if sys.platform != "win32":
        return
    try:
        lnk_path = Path.home() / "Desktop" / "VibeNode.lnk"
        if not lnk_path.exists():
            return
        # Find pythonw.exe next to the running python.exe
        pythonw = Path(sys.executable).parent / "pythonw.exe"
        if not pythonw.exists():
            return
        script = Path(__file__).resolve().parent / "session_manager.py"
        icon = Path(__file__).resolve().parent / "static" / "vibenode.ico"
        # Read current shortcut target to see if it already points to pythonw
        # (avoid rewriting on every launch if already correct)
        ps_read = (
            "$ws = New-Object -ComObject WScript.Shell;"
            f"$lnk = $ws.CreateShortcut('{lnk_path}');"
            "Write-Output $lnk.TargetPath;"
            "Write-Output $lnk.IconLocation"
        )
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_read],
            capture_output=True, text=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        lines = r.stdout.strip().splitlines()
        current_target = (lines[0] if lines else "").lower()
        current_icon = (lines[1] if len(lines) > 1 else "").lower()
        expected_icon = str(icon).lower()
        if "pythonw" in current_target and expected_icon in current_icon:
            return  # Already correct
        # Rewrite the shortcut
        ps_write = (
            "$ws = New-Object -ComObject WScript.Shell;"
            f"$lnk = $ws.CreateShortcut('{lnk_path}');"
            f"$lnk.TargetPath = '{pythonw}';"
            f"$lnk.Arguments = '\"{script}\"';"
            f"$lnk.WorkingDirectory = '{script.parent}';"
            f"$lnk.IconLocation = '{icon},0';"
            "$lnk.WindowStyle = 7;"
            "$lnk.Save()"
        )
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", ps_write],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        pass  # Best effort — never block startup

# Run shortcut repair in background — it's best-effort and involves
# slow PowerShell calls that shouldn't block startup.
threading.Thread(target=_fix_shortcut, daemon=True).start()

# ---------------------------------------------------------------------------
# Dependency health check — auto-install missing packages at boot
# ---------------------------------------------------------------------------
def _check_dependencies():
    """Verify required Python packages are installed. Auto-install any missing."""
    # (package_import_name, pip_install_name)
    # SpeechNode is a git dependency (not on PyPI) and has NO in-tree fallback since
    # it was extracted out of VibeNode — without auto-install here, one-click/Update
    # users (who never run pip manually) silently lose voice ("SpeechNode unavailable").
    # Pinned to the same commit as requirements.txt; bump both together to upgrade.
    _SPEECHNODE_SPEC = (
        "speechnode[flask] @ git+https://github.com/CustomerNode/SpeechNode.git"
        "@bf6c78ba3a092dd86cc086874a2fa5d67d1a5472"
    )
    required = [
        ("flask", "flask"),
        ("flask_socketio", "flask-socketio"),
        ("anthropic", "anthropic"),
        ("speechnode", _SPEECHNODE_SPEC),
    ]
    missing = []
    for import_name, pip_name in required:
        if importlib.util.find_spec(import_name) is None:
            missing.append(pip_name)

    if missing:
        print("  Installing missing packages: %s" % ", ".join(missing), flush=True)
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet"] + missing,
                timeout=120,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            print("  Packages installed successfully.", flush=True)
        except Exception as e:
            print("  WARNING: Could not install packages: %s" % e, flush=True)

_update_boot_status("STEP:deps")
_check_dependencies()

# ---------------------------------------------------------------------------
# Claude auto-update — keep the claude CLI + Python SDK current so newly
# released models are picked up without manual intervention.
#
# Throttled to once per 24h via a gitignored state file, time-boxed, and
# best-effort: any failure just logs a warning and boot continues.
#
# Placement is deliberate: this MUST run BEFORE ensure_daemon().  The session
# daemon is the process that imports claude_code_sdk and spawns the claude
# CLI, so an update applied after the daemon starts has no effect until the
# daemon's next cold start.  If the daemon is already running (it survives
# web restarts), the update still lands on disk and applies on the daemon's
# next start — we NEVER restart the daemon automatically (see CLAUDE.md).
# ---------------------------------------------------------------------------
_UPDATE_STATE_FILE = Path(__file__).resolve().parent / ".cache" / "claude_update_state.json"
_UPDATE_CHECK_INTERVAL = 24 * 3600  # seconds between update checks


def _claude_cli_version(claude_path):
    """Return `claude --version` output, or '' on any failure."""
    if not claude_path:
        return ""
    try:
        r = subprocess.run(
            [claude_path, "--version"], capture_output=True, text=True, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return (r.stdout or "").strip()
    except Exception:
        return ""


def _daemon_is_running():
    """True if something is listening on the daemon port (read-only check)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(("127.0.0.1", DAEMON_PORT))
        s.close()
        return True
    except (ConnectionRefusedError, OSError):
        return False


def _check_claude_updates():
    """Auto-update the claude CLI and claude-code-sdk, at most once per day.

    New Claude models ship via CLI/SDK releases — without this, users on the
    one-click install never get them until someone updates manually.  Set
    VIBENODE_NO_AUTO_UPDATE=1 to disable.
    """
    import json

    if os.environ.get("VIBENODE_NO_AUTO_UPDATE"):
        return

    # Throttle: at most one check per _UPDATE_CHECK_INTERVAL.  On throttled
    # boots this function costs a single small file read.
    state = {}
    try:
        if _UPDATE_STATE_FILE.exists():
            state = json.loads(_UPDATE_STATE_FILE.read_text())
    except Exception:
        state = {}
    if time.time() - state.get("last_check", 0) < _UPDATE_CHECK_INTERVAL:
        return

    no_window = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    claude_path = shutil.which("claude")
    daemon_was_running = _daemon_is_running()
    before = _claude_cli_version(claude_path)

    # 1. claude CLI — this is what gates new model availability.  Its
    #    built-in self-updater handles native installs and refuses (with an
    #    npm hint) when the install is npm-managed.
    if claude_path:
        print("  Checking for Claude CLI updates...", flush=True)
        try:
            r = subprocess.run(
                [claude_path, "update"], capture_output=True, text=True,
                timeout=240, creationflags=no_window,
            )
            out = (r.stdout or "") + (r.stderr or "")
            if r.returncode != 0 and "npm" in out.lower():
                npm = shutil.which("npm")
                if npm:
                    subprocess.run(
                        [npm, "update", "-g", "@anthropic-ai/claude-code"],
                        capture_output=True, timeout=240, creationflags=no_window,
                    )
        except Exception as e:
            print("  WARNING: claude CLI update check failed: %s" % e, flush=True)

    # 2. Python SDK — the daemon's interface to the CLI.  requirements.txt
    #    only pins a floor (>=), so a plain upgrade is always safe here.
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade",
             "claude-code-sdk"],
            timeout=180, creationflags=no_window,
        )
    except Exception as e:
        print("  WARNING: claude-code-sdk upgrade failed: %s" % e, flush=True)

    after = _claude_cli_version(claude_path)
    updated = bool(before and after and before != after)
    if updated:
        print("  Claude CLI updated: %s -> %s" % (before, after), flush=True)
        if daemon_was_running:
            print(
                "  NOTE: the session daemon is already running and keeps the old\n"
                "  version until its next restart. To apply now (this ends all\n"
                "  running sessions): System -> Restart Server -> Session Daemon.",
                flush=True,
            )

    try:
        _UPDATE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _UPDATE_STATE_FILE.write_text(json.dumps({
            "last_check": time.time(),
            "cli_version": after or before,
            "updated_last_check": updated,
            "daemon_restart_pending": updated and daemon_was_running,
        }, indent=2))
    except Exception:
        pass


if not _TEST_PORT:
    _update_boot_status("STEP:update")
    try:
        _check_claude_updates()
    except Exception as _upd_err:
        print("  WARNING: Claude update check failed: %s" % _upd_err, flush=True)

# Ensure daemon is running before creating the Flask app (skip in test mode)
if not _TEST_PORT:
    _update_boot_status("STEP:daemon")
    ensure_daemon()

_update_boot_status("STEP:server")
try:
    from app import create_app, socketio
    app = create_app()
    # Past the historically-wedging import + daemon phases — stand the
    # watchdog and the faulthandler autopsy down.
    _boot_done.set()
    _early_boot.disarm_hang_dump()
except Exception as _init_err:
    _update_boot_status("ERROR:Failed to initialize server: %s" % _init_err)
    raise


## ── CRITICAL: Chrome-first browser launch ──────────────────────────────────
## DO NOT replace this with os.startfile(url) or webbrowser.open(url) alone.
## The Web Speech API (voice input) is Chromium-only. Firefox does not support
## it. If the default browser is Firefox, the browser opener silently breaks
## voice with zero error messages — the mic button just disappears.
## This exact regression already happened once on Windows and shipped to users.
##
## All three platforms use Chrome-first, system-browser fallback:
##   Windows: _find_chrome()       → ShellExecuteW(chrome, url) → os.startfile
##   Linux:   _find_chrome_linux() → Popen([chrome, url])       → xdg-open
##   macOS:   _find_chrome_macos() → Popen([chrome, url])       → open
##
## Tests in tests/test_browser_launch.py enforce the Windows pattern.
## Run them before changing _find_chrome() or the Windows open_browser block.
## ────────────────────────────────────────────────────────────────────────────

def _find_chrome():
    """Return the path to chrome.exe on Windows, or None."""
    import winreg
    # Check registry first (most reliable — works for all install types)
    for hive, key in [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
    ]:
        try:
            with winreg.OpenKey(hive, key) as k:
                p = winreg.QueryValue(k, None)
                if p and os.path.isfile(p):
                    return p
        except OSError:
            pass
    # Fallback: common install paths
    for base in [os.environ.get("PROGRAMFILES", ""), os.environ.get("PROGRAMFILES(X86)", ""),
                 os.path.expandvars(r"%LOCALAPPDATA%")]:
        if not base:
            continue
        p = os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
        if os.path.isfile(p):
            return p
    return None


def _find_chrome_linux():
    """Return the path to a Chromium-based browser on Linux, or None.

    Tries binary names in order of preference — the Web Speech API requires
    a Chromium-based browser, so we prefer Google Chrome over Chromium over
    other Chromium-derived browsers.

    Returns None if no Chromium-based browser is found in PATH or common
    install locations, so the caller can fall back to xdg-open.
    """
    # Ordered by preference: stable Chrome first, then Chromium variants
    candidates = [
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "google-chrome-beta",
        "google-chrome-unstable",
    ]
    for name in candidates:
        path = shutil.which(name)
        if path:
            return path
    # Check common install locations not always on PATH
    home = str(Path.home())
    common_paths = [
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/google-chrome",          # Ubuntu snap: google-chrome package
        "/snap/bin/chromium",               # Ubuntu snap: chromium package
        "/opt/google/chrome/google-chrome", # Manual/enterprise Chrome install
        # Flatpak — system and per-user exports
        "/var/lib/flatpak/exports/bin/com.google.Chrome",
        os.path.join(home, ".local/share/flatpak/exports/bin/com.google.Chrome"),
    ]
    for p in common_paths:
        if os.path.isfile(p):
            return p
    return None


def _find_chrome_macos():
    """Return the path to Chrome/Chromium on macOS, or None.

    Checks standard /Applications and ~/Applications locations.
    Returns None so the caller falls back to the system `open` command.
    """
    home = str(Path.home())
    common_paths = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        os.path.join(home, "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        os.path.join(home, "Applications/Chromium.app/Contents/MacOS/Chromium"),
    ]
    for p in common_paths:
        if os.path.isfile(p):
            return p
    return None


def _chrome_running():
    """Best-effort check for whether a Chrome/Chromium process is already up.

    This is the linchpin of tab-mode's tradeoff-free launch (see open_browser).
    The two bugs the 6/13 isolated-profile change fixed are mutually exclusive
    by Chrome's running state, so knowing that state lets us pick the safe
    invocation for each case instead of paying for isolation we don't need:

      * Chrome already running  -> a bare URL opens a new TAB. It can't wedge
        focus (only a launcher-spawned *window* does that) and session restore
        is irrelevant (Chrome already restored on its own startup).
      * Chrome not running       -> ``--new-window <url>`` forces the URL to
        display. This was the original pre-6/13 fix for "Continue where you
        left off" swallowing a bare URL, and there is no running Chrome for a
        new window to wedge.

    Returns True/False. On any detection failure we return True (assume
    running) so the common case — the user already has their Chrome-with-tabs
    open — uses the bare-URL tab path and never risks a focus wedge. The only
    cost of a wrong "True" is a rare cold-start bare-URL open, which at worst
    means reopening the shortcut; a wrong "False" would wedge focus, which is
    the more annoying failure, so we bias away from it.
    """
    try:
        if sys.platform == "win32":
            # CREATE_NO_WINDOW (0x08000000) keeps tasklist from flashing a
            # console window during the detached/minimized launch.
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
                capture_output=True, text=True, timeout=4,
                creationflags=0x08000000,
            )
            return "chrome.exe" in (out.stdout or "").lower()
        else:
            # pgrep is present on macOS and virtually all Linux. -i matches
            # "Google Chrome" (macOS), "chrome", and "chromium" (Linux).
            for pat in ("chrome", "chromium"):
                r = subprocess.run(
                    ["pgrep", "-i", pat],
                    capture_output=True, text=True, timeout=4,
                )
                if r.returncode == 0 and (r.stdout or "").strip():
                    return True
            return False
    except Exception:
        return True  # bias toward the no-wedge tab path — see docstring


def open_browser():
    import time
    import urllib.request
    url = f"http://localhost:{_WEB_PORT}"
    log_path = Path(__file__).resolve().parent / "logs" / "browser_open.log"
    log_path.parent.mkdir(exist_ok=True)

    # ISOLATED CHROME INSTANCE — see CLAUDE.md item 18.
    # --app= + --user-data-dir= give VibeNode its own Chrome window and its
    # own profile. The user's everyday Chrome stays fully independent: new
    # windows open normally, closing it doesn't kill VibeNode's tab, and the
    # launcher-spawned window no longer wedges focus on the main profile.
    # The dedicated profile also sidesteps Chrome's session restore, so the
    # URL always loads instead of being swallowed by "Continue where you
    # left off" on a cold start.
    profile_dir = Path(__file__).resolve().parent / "data" / "chrome-profile"
    try:
        profile_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        # If we can't create the profile dir, fall back to Chrome's default
        # profile rather than failing the launch outright.
        profile_dir = None
        _log_dir_err = str(e)
    else:
        _log_dir_err = None

    def _log(msg):
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
        except Exception:
            pass

    if _log_dir_err:
        _log("Could not create isolated Chrome profile dir: %s" % _log_dir_err)

    # LAUNCH MODE — see CLAUDE.md item 18 and get_kanban_config() defaults.
    #   "app" (default) → isolated Chrome app window (--app= + --user-data-dir=).
    #   "tab"           → a normal tab in the user's everyday Chrome profile.
    # The flags are gated, NOT removed: the shipped default stays "app" so the
    # bug fixes remain in force for everyone who hasn't opted out.
    launch_mode = "app"
    try:
        from app.config import get_kanban_config
        launch_mode = (get_kanban_config().get("browser_launch_mode") or "app").strip().lower()
        if launch_mode not in ("app", "tab"):
            launch_mode = "app"
    except Exception as e:
        _log("Could not read browser_launch_mode (defaulting to 'app'): %s" % e)
    # In tab mode we launch into the user's default Chrome profile, so the
    # isolated profile dir is intentionally unused.
    use_app_window = (launch_mode == "app" and profile_dir is not None)
    # Tab mode picks its invocation from Chrome's running state so BOTH 6/13
    # bugs are avoided without an isolated profile (see _chrome_running):
    #   running     -> bare URL  (new tab; no wedge, no session-restore swallow)
    #   not running -> --new-window (forces URL; nothing running to wedge)
    tab_new_window = False
    if launch_mode == "tab":
        tab_new_window = not _chrome_running()
    _log("Browser launch mode: %s (app window=%s, tab_new_window=%s)" % (
        launch_mode, use_app_window, tab_new_window))

    _log(f"Waiting for server on port {_WEB_PORT}...")
    # Wait until the server is actually accepting connections before opening
    for attempt in range(60):
        try:
            urllib.request.urlopen(url, timeout=1)
            _log("Server responded after %d attempts" % (attempt + 1))
            break
        except Exception:
            time.sleep(1)
    else:
        _log("ERROR: Server never responded after 60 attempts. Aborting browser open.")
        return  # server never came up, don't open a broken tab

    opened = False
    if sys.platform == "win32":
        # CRITICAL: Must open Chrome, NOT the default browser. See comment
        # block above _find_chrome() for full explanation and history.
        # ShellExecuteW is focus-safe even when parent is minimized
        # (launch.bat uses start /min). os.startfile is ONLY a fallback
        # for systems where Chrome is genuinely not installed.
        chrome_path = _find_chrome()
        if chrome_path:
            try:
                import ctypes
                # Isolated Chrome app window — see open_browser() comment block
                # at the top for the full rationale. --app= mode also avoids
                # session restore swallowing the URL, so we no longer need
                # --new-window. If profile_dir is None (creation failed), fall
                # back to plain --new-window so the launch still works.
                if use_app_window:
                    params = '--app="%s" --user-data-dir="%s"' % (url, profile_dir)
                elif launch_mode == "app":
                    # App mode requested but the isolated profile dir couldn't
                    # be created — fall back to a plain new window.
                    params = '--new-window "%s"' % url
                elif tab_new_window:
                    # Tab mode, Chrome cold — force the URL into a new window so
                    # session restore can't swallow it (nothing running to wedge).
                    params = '--new-window "%s"' % url
                else:
                    # Tab mode, Chrome already up — bare URL → new tab in the
                    # user's everyday window. No wedge, no session-restore race.
                    params = '"%s"' % url
                result = ctypes.windll.shell32.ShellExecuteW(
                    None, "open", chrome_path, params, None, 1)
                if result > 32:
                    _log("Opened Chrome via ShellExecuteW: %s (%s)" % (
                        chrome_path,
                        "isolated app mode" if use_app_window else "shared profile (tab)"))
                    opened = True
                else:
                    _log("ShellExecuteW returned %s for Chrome" % result)
            except Exception as e:
                _log("Chrome ShellExecuteW failed: %s" % e)
        if not opened:
            try:
                os.startfile(url)
                _log("Chrome not found — opened default browser via os.startfile")
                opened = True
            except Exception as e:
                _log("os.startfile failed: %s" % e)
    elif sys.platform == "darwin":
        # CRITICAL: Prefer Chrome/Chromium on macOS for the same reason as
        # Windows and Linux — the Web Speech API (voice input) is Chromium-only.
        # `open url` would launch whatever the user's default browser is.
        chrome_path = _find_chrome_macos()
        if chrome_path:
            try:
                # start_new_session=True detaches Chrome from session_manager.py's
                # process group so a daemon restart doesn't take Chrome with it.
                # DEVNULL stdio keeps Chrome's chatty debug output out of the
                # launch.sh terminal where it would scroll over our own messages.
                # --app= + --user-data-dir= give VibeNode an isolated Chrome
                # window — see open_browser() top comment for the rationale.
                if use_app_window:
                    chrome_args = [chrome_path,
                                   "--app=%s" % url,
                                   "--user-data-dir=%s" % profile_dir]
                elif tab_new_window or launch_mode == "app":
                    # Tab mode with Chrome cold (force URL past session restore),
                    # or app mode whose isolated profile couldn't be created.
                    # Nothing running to wedge, so a new window is safe.
                    chrome_args = [chrome_path, "--new-window", url]
                else:
                    # Tab mode, Chrome already up — bare URL → new tab in the
                    # user's everyday Chrome. No wedge, no session-restore race.
                    chrome_args = [chrome_path, url]
                subprocess.Popen(
                    chrome_args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                _log("Opened Chrome/Chromium via %s (%s)" % (
                    chrome_path,
                    "isolated app mode" if use_app_window else "shared profile (tab)"))
                opened = True
            except Exception as e:
                _log("Chrome launch failed (%s): %s" % (chrome_path, e))
        if not opened:
            try:
                subprocess.Popen(["open", url])
                _log("Chrome not found — opened default browser via open")
                opened = True
            except FileNotFoundError:
                pass
    elif sys.platform == "linux":
        # CRITICAL: Prefer Chrome/Chromium on Linux for the same reason as
        # Windows — the Web Speech API (voice input) is Chromium-only.
        # Try to find and launch Chrome/Chromium first; fall back to xdg-open
        # (which may open Firefox or another browser) only if Chromium is
        # not installed.  This mirrors the Windows ShellExecuteW + Chrome
        # pattern that was added after voice input broke for Windows users.
        chrome_path = _find_chrome_linux()
        if chrome_path:
            try:
                # start_new_session=True detaches Chrome from session_manager.py's
                # process group so a daemon restart doesn't take Chrome with it.
                # DEVNULL stdio keeps Chrome's chatty debug output out of the
                # launch.sh terminal where it would scroll over our own messages.
                # --app= + --user-data-dir= give VibeNode an isolated Chrome
                # window — see open_browser() top comment for the rationale.
                if use_app_window:
                    chrome_args = [chrome_path,
                                   "--app=%s" % url,
                                   "--user-data-dir=%s" % profile_dir]
                elif tab_new_window or launch_mode == "app":
                    # Tab mode with Chrome cold (force URL past session restore),
                    # or app mode whose isolated profile couldn't be created.
                    # Nothing running to wedge, so a new window is safe.
                    chrome_args = [chrome_path, "--new-window", url]
                else:
                    # Tab mode, Chrome already up — bare URL → new tab in the
                    # user's everyday Chrome. No wedge, no session-restore race.
                    chrome_args = [chrome_path, url]
                subprocess.Popen(
                    chrome_args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                _log("Opened Chrome/Chromium via %s (%s)" % (
                    chrome_path,
                    "isolated app mode" if use_app_window else "shared profile (tab)"))
                opened = True
            except Exception as e:
                _log("Chrome launch failed (%s): %s" % (chrome_path, e))
        if not opened:
            try:
                subprocess.Popen(["xdg-open", url])
                _log("Chrome not found — opened default browser via xdg-open")
                opened = True
            except FileNotFoundError:
                pass

    if not opened:
        try:
            webbrowser.open(url)
            _log("Opened via webbrowser.open() fallback")
        except Exception as e:
            _log("ERROR: All browser methods failed. Last error: %s" % e)


if __name__ == "__main__":
    # Label this process so a resource-hunting human or AI sees a "do not kill"
    # marker in the process list / window title.  Best-effort; never raises.
    try:
        import process_label
        process_label.label_current_process("web-server")
    except Exception:
        pass

    # Log web server startup to file so we can diagnose launch failures
    # (console output is lost when launch.bat runs minimized)
    _web_log_path = Path(__file__).resolve().parent / "logs" / "web_server.log"
    _web_log_path.parent.mkdir(exist_ok=True)
    _web_log_fh = open(_web_log_path, "a", encoding="utf-8")
    import datetime as _dt
    _web_log_fh.write("\n--- Web server starting at %s ---\n" % _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    _web_log_fh.flush()
    # Tee stdout/stderr to the log file (keep console output too)
    class _TeeWriter:
        def __init__(self, original, log_fh):
            self._orig = original
            self._log = log_fh
        def write(self, s):
            if self._orig:
                self._orig.write(s)
            try:
                self._log.write(s)
                self._log.flush()
            except Exception:
                pass
        def flush(self):
            if self._orig:
                self._orig.flush()
    sys.stdout = _TeeWriter(sys.stdout, _web_log_fh)
    sys.stderr = _TeeWriter(sys.stderr, _web_log_fh)

    # Route PROFILE logs (and any INFO+) from the web process to the
    # tee'd stdout so they land in web_server.log alongside BOARD timing.
    # Format mirrors the daemon's "HH:MM:SS [web] LEVEL message" style.
    _root = logging.getLogger()
    _root.setLevel(logging.INFO)
    _sh = logging.StreamHandler(sys.stdout)  # stdout is already tee'd
    _sh.setLevel(logging.INFO)
    _sh.setFormatter(logging.Formatter("%(asctime)s [web] %(levelname)s %(message)s",
                                       datefmt="%H:%M:%S"))
    # PERF-CRITICAL: "app.daemon_client" must stay in this list — removing silences IPC profiling. See CLAUDE.md #14.
    # Only attach to app.routes loggers — avoid flooding from libraries
    for _ns in ("app.routes", "app.daemon_client"):
        logging.getLogger(_ns).addHandler(_sh)
        logging.getLogger(_ns).setLevel(logging.INFO)

    # Suppress Flask/Werkzeug request logging and startup banner
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.ERROR)
    cli = sys.modules.get("flask.cli")
    if cli:
        cli.show_server_banner = lambda *a, **k: None

    print("\n"
          "  =========================================================\n"
          "    VIBENODE RUNNING - KEEP THIS TERMINAL OPEN\n"
          "  =========================================================\n\n"
          "  Open your browser to: http://localhost:5050\n\n"
          "  This is a local server for personal use.\n"
          "  Close it or press Ctrl+C to stop.\n\n"
          "  ---------------------------------------------------------\n\n"
          "  Building something complex and need to sell it?\n\n"
          "  CustomerNode.com\n"
          "  Turns Complex Deals Into Executable Journeys.\n"
          "  One shared path from discovery to mutual success.\n"
          "  Guided for buyers. Repeatable for sellers.\n"
          "  Powered by First-Party AI(TM).\n\n"
          "  Developed by the team at customernode.com\n"
          "  MIT License | Copyright 2026 CustomerNode LLC\n\n"
          "  ---------------------------------------------------------\n",
          flush=True)

    _port = _TEST_PORT or _WEB_PORT
    if not _TEST_PORT:
        _update_boot_status("STEP:browser")
        _update_boot_status("DONE")
        # Clean up the status file after the splash has had time to read DONE
        _sf = os.environ.get("VIBENODE_BOOT_STATUS_FILE")
        if _sf:
            def _cleanup_status_file(path):
                try:
                    os.unlink(path)
                except Exception:
                    pass
            threading.Timer(5.0, _cleanup_status_file, args=[_sf]).start()
        # Only open browser on cold start. On web restarts
        # (VIBENODE_PRESERVE_DAEMON=1) the user already has the tab open
        # and Socket.IO will auto-reconnect — no duplicate window needed.
        if os.environ.get("VIBENODE_PRESERVE_DAEMON") != "1":
            threading.Thread(target=open_browser, daemon=True).start()
    socketio.run(app, host="127.0.0.1", port=_port, debug=False, allow_unsafe_werkzeug=True)
