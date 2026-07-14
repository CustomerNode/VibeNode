"""
Tests for app/mobile_command.py — the private phone-access (Tailscale serve) core.

The Tailscale CLI is mocked via monkeypatching `_run_ts`, so these run anywhere,
with or without Tailscale installed. Config is redirected to a temp file through
the VIBENODE_CONFIG env var that app.config already honors.

Node/tailnet identifiers below are deliberately fake placeholders (this is a public
repo) — do NOT paste a real MagicDNS name or tailnet suffix into these fixtures.
"""

import json

import pytest

# Fake, obviously-synthetic identifiers — never a real tailnet.
_DNS = "demohost.tailnet-example.ts.net"
_SUFFIX = "tailnet-example.ts.net"


@pytest.fixture()
def mc(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBENODE_CONFIG", str(tmp_path / "kanban_config.json"))
    import importlib
    import app.config as cfg
    importlib.reload(cfg)
    import app.mobile_command as m
    importlib.reload(m)
    m._WARM_CERT_ENABLED = False   # keep unit runs network-free (no cert warm-up thread)
    return m


def _status_running(certs=True):
    st = {
        "BackendState": "Running",
        "MagicDNSSuffix": _SUFFIX,
        "Self": {"DNSName": _DNS + ".", "Online": True},
    }
    if certs:
        # CertDomains present => tailnet HTTPS-certs feature is ON (serve can do TLS).
        st["CertDomains"] = [_DNS]
    return st


def _fake_ts(mapping):
    """Build a _run_ts stand-in from a dict of arg-tuple -> (rc, out, err)."""
    def _fn(args, timeout=15):
        key = tuple(args)
        if key in mapping:
            return mapping[key]
        # match on first token for coarse cases
        for k, v in mapping.items():
            if k and k[0] == args[0]:
                return v
        return (1, "", "unexpected")
    return _fn


# ---------------------------------------------------------------------------
# URL derivation
# ---------------------------------------------------------------------------

def test_tailnet_url_strips_trailing_dot(mc, monkeypatch):
    monkeypatch.setattr(mc, "_run_ts", _fake_ts({
        ("status", "--json"): (0, json.dumps(_status_running()), ""),
    }))
    monkeypatch.setattr(mc, "tailscale_bin", lambda: "tailscale")
    url = mc.tailnet_url()
    assert url == f"https://{_DNS}/"


# ---------------------------------------------------------------------------
# status(): the UI contract
# ---------------------------------------------------------------------------

def test_status_when_not_installed(mc, monkeypatch):
    monkeypatch.setattr(mc, "tailscale_bin", lambda: None)
    s = mc.status()
    assert s["installed"] is False
    assert s["needs"] == "install_tailscale"
    assert s["url"] is None


def test_status_when_logged_out(mc, monkeypatch):
    monkeypatch.setattr(mc, "tailscale_bin", lambda: "tailscale")
    monkeypatch.setattr(mc, "_run_ts", _fake_ts({
        ("status", "--json"): (0, json.dumps({"BackendState": "NeedsLogin"}), ""),
    }))
    s = mc.status()
    assert s["installed"] is True
    assert s["logged_in"] is False
    assert s["needs"] == "tailscale_login"


def test_status_running_and_serving(mc, monkeypatch):
    serve_json = json.dumps({
        "Web": {f"{_DNS}:443": {
            "Handlers": {"/": {"Proxy": "http://127.0.0.1:5050"}}}}
    })
    monkeypatch.setattr(mc, "tailscale_bin", lambda: "tailscale")
    monkeypatch.setattr(mc, "_run_ts", _fake_ts({
        ("status", "--json"): (0, json.dumps(_status_running()), ""),
        ("serve", "status", "--json"): (0, serve_json, ""),
    }))
    s = mc.status(port=5050)
    assert s["logged_in"] is True
    assert s["serving"] is True
    assert s["url"] == f"https://{_DNS}/"
    assert s["needs"] is None


def test_status_stale_http_serve_is_not_counted_as_serving(mc, monkeypatch):
    # A leftover serve from the pre-HTTPS version listens on :80 and proxies the SAME
    # local port. It must NOT be mistaken for a working HTTPS serve, or the user gets a
    # confident "On" whose https QR is broken (D4). serving must be False here.
    stale_http = json.dumps({
        "Web": {f"{_DNS}:80": {
            "Handlers": {"/": {"Proxy": "http://127.0.0.1:5050"}}}}
    })
    monkeypatch.setattr(mc, "tailscale_bin", lambda: "tailscale")
    monkeypatch.setattr(mc, "_run_ts", _fake_ts({
        ("status", "--json"): (0, json.dumps(_status_running()), ""),
        ("serve", "status", "--json"): (0, stale_http, ""),
    }))
    mc._set_flag(True, port=5050)
    s = mc.status(port=5050)
    assert s["serving"] is False        # :80 listener does not count
    assert s["needs"] == "starting"


def test_serve_targets_port_counts_non_http_listener(mc, monkeypatch):
    # Robustness: the gate EXCLUDES :80 rather than REQUIRING :443, so a valid HTTPS
    # serve is still counted even if Tailscale keys the Web map differently than we
    # assume (e.g. a bare host with no explicit port). Guards against a false-negative
    # perpetual-"starting" if the JSON shape surprises us.
    serve_json = json.dumps({
        "Web": {_DNS: {"Handlers": {"/": {"Proxy": "http://127.0.0.1:5050"}}}}
    })
    monkeypatch.setattr(mc, "tailscale_bin", lambda: "tailscale")
    monkeypatch.setattr(mc, "_run_ts", _fake_ts({
        ("status", "--json"): (0, json.dumps(_status_running()), ""),
        ("serve", "status", "--json"): (0, serve_json, ""),
    }))
    assert mc._serve_targets_port(5050) is True


def test_status_running_but_not_serving_when_enabled(mc, monkeypatch):
    monkeypatch.setattr(mc, "tailscale_bin", lambda: "tailscale")
    monkeypatch.setattr(mc, "_run_ts", _fake_ts({
        ("status", "--json"): (0, json.dumps(_status_running()), ""),
        ("serve", "status", "--json"): (0, "", ""),  # nothing served
    }))
    mc._set_flag(True, port=5050)
    s = mc.status(port=5050)
    assert s["serving"] is False
    assert s["needs"] == "starting"


def test_status_needs_enable_https_when_certs_off(mc, monkeypatch):
    # Logged in + enabled, but the tailnet's HTTPS-certs switch is off (no CertDomains).
    monkeypatch.setattr(mc, "tailscale_bin", lambda: "tailscale")
    monkeypatch.setattr(mc, "_run_ts", _fake_ts({
        ("status", "--json"): (0, json.dumps(_status_running(certs=False)), ""),
        ("serve", "status", "--json"): (0, "", ""),
    }))
    mc._set_flag(True, port=5050)
    s = mc.status(port=5050)
    assert s["needs"] == "enable_https"


# ---------------------------------------------------------------------------
# _https_available: the CertDomains gate across shapes
# ---------------------------------------------------------------------------

def test_device_name_defaults_to_short_hostname(mc, monkeypatch):
    monkeypatch.setattr(mc.socket, "gethostname", lambda: "Studio-PC.local")
    assert mc.device_name() == "Studio-PC"   # domain suffix stripped


def test_set_device_name_persists_and_resets(mc):
    assert mc.set_device_name("  Work Mac  ") == "Work Mac"          # trimmed
    assert mc.device_name() == "Work Mac"
    assert mc.get_kanban_config().get("mobile_command_device_name") == "Work Mac"
    # empty clears the override -> back to the hostname default
    assert mc.set_device_name("") == mc._default_device_name()
    assert "mobile_command_device_name" not in mc.get_kanban_config()


def test_status_includes_device_name(mc, monkeypatch):
    monkeypatch.setattr(mc, "tailscale_bin", lambda: None)
    mc.set_device_name("Studio")
    assert mc.status()["device_name"] == "Studio"


def test_device_name_flows_into_page_and_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBENODE_CONFIG", str(tmp_path / "kanban_config.json"))
    import importlib
    import app.config as cfg
    import app.mobile_command as m
    importlib.reload(cfg)
    importlib.reload(m)
    from app import create_app
    c = create_app(testing=True).test_client()
    c.post("/api/mobile/name", json={"name": "Studio Mac"})
    # per-machine name reaches the dynamic manifest...
    r = c.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert "manifest" in r.headers.get("Content-Type", "")
    assert r.get_json(force=True)["name"] == "Studio Mac"
    # ...and the served page's iOS Add-to-Home-Screen title
    body = c.get("/").get_data(as_text=True)
    assert 'apple-mobile-web-app-title" content="Studio Mac"' in body


@pytest.mark.parametrize("certdomains,expected", [
    ({}, False),                       # key missing (dict without CertDomains)
    ({"CertDomains": None}, False),    # explicit null
    ({"CertDomains": []}, False),      # empty list
    ({"CertDomains": [_DNS]}, True),   # populated
])
def test_https_available_gate(mc, certdomains, expected):
    st = {"BackendState": "Running", "Self": {"DNSName": _DNS + "."}}
    st.update(certdomains)
    assert mc._https_available(st) is expected


# ---------------------------------------------------------------------------
# enable / disable / persistence
# ---------------------------------------------------------------------------

def test_enable_persists_flag_even_without_tailscale(mc, monkeypatch):
    monkeypatch.setattr(mc, "tailscale_bin", lambda: None)
    assert mc.is_flag_enabled() is False
    mc.enable(port=5050)
    assert mc.is_flag_enabled() is True          # flag persists for rearm()
    assert mc.configured_port() == 5050


def test_enable_runs_serve_when_running(mc, monkeypatch):
    calls = []

    def rec(args, timeout=15):
        calls.append(tuple(args))
        if tuple(args) == ("status", "--json"):
            return (0, json.dumps(_status_running()), "")
        if tuple(args) == ("serve", "--bg", "5050"):
            return (0, "", "")
        if tuple(args) == ("serve", "status", "--json"):
            return (0, json.dumps({"Web": {f"{_DNS}:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:5050"}}}}}), "")
        return (0, "", "")

    monkeypatch.setattr(mc, "tailscale_bin", lambda: "tailscale")
    monkeypatch.setattr(mc, "_run_ts", rec)
    s = mc.enable(port=5050)
    assert ("serve", "--bg", "5050") in calls
    assert s["serving"] is True


