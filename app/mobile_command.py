"""
Mobile Command — private phone access to VibeNode over your Tailscale tailnet.

WHAT IT DOES
------------
A persistent toggle (System -> Mobile Command, mirroring Persistent Storage). When
ON, VibeNode asks the already-installed Tailscale daemon to expose the local web
server to *your tailnet only* via ``tailscale serve``:

    phone (on your tailnet)  --HTTPS-->  MagicDNS name  -->  127.0.0.1:<port>

DESIGN INVARIANTS
-----------------
1. ZERO CHANGE TO NORMAL VIBENODE. VibeNode itself stays bound to 127.0.0.1. Nothing
   here rebinds the server or exposes it to the LAN or the public internet.
   ``tailscale serve`` (NOT ``funnel``) is tailnet-only — your own authenticated
   devices are the only things that can reach it.

2. PERSISTENT, LIKE PERSISTENT STORAGE. The ON/OFF state is saved in the app config.
   ``rearm()`` runs at every startup and re-establishes the bridge if it was left on,
   so the user never re-does anything.

3. DEFENSIVE. Tailscale may be missing, logged out, or lack HTTPS certs. Every code
   path degrades to a structured ``status()`` telling the UI exactly what the user
   needs to do next — never a stack trace.

4. NO NEW DEPENDENCY. Pure stdlib + the Tailscale CLI that's already on the box.

We serve over **HTTPS** on the tailnet (``tailscale serve --bg <port>``), using the real
Let's Encrypt cert Tailscale provisions for the node's ``ts.net`` MagicDNS name. HTTPS is
NOT for privacy — the tailnet is already WireGuard-encrypted — it's for the phone: iOS only
grants a *secure context* (the thing that unlocks the microphone / Web Speech / voice input)
to ``https://`` or ``localhost`` origins. Plain ``http://<name>.ts.net`` is not a secure
context, so voice silently dies there. HTTPS is what makes voice work on the phone.

The one cost: HTTPS certs require the tailnet's "HTTPS Certificates" feature to be enabled
once, in the admin console. We detect that from ``CertDomains`` in ``status --json`` and,
when it's off, route the user through the ``enable_https`` guidance panel instead of blindly
running ``serve`` (which blocks when certs are unavailable). After the one-time toggle it's
automatic forever — ``rearm()`` re-establishes the bridge on every startup.

Verified against Tailscale 1.98.2:
  * ``tailscale status --json``       -> BackendState, Self.DNSName, MagicDNSSuffix, CertDomains
  * ``tailscale serve --bg <port>``   -> expose 127.0.0.1:<port> over HTTPS, tailnet-only,
                                         using the ts.net cert. URL: https://<magicdns>/
  * ``tailscale serve status --json`` -> current serve config
  * ``tailscale serve reset``         -> tear it down
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Optional

from .platform_utils import NO_WINDOW
from .config import get_kanban_config, save_kanban_config

_log = logging.getLogger("app")

_CONFIG_KEY = "mobile_command_enabled"
_PORT_KEY = "mobile_command_port"
_DEVICE_NAME_KEY = "mobile_command_device_name"
_DEFAULT_PORT = 5050

# Admin console page where a user enables HTTPS certs for their tailnet (the one
# manual, one-time-per-tailnet step Tailscale requires before `serve` can do HTTPS).
HTTPS_HELP_URL = "https://login.tailscale.com/admin/dns"

# Tailscale issues the ts.net TLS cert LAZILY, on the first HTTPS connection — so the
# user's very first phone hit can hang/time out for several seconds while Let's Encrypt
# provisions it. We pre-warm the cert server-side (a throwaway HTTPS request to our own
# tailnet name) right after `serve` comes up, so the phone's first load is instant.
# Tests flip this off to keep unit runs network-free.
_WARM_CERT_ENABLED = True


# ---------------------------------------------------------------------------
# Tailscale binary discovery
# ---------------------------------------------------------------------------

def _candidate_paths() -> list[str]:
    if sys.platform == "win32":
        return [
            r"C:\Program Files\Tailscale\tailscale.exe",
            r"C:\Program Files (x86)\Tailscale\tailscale.exe",
        ]
    if sys.platform == "darwin":
        return [
            "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
            "/usr/local/bin/tailscale",
            "/opt/homebrew/bin/tailscale",
        ]
    # Linux: apt/repo installs land on PATH (found by shutil.which first); these are
    # fallbacks, including /snap/bin for the common Ubuntu snap package.
    return ["/usr/bin/tailscale", "/usr/local/bin/tailscale", "/snap/bin/tailscale"]


def tailscale_bin() -> Optional[str]:
    """Return the path to the tailscale CLI, or None if not installed."""
    found = shutil.which("tailscale")
    if found:
        return found
    for p in _candidate_paths():
        if os.path.isfile(p):
            return p
    return None


def _run_ts(args: list[str], timeout: int = 15) -> tuple[int, str, str]:
    """Run a tailscale subcommand. Returns (returncode, stdout, stderr).

    Returns (127, "", "tailscale not found") when the CLI is missing, so callers
    never have to special-case the None binary.
    """
    binary = tailscale_bin()
    if not binary:
        return 127, "", "tailscale not found"
    try:
        r = subprocess.run(
            [binary, *args],
            capture_output=True, text=True, timeout=timeout,
            creationflags=NO_WINDOW,
        )
        return r.returncode, (r.stdout or ""), (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "", "tailscale command timed out"
    except Exception as e:  # noqa: BLE001
        return 1, "", str(e)


# ---------------------------------------------------------------------------
# Config (persisted flag) — reuses the existing kanban_config.json store
# ---------------------------------------------------------------------------

def is_flag_enabled() -> bool:
    return bool(get_kanban_config().get(_CONFIG_KEY, False))


def _set_flag(enabled: bool, port: Optional[int] = None) -> None:
    cfg = get_kanban_config()
    cfg[_CONFIG_KEY] = bool(enabled)
    if port:
        cfg[_PORT_KEY] = int(port)
    save_kanban_config(cfg)


def configured_port() -> int:
    try:
        return int(get_kanban_config().get(_PORT_KEY, _DEFAULT_PORT))
    except (TypeError, ValueError):
        return _DEFAULT_PORT


# ---------------------------------------------------------------------------
# Device name — the label the phone's Home-Screen icon shows for THIS computer.
# iOS uses <meta name="apple-mobile-web-app-title"> (and the manifest short_name)
# for the Add-to-Home-Screen name. Without a per-machine value every computer's
# icon would just say "VibeNode", so you couldn't tell two boxes apart. Defaults
# to the hostname (distinct out of the box) and is user-editable in the modal.
# ---------------------------------------------------------------------------

def _default_device_name() -> str:
    """Cheap per-machine default (no subprocess): the short hostname."""
    try:
        host = (socket.gethostname() or "").split(".")[0].strip()
    except Exception:  # noqa: BLE001
        host = ""
    return host or "VibeNode"


def device_name() -> str:
    """The Home-Screen label for this computer: the user's custom value if set,
    else the hostname. Cheap — safe to call on every page render (no Tailscale)."""
    try:
        val = (get_kanban_config().get(_DEVICE_NAME_KEY) or "").strip()
    except Exception:  # noqa: BLE001
        val = ""
    return val or _default_device_name()


def set_device_name(name: str) -> str:
    """Persist a custom Home-Screen label for this computer (empty resets to the
    hostname default). Capped to a sensible length for an icon label. Returns the
    effective name."""
    cfg = get_kanban_config()
    clean = (name or "").strip()[:40]
    if clean:
        cfg[_DEVICE_NAME_KEY] = clean
    else:
        cfg.pop(_DEVICE_NAME_KEY, None)
    save_kanban_config(cfg)
    return device_name()


# ---------------------------------------------------------------------------
# Tailscale state readers
# ---------------------------------------------------------------------------

def _status_json() -> Optional[dict]:
    rc, out, _err = _run_ts(["status", "--json"])
    if rc != 0 or not out.strip():
        return None
    try:
        return json.loads(out)
    except (ValueError, TypeError):
        return None


def _backend_state(status: Optional[dict]) -> str:
    """One of: 'Running', 'NeedsLogin', 'Stopped', 'NoState', 'Unknown'."""
    if not status:
        return "Unknown"
    return status.get("BackendState") or "Unknown"


def tailnet_url(status: Optional[dict] = None) -> Optional[str]:
    """Build the phone-facing HTTPS URL from the node's MagicDNS name."""
    status = status if status is not None else _status_json()
    if not status:
        return None
    self_node = status.get("Self") or {}
    dns = self_node.get("DNSName") or ""
    dns = dns.rstrip(".")
    if not dns:
        return None
    # HTTPS (not HTTP): the phone needs a secure-context origin to unlock the mic /
    # Web Speech (voice input). Served via the node's real ts.net Let's Encrypt cert.
    return f"https://{dns}/"


