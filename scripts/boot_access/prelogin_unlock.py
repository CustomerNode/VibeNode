#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VibeNode pre-login "Unlock & start" page for Linux machines with an
ecryptfs-encrypted home. Installed OUTSIDE the home (e.g. /opt/vibenode-prelogin)
by setup_linux_boot.sh, as a SYSTEM service — see README.md in this folder.

WHY: pre-login, an ecryptfs home is ciphertext, so neither the reviver's user
unit (~/.config/systemd/user) nor reviver.py itself can run — linger can't fix
that. After an unattended reboot the phone's Tailscale link hits a bare 502.

WHAT: while the home is NOT mounted and nothing owns the VibeNode web port,
this serves a page with a password field. On submit it:

  password -> ecryptfs-insert-wrapped-passphrase-into-keyring
           -> mount.ecryptfs_private            (home is now decrypted)
           -> systemctl --user daemon-reload + start the reviver unit
           -> release the port
           -> launch VibeNode (session_manager.py, same entry as the launchers)

The page then polls /api/health and navigates in when web+daemon are both up.
The moment the home is mounted by ANY means (console login included), it
releases the port and goes dormant — the reviver owns the post-login world.

SECURITY:
  * Binds 127.0.0.1 only — remote reachability is solely via the existing
    tailnet-only `tailscale serve` HTTPS mapping. Nothing new is exposed.
  * The password is piped once to the stock ecryptfs helpers' stdin — never
    stored, never logged, never in argv.
  * 5 failed attempts -> 15-minute lockout (in-memory).
  * /api/* is answered 503 (mirrors reviver.py) so a parked VibeNode tab
    correctly detects "server down" instead of mistaking this page for it.

CONFIG (env, all optional — the installer bakes them into the unit):
  VN_USER                target username          (default: the service user)
  VN_HOME                home dir                 (default: ~VN_USER)
  VN_CHECKOUT            VibeNode checkout path   (REQUIRED to launch/poke)
  VN_RUNTIME_DIR         XDG_RUNTIME_DIR          (default: /run/user/<uid>)
  VN_WRAPPED_PASSPHRASE  wrapped-passphrase path  (default: /home/.ecryptfs/<user>/.ecryptfs/wrapped-passphrase)
  VIBENODE_WEB_PORT      web port                 (default: 5050)
"""

from __future__ import annotations

import getpass
import hashlib
import html
import json
import os
import pwd
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VN_USER = os.environ.get("VN_USER") or getpass.getuser()
try:
    _PW = pwd.getpwnam(VN_USER)
except KeyError:
    _PW = None
VN_HOME = os.environ.get("VN_HOME") or (_PW.pw_dir if _PW else "/home/" + VN_USER)
VN_CHECKOUT = os.environ.get("VN_CHECKOUT", "")
VN_RUNTIME_DIR = os.environ.get("VN_RUNTIME_DIR") or (
    "/run/user/%d" % (_PW.pw_uid if _PW else os.getuid()))
WRAPPED_PASSPHRASE = os.environ.get(
    "VN_WRAPPED_PASSPHRASE",
    "/home/.ecryptfs/%s/.ecryptfs/wrapped-passphrase" % VN_USER)
WEB_PORT = int(os.environ.get("VIBENODE_WEB_PORT", 0) or 5050)

_POLL_SECONDS = 2.0
_MAX_FAILS = 5
_LOCKOUT_SECONDS = 900

# Sentinel comment embedded in served pages (parallel to reviver.py's).
_SENTINEL = "vibenode-prelogin-page"


def _reviver_unit() -> str:
    """Same name reviver.py registers: salted with the checkout path."""
    if os.environ.get("VN_REVIVER_UNIT"):
        return os.environ["VN_REVIVER_UNIT"]
    if not VN_CHECKOUT:
        return ""
    salt = hashlib.md5(VN_CHECKOUT.encode("utf-8")).hexdigest()[:8]
    return "vibenode-reviver-%s.service" % salt


def _log(msg: str) -> None:
    """stdout -> journald. /opt is root-owned and the home is ciphertext
    pre-login, so the journal is the only sane sink. Never raises."""
    try:
        print("[prelogin] %s" % msg, flush=True)
    except Exception:
        pass


def _device_name() -> str:
    try:
        return (socket.gethostname() or "").split(".")[0].strip() or "VibeNode"
    except Exception:
        return "VibeNode"


def _home_mounted() -> bool:
    """True if the user's ecryptfs home is currently decrypted/mounted."""
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == VN_HOME and parts[2] == "ecryptfs":
                    return True
    except Exception:
        pass
    return False


def _something_listening(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.75)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _mount_helper() -> str:
    for cand in ("/sbin/mount.ecryptfs_private", "/usr/sbin/mount.ecryptfs_private"):
        if os.path.isfile(cand):
            return cand
    return shutil.which("mount.ecryptfs_private") or "/sbin/mount.ecryptfs_private"


def _try_unlock(password: str) -> tuple[bool, str]:
    """Replicates the stock ecryptfs-mount-private chain, non-interactively:

        printf "%s\\0" "$PASS" |
            ecryptfs-insert-wrapped-passphrase-into-keyring <wrapped> -
        mount.ecryptfs_private            (setuid helper, as the plain user)

    Both steps run inside ONE /bin/sh child so they share the inherited
    session keyring (`keyctl link @us @s` is the standard belt-and-braces for
    non-PAM contexts; best-effort). Success is judged by the mount actually
    appearing, not by exit codes.
    """
    insert = shutil.which("ecryptfs-insert-wrapped-passphrase-into-keyring")
    if not insert or not os.path.isfile(WRAPPED_PASSPHRASE):
        return False, "ecryptfs tooling or wrapped-passphrase file missing"
    script = (
        '"%s" "$1" - && '
        '{ keyctl link @us @s 2>/dev/null || true; } && '
        'exec "%s"' % (insert, _mount_helper())
    )
    try:
        r = subprocess.run(
            ["/bin/sh", "-c", script, "vn-unlock", WRAPPED_PASSPHRASE],
            input=password.encode("utf-8") + b"\0",
            capture_output=True, timeout=30,
        )
        detail = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
    except Exception as e:
        return False, "unlock helper failed: %s" % e
    if _home_mounted():
        return True, ""
    # Helper output never contains the password — safe to log/show.
    return False, detail or "mount did not appear"


def _poke_user_manager() -> None:
    """Make the (lingering, started-pre-login) user manager see and start the
    reviver unit now that ~/.config/systemd/user is finally readable."""
    env = dict(os.environ)
    env["XDG_RUNTIME_DIR"] = VN_RUNTIME_DIR
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=%s/bus" % VN_RUNTIME_DIR)
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return
    unit = _reviver_unit()
    for cmd in (
        [systemctl, "--user", "daemon-reload"],
        ([systemctl, "--user", "start", unit] if unit else None),
    ):
        if not cmd:
            continue
        try:
            r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)
            _log("%s -> rc=%d %s" % (" ".join(cmd[1:]), r.returncode,
                                     (r.stderr or "").strip()[:200]))
        except Exception as e:
            _log("user-manager poke failed: %s" % e)


def _launch_vibenode() -> None:
    """Launch VibeNode exactly like the desktop launchers: session_manager.py,
    detached, output appended to logs/_server.log. bash -lc sources the user's
    profile (readable now that the home is mounted) so PATH matches a normal
    login shell — the daemon's CLI shell-outs depend on it."""
    if not VN_CHECKOUT:
        _log("VN_CHECKOUT not set — cannot launch VibeNode")
        return
    entry = os.path.join(VN_CHECKOUT, "session_manager.py")
    if not os.path.isfile(entry):
        _log("cannot launch: %s not found (home mounted?)" % entry)
        return
    log_fh = None
    try:
        logs_dir = os.path.join(VN_CHECKOUT, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        log_fh = open(os.path.join(logs_dir, "_server.log"), "a", encoding="utf-8")
    except Exception:
        log_fh = None
    kwargs = {"cwd": VN_CHECKOUT, "start_new_session": True}
    if log_fh is not None:
        kwargs["stdout"] = log_fh
        kwargs["stderr"] = log_fh
    else:
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
    try:
        subprocess.Popen(
            ["/bin/bash", "-lc", "exec python3 session_manager.py"], **kwargs)
        _log("launched VibeNode (session_manager.py) in %s" % VN_CHECKOUT)
    except Exception as e:
        _log("VibeNode launch failed: %s" % e)
    finally:
        try:
            if log_fh is not None:
                log_fh.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class PreLogin:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._attempt_lock = threading.Lock()
        self._fails = 0
        self._locked_until = 0.0
        self._unlock_started = False   # a successful unlock is in flight/done
        self._was_mounted = _home_mounted()

    # ---- serve lifecycle ----

    def _start_serving(self) -> None:
        with self._lock:
            if self._httpd is not None:
                return
            try:
                httpd = ThreadingHTTPServer(("127.0.0.1", WEB_PORT), _Handler)
            except OSError as e:
                _log("could not bind %d: %s (retrying)" % (WEB_PORT, e))
                return
            httpd.ctl = self  # type: ignore[attr-defined]
            self._httpd = httpd
            threading.Thread(target=httpd.serve_forever,
                             name="prelogin-serve", daemon=True).start()
            _log("serving pre-login Start page on 127.0.0.1:%d" % WEB_PORT)

    def _stop_serving(self) -> None:
        with self._lock:
            httpd = self._httpd
            self._httpd = None
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass
            _log("released port %d" % WEB_PORT)

    # ---- unlock ----

    def attempt_unlock(self, password: str) -> tuple[int, dict]:
        """Returns (http_status, json_payload). Serialized; rate-limited."""
        if not password:
            return 400, {"ok": False, "error": "empty password"}
        with self._attempt_lock:
            now = time.monotonic()
            if now < self._locked_until:
                return 429, {"ok": False, "error": "locked",
                             "retry_seconds": int(self._locked_until - now)}
            if self._unlock_started or _home_mounted():
                return 200, {"ok": True, "already": True}
            _log("unlock attempt from phone")
            ok, detail = _try_unlock(password)
            if not ok:
                self._fails += 1
                _log("unlock FAILED (%d/%d): %s"
                     % (self._fails, _MAX_FAILS, detail[:200]))
                if self._fails >= _MAX_FAILS:
                    self._fails = 0
                    self._locked_until = now + _LOCKOUT_SECONDS
                    return 429, {"ok": False, "error": "locked",
                                 "retry_seconds": _LOCKOUT_SECONDS}
                return 403, {"ok": False, "error": "wrong password",
                             "remaining": _MAX_FAILS - self._fails}
            self._fails = 0
            self._unlock_started = True
            _log("home UNLOCKED via phone — handing off to VibeNode")
            threading.Thread(target=self._after_unlock,
                             name="prelogin-handoff", daemon=True).start()
            return 200, {"ok": True}

    def _after_unlock(self) -> None:
        # Order matters: free the port first so the reviver/run.py can bind it.
        self._stop_serving()
        _poke_user_manager()
        _launch_vibenode()

    # ---- main loop ----

    def run(self) -> None:
        _log("up: user=%s home=%s port=%d checkout=%s"
             % (VN_USER, VN_HOME, WEB_PORT, VN_CHECKOUT or "<unset>"))
        while True:
            mounted = _home_mounted()
            if mounted and not self._was_mounted:
                # Console login (or our own unlock) just happened. Stand down
                # and make sure the user manager loads + starts the reviver —
                # it cannot see the unit dir until the home is decrypted.
                _log("home became mounted — standing down, poking user manager")
                self._stop_serving()
                if not self._unlock_started:
                    _poke_user_manager()
            if not mounted:
                self._unlock_started = False
                with self._lock:
                    serving = self._httpd is not None
                if not serving and not _something_listening(WEB_PORT):
                    self._start_serving()
            self._was_mounted = mounted
            time.sleep(_POLL_SECONDS)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _page(device: str, booting: bool) -> bytes:
    device = html.escape(device)
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{device} — locked</title>
<!-- {_SENTINEL} -->
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  html, body {{ height: 100%; margin: 0; }}
  body {{
    background: #0f1115; color: #e7e9ee;
    font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    display: flex; align-items: center; justify-content: center;
    padding: max(24px, env(safe-area-inset-top)) 24px max(24px, env(safe-area-inset-bottom));
  }}
  .card {{ width: 100%; max-width: 420px; text-align: center; }}
  .dot {{ width: 12px; height: 12px; border-radius: 50%; background: #e5a50a;
    display: inline-block; margin-right: 8px; box-shadow: 0 0 12px #e5a50a88; }}
  .status {{ font-size: 14px; color: #9aa0ad; letter-spacing: .02em;
    text-transform: uppercase; margin-bottom: 18px; }}
  h1 {{ font-size: 24px; margin: 0 0 6px; }}
  .sub {{ color: #9aa0ad; margin: 0 0 28px; }}
  input[type=password] {{
    width: 100%; padding: 16px; border-radius: 12px; border: 1px solid #2a2f3a;
    background: #171a21; color: #e7e9ee; font-size: 17px; margin-bottom: 14px;
  }}
  input[type=password]:focus {{ outline: none; border-color: #4f8cff; }}
  button {{
    -webkit-appearance: none; appearance: none; border: 0; cursor: pointer;
    width: 100%; padding: 18px 20px; border-radius: 14px;
    font-size: 18px; font-weight: 600; color: #fff;
    background: linear-gradient(180deg, #4f8cff, #2f6ff0);
    box-shadow: 0 8px 24px #2f6ff055; transition: transform .05s ease, opacity .2s;
  }}
  button:active {{ transform: translateY(1px) scale(.995); }}
  button[disabled] {{ opacity: .6; cursor: default; }}
  .hint {{ margin-top: 20px; font-size: 13px; color: #6c7280; }}
  .err {{ margin-top: 14px; font-size: 14px; color: #f0553b; min-height: 1.4em; }}
  .spin {{ width: 34px; height: 34px; border-radius: 50%;
    border: 3px solid #2a2f3a; border-top-color: #4f8cff;
    animation: r 0.9s linear infinite; margin: 0 auto 20px; }}
  @keyframes r {{ to {{ transform: rotate(360deg); }} }}
</style></head>
<body>
  <div class="card" id="card" style="{'display:none' if booting else ''}">
    <div class="status"><span class="dot"></span>Rebooted &amp; locked</div>
    <h1>{device}</h1>
    <p class="sub">The computer restarted. Its drive is locked until you
      enter your login password.</p>
    <form onsubmit="go(event)">
      <input type="password" id="pw" placeholder="Login password"
             autocomplete="current-password" autocapitalize="none">
      <button id="btn" type="submit">Unlock &amp; start VibeNode</button>
    </form>
    <div class="err" id="err"></div>
    <div class="hint">Sent over your Tailscale link. Unlocks the encrypted
      home and boots VibeNode.</div>
  </div>
  <div class="card" id="booting" style="{'' if booting else 'display:none'}">
    <div class="spin"></div>
    <h1>Starting VibeNode…</h1>
    <p class="sub">Home unlocked — booting up. You'll be taken in automatically.</p>
    <div class="hint" id="elapsed">0s</div>
  </div>
<script>
  var _t0 = 0;
  function showBooting() {{
    document.getElementById('card').style.display = 'none';
    document.getElementById('booting').style.display = '';
    _t0 = Date.now();
    setInterval(function() {{
      document.getElementById('elapsed').textContent =
        Math.floor((Date.now() - _t0) / 1000) + 's';
    }}, 1000);
    poll();
  }}
  async function go(ev) {{
    ev.preventDefault();
    var pw = document.getElementById('pw').value;
    if (!pw) return;
    var btn = document.getElementById('btn'), err = document.getElementById('err');
    btn.disabled = true; err.textContent = '';
    try {{
      var r = await fetch('/unlock', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
        body: 'password=' + encodeURIComponent(pw),
      }});
      var d = await r.json();
      if (d.ok) {{ showBooting(); return; }}
      if (d.error === 'locked') {{
        err.textContent = 'Too many attempts — locked for ' +
          Math.ceil((d.retry_seconds || 900) / 60) + ' min.';
      }} else if (d.error === 'wrong password') {{
        err.textContent = 'Wrong password (' + d.remaining + ' tries left).';
      }} else {{
        err.textContent = d.error || 'Unlock failed.';
      }}
    }} catch (e) {{
      // Serve socket vanished mid-request => unlock likely succeeded and we
      // released the port. Fall through to the booting poller.
      showBooting(); return;
    }}
    btn.disabled = false;
    document.getElementById('pw').value = '';
    document.getElementById('pw').focus();
  }}
  var _polling = false;
  async function _readyCheck() {{
    // Same contract as the reviver pages: enter ONLY when web AND daemon are
    // up. Pre-login 503 / tailscale 502 / connection errors all mean
    // "keep waiting".
    try {{
      const r = await fetch('/api/health?_=' + Date.now(), {{ cache: 'no-store' }});
      if (!r.ok) return;
      const d = await r.json();
      if (d && d.daemon) location.href = '/';
    }} catch (e) {{}}
  }}
  function poll() {{
    if (_polling) return;
    _polling = true;
    setInterval(_readyCheck, 1500);
    document.addEventListener('visibilitychange', () => {{
      if (document.visibilityState === 'visible') _readyCheck();
    }});
    window.addEventListener('pageshow', _readyCheck);
    window.addEventListener('focus', _readyCheck);
  }}
  {'poll(); _readyCheck();' if booting else ''}
</script>
</body></html>""".encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    server_version = "VibeNodePreLogin"

    def log_message(self, *args):  # silence default stderr access logging
        pass

    def _send(self, body: bytes, status: int = 200,
              ctype: str = "text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, status: int, obj: dict):
        self._send(json.dumps(obj).encode("utf-8"), status=status,
                   ctype="application/json")

    def do_GET(self):
        # Mirror reviver.py: /api/* must FAIL so a parked app tab detects a
        # dead backend instead of mistaking this page for a live server.
        if self.path.split("?", 1)[0].startswith("/api/"):
            self._json(503, {"ok": False, "error": "vibenode-prelogin",
                             "reviver": True})
            return
        ctl = getattr(self.server, "ctl", None)
        booting = bool(ctl and ctl._unlock_started)
        self._send(_page(_device_name(), booting))

    def do_POST(self):
        if self.path.rstrip("/") != "/unlock":
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 65536)
            raw = self.rfile.read(length).decode("utf-8", "replace")
            password = urllib.parse.parse_qs(raw).get("password", [""])[0]
        except Exception:
            self._json(400, {"ok": False, "error": "bad request"})
            return
        ctl = getattr(self.server, "ctl", None)
        if ctl is None:
            self._json(500, {"ok": False, "error": "no controller"})
            return
        status, payload = ctl.attempt_unlock(password)
        self._json(status, payload)


def main() -> int:
    ctl = PreLogin()
    try:
        ctl.run()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        _log("crashed: %s" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