def test_enable_timeout_surfaces_error_and_not_serving(mc, monkeypatch):
    # A serve that times out (rc=124) must surface an error the UI can show, and leave
    # serving False -> the modal shows the recoverable "starting" panel, not a lie (D2).
    def rec(args, timeout=15):
        if tuple(args) == ("status", "--json"):
            return (0, json.dumps(_status_running()), "")
        if tuple(args) == ("serve", "--bg", "5050"):
            return (124, "", "tailscale command timed out")
        if tuple(args) == ("serve", "status", "--json"):
            return (0, "", "")  # never came up
        return (0, "", "")

    monkeypatch.setattr(mc, "tailscale_bin", lambda: "tailscale")
    monkeypatch.setattr(mc, "_run_ts", rec)
    s = mc.enable(port=5050)
    assert s["serving"] is False
    assert s["needs"] == "starting"          # recoverable, not enable_https
    assert "timed out" in (s.get("error") or "")


def test_enable_needs_https_when_certs_disabled(mc, monkeypatch):
    # When the tailnet has no HTTPS certs, enable() must NOT attempt an HTTPS serve
    # (it would block); it routes the user to the one-time admin toggle instead.
    calls = []

    def rec(args, timeout=15):
        calls.append(tuple(args))
        if tuple(args) == ("status", "--json"):
            return (0, json.dumps(_status_running(certs=False)), "")
        return (0, "", "")

    monkeypatch.setattr(mc, "tailscale_bin", lambda: "tailscale")
    monkeypatch.setattr(mc, "_run_ts", rec)
    s = mc.enable(port=5050)
    assert s["needs"] == "enable_https"
    assert "https_help" in s and s["https_help"].startswith("https://")
    assert not any(c[:2] == ("serve", "--bg") for c in calls)  # never blindly serves