def _https_available(status: Optional[dict]) -> bool:
    """True if the tailnet has HTTPS certs enabled (so `serve` can do TLS).

    Tailscale populates ``CertDomains`` in ``status --json`` only when the tailnet's
    "HTTPS Certificates" feature is on. When it's off, this is null/empty and any
    HTTPS ``serve`` would block/fail — so we gate on it and guide the one-time toggle.
    """
    if not status:
        return False
    return bool(status.get("CertDomains"))


def _serve_targets_port(port: int) -> bool:
    """True if the current serve config serves our local port over HTTPS.

    Not just "proxies to ``:{port}``": a leftover serve from the previous
    HTTP-on-:80 version also proxied to the same local port. If we accepted that,
    rearm() would think it's "already serving" and never (re)establish the HTTPS
    serve — leaving the user a confident "On" whose ``https://…/`` QR fails on the
    phone. So we EXCLUDE the HTTP (:80) listener rather than *require* :443.

    Excluding :80 (rather than requiring :443) is deliberate robustness: we create
    only HTTPS serves and disable() runs ``serve reset``, so the sole non-HTTPS
    entry that can exist is the legacy :80 one. Rejecting exactly that — instead of
    betting on the precise ``host:443`` key shape — fixes the HTTP→HTTPS upgrade
    without risking a false-negative (perpetual "starting") if Tailscale's JSON keys
    a valid HTTPS serve differently than we assume.
    """
    rc, out, _err = _run_ts(["serve", "status", "--json"])
    if rc != 0 or not out.strip():
        return False
    needle = f":{port}"
    try:
        data = json.loads(out)
    except (ValueError, TypeError):
        # Plain-text fallback: our port present, and not obviously an HTTP(:80) serve.
        return needle in out and ":80" not in out
    # Walk the Web handler tree; skip any handler under the legacy HTTP (:80) listener.
    web = (data or {}).get("Web") or {}
    for host, conf in web.items():
        if str(host).endswith(":80"):
            continue  # ignore a stale HTTP (:80) serve from the pre-HTTPS version
        handlers = (conf or {}).get("Handlers") or {}
        for _path, h in handlers.items():
            proxy = (h or {}).get("Proxy") or ""
            if needle in proxy:
                return True
    return False


def _looks_like_https_cert_error(text: str) -> bool:
    t = (text or "").lower()
    return ("https" in t and ("enable" in t or "cert" in t)) or "httpscert" in t


# ---------------------------------------------------------------------------
# Enable / disable
# ---------------------------------------------------------------------------

def enable(port: Optional[int] = None) -> dict:
    """Turn Mobile Command on: bring up `tailscale serve` for the local port.

    Persists the ON flag regardless of whether the serve command succeeds this
    instant, so a transient tailnet hiccup doesn't silently disable the feature —
    ``rearm()`` will keep retrying at startup. Returns a full status() dict.
    """
    port = int(port or configured_port())
    _set_flag(True, port=port)

    if not tailscale_bin():
        return status(port=port)

    st = _status_json()
    if _backend_state(st) != "Running":
        # Not logged in / not up yet — leave the flag on; UI will guide login.
        return status(port=port)

    if not _https_available(st):
        # Tailnet HTTPS certs aren't enabled yet. Do NOT attempt `serve` — without a
        # cert an HTTPS serve blocks. Route the user to the one-time admin toggle.
        result = status(port=port)
        result["needs"] = "enable_https"
        return result

    rc, out, err = _run_ts(["serve", "--bg", str(port)], timeout=45)
    combined = f"{out}\n{err}".strip()
    if rc == 0:
        _warm_https_cert(_dns_from_status(st))  # pre-provision so the first phone hit is fast
    else:
        _log.warning("Mobile Command: `tailscale serve` failed rc=%s: %s", rc, combined[:400])
    result = status(port=port)
    if rc != 0 and _looks_like_https_cert_error(combined):
        result["needs"] = "enable_https"
        result["error"] = combined[:400]
    elif rc != 0:
        result["error"] = combined[:400] or f"serve exited {rc}"
    return result


def disable() -> dict:
    """Turn Mobile Command off: tear down the serve config and clear the flag."""
    _set_flag(False)
    if tailscale_bin():
        rc, _out, err = _run_ts(["serve", "reset"], timeout=15)
        if rc != 0:
            _log.warning("Mobile Command: `tailscale serve reset` rc=%s: %s", rc, err[:200])
    return status()


def rearm() -> None:
    """Startup hook (NON-BLOCKING): if the feature was left ON, re-establish the
    HTTPS bridge in a background thread that RETRIES.

    Two reasons this is threaded + retrying rather than a single synchronous attempt:
      1. Reboot race — ``tailscaled`` is frequently not yet "Running" when Flask starts,
         so a one-shot attempt no-ops and the feature stays dark until the next manual
         restart (breaking the "set once, always works" promise). We retry until it's up.
      2. Startup latency — a synchronous attempt shells out up to ~75s (status + serve
         status + serve). Running it on the app-factory thread would stall startup.

    Best-effort and silent — never raises into the app factory.
    """
    try:
        if not is_flag_enabled() or not tailscale_bin():
            return
    except Exception:  # noqa: BLE001
        return
    threading.Thread(target=_rearm_loop, name="mobile-command-rearm", daemon=True).start()