def test_disable_clears_flag_and_resets(mc, monkeypatch):
    calls = []
    monkeypatch.setattr(mc, "tailscale_bin", lambda: "tailscale")
    monkeypatch.setattr(mc, "_run_ts", lambda args, timeout=15: (calls.append(tuple(args)), (0, "", ""))[1])
    mc._set_flag(True, port=5050)
    mc.disable()
    assert mc.is_flag_enabled() is False
    assert ("serve", "reset") in calls


# ---------------------------------------------------------------------------
# rearm(): startup re-establish. rearm() itself is now non-blocking (spawns a
# retrying thread), so the synchronous unit _rearm_once() is what we assert on.
# ---------------------------------------------------------------------------

def test_rearm_noop_when_disabled(mc, monkeypatch):
    called = []
    monkeypatch.setattr(mc, "tailscale_bin", lambda: "tailscale")
    monkeypatch.setattr(mc, "_run_ts", lambda args, timeout=15: (called.append(tuple(args)), (0, "", ""))[1])
    mc._set_flag(False)
    mc.rearm()
    assert called == []  # never touches tailscale when off (no thread spawned)


def test_rearm_once_serves_when_enabled_and_not_serving(mc, monkeypatch):
    calls = []

    def rec(args, timeout=15):
        calls.append(tuple(args))
        if tuple(args) == ("status", "--json"):
            return (0, json.dumps(_status_running()), "")
        if tuple(args) == ("serve", "status", "--json"):
            return (0, "", "")  # not serving yet
        return (0, "", "")

    monkeypatch.setattr(mc, "tailscale_bin", lambda: "tailscale")
    monkeypatch.setattr(mc, "_run_ts", rec)
    mc._set_flag(True, port=5050)
    done = mc._rearm_once(5050)
    assert done is True
    assert ("serve", "--bg", "5050") in calls