def _rearm_loop() -> None:
    """Retry _rearm_once for ~2 minutes to survive the reboot race, then give up
    (the modal's own retry path takes over when the user next opens it)."""
    port = configured_port()
    attempts = 24  # 24 * 5s ≈ 2 min
    for i in range(attempts):
        try:
            if _rearm_once(port):
                return
        except Exception:  # noqa: BLE001
            _log.exception("Mobile Command: rearm attempt failed (non-fatal)")
        if i < attempts - 1:
            time.sleep(5)   # don't sleep after the final attempt


def _rearm_once(port: int) -> bool:
    """One re-arm attempt.

    Returns True when there's nothing more to do (serving, already serving, or certs
    are off so retrying is pointless), False when the caller should retry (tailscaled
    not up yet, or the serve command failed transiently).
    """
    st = _status_json()
    if _backend_state(st) != "Running":
        return False   # tailscaled not up yet (reboot race) — retry
    if not _https_available(st):
        return True    # certs off — stop; the modal guides the one-time toggle
    if _serve_targets_port(port):
        return True    # already serving over HTTPS — nothing to do
    rc, _out, err = _run_ts(["serve", "--bg", str(port)], timeout=45)
    if rc == 0:
        _log.info("Mobile Command: re-armed HTTPS bridge on port %s at startup", port)
        _warm_https_cert(_dns_from_status(st))
        return True
    _log.warning("Mobile Command: startup re-arm failed rc=%s: %s", rc, err[:200])
    return False   # transient — retry


def _dns_from_status(status: Optional[dict]) -> str:
    """This node's MagicDNS name (no trailing dot), or '' if unavailable."""
    self_node = (status or {}).get("Self") or {}
    return (self_node.get("DNSName") or "").rstrip(".")


def _warm_https_cert(dns: str) -> None:
    """Best-effort, non-blocking pre-provision of the ts.net TLS cert.

    Tailscale issues the cert lazily on the first HTTPS connection, which can take
    several seconds. We absorb that here by making a throwaway HTTPS request to our own
    tailnet name in a daemon thread, so the user's FIRST phone hit doesn't time out.
    Failures are ignored — the phone would simply trigger provisioning itself, as before.
    """
    if not _WARM_CERT_ENABLED or not dns:
        return

    def _go() -> None:
        import ssl
        import urllib.request
        url = f"https://{dns}/"
        ctx = ssl.create_default_context()
        for _ in range(3):
            try:
                urllib.request.urlopen(url, timeout=30, context=ctx)  # noqa: S310 (own tailnet)
                return
            except Exception:  # noqa: BLE001
                time.sleep(3)  # provisioning still in flight — retry

    threading.Thread(target=_go, name="mobile-command-warm", daemon=True).start()


# ---------------------------------------------------------------------------
# Aggregate status for the UI
# ---------------------------------------------------------------------------

def status(port: Optional[int] = None) -> dict:
    """Everything the UI needs in one structured dict.

    Keys:
      enabled       -- the persisted ON/OFF flag
      installed     -- is the tailscale CLI present
      logged_in     -- BackendState == Running
      backend_state -- raw BackendState string
      serving       -- is our local port currently served on the tailnet
      url           -- phone-facing HTTPS URL (or None)
      needs         -- next action for the UI: None | 'install_tailscale'
                       | 'tailscale_login' | 'enable_https' | 'starting'
      https_help    -- admin URL for enabling HTTPS certs
      device_name   -- Home-Screen label for THIS computer (hostname by default)
    """
    port = int(port or configured_port())
    installed = bool(tailscale_bin())
    st = _status_json() if installed else None
    backend = _backend_state(st)
    logged_in = backend == "Running"
    https_ok = bool(installed and logged_in and _https_available(st))
    serving = bool(installed and logged_in and _serve_targets_port(port))
    url = tailnet_url(st) if logged_in else None
    enabled = is_flag_enabled()

    needs = None
    if not installed:
        needs = "install_tailscale"
    elif not logged_in:
        needs = "tailscale_login"
    elif enabled and not https_ok:
        # Turned on, but the tailnet's one-time HTTPS-certs switch is still off.
        needs = "enable_https"
    elif enabled and not serving:
        needs = "starting"

    return {
        "enabled": enabled,
        "installed": installed,
        "logged_in": logged_in,
        "backend_state": backend,
        "serving": serving,
        "url": url,
        "port": port,
        "needs": needs,
        "https_help": HTTPS_HELP_URL,
        "device_name": device_name(),
    }