def test_rearm_once_retries_when_backend_not_running(mc, monkeypatch):
    # Reboot race: tailscaled not "Running" yet -> _rearm_once returns False (retry) and
    # must NOT attempt a serve (D1 precondition).
    calls = []

    def rec(args, timeout=15):
        calls.append(tuple(args))
        if tuple(args) == ("status", "--json"):
            return (0, json.dumps({"BackendState": "Stopped"}), "")
        return (0, "", "")

    monkeypatch.setattr(mc, "tailscale_bin", lambda: "tailscale")
    monkeypatch.setattr(mc, "_run_ts", rec)
    done = mc._rearm_once(5050)
    assert done is False
    assert not any(c[:2] == ("serve", "--bg") for c in calls)


def test_rearm_once_stops_when_certs_off(mc, monkeypatch):
    # Certs off -> retrying is pointless; _rearm_once returns True (stop) without serving.
    calls = []

    def rec(args, timeout=15):
        calls.append(tuple(args))
        if tuple(args) == ("status", "--json"):
            return (0, json.dumps(_status_running(certs=False)), "")
        return (0, "", "")

    monkeypatch.setattr(mc, "tailscale_bin", lambda: "tailscale")
    monkeypatch.setattr(mc, "_run_ts", rec)
    done = mc._rearm_once(5050)
    assert done is True
    assert not any(c[:2] == ("serve", "--bg") for c in calls)
