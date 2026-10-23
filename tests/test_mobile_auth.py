"""Two tiers of access to the web server (app/mobile_auth.py).

Mobile Command exposes the server to the user's tailnet, and until this module
reaching the tailnet was the whole check. Now every page and /api/* route needs
the paired tier (a local caller, or the vn_pair cookie), routes that run
commands on this machine are local-only whatever the config says, and the
Socket.IO handshake applies the same two rules plus an Origin check.

Everything here drives the real app (``create_app(testing=True)``) through the
Flask test client, with the pairing file and kanban_config.json redirected to
tmp. The last block scans the route modules: a handler that runs a command and
is not marked ``@local_only`` fails the suite unless it is listed, with a
reason, in EXEMPT.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]

# How a phone arrives: through `tailscale serve`, which connects from 127.0.0.1
# and marks the request.
PHONE = {"X-Forwarded-For": "100.64.0.5", "Tailscale-User-Login": "someone@example.com",
         "Host": "box.tail1234.ts.net"}
PHONE_PAGE = dict(PHONE, Origin="https://box.tail1234.ts.net")


@pytest.fixture
def auth(tmp_path, monkeypatch):
    """(app, client, mobile_auth): the real app with the pairing file and
    kanban_config.json in tmp, and a fresh rate limiter."""
    import app.config as config_mod
    from app import create_app, mobile_auth

    cfg = tmp_path / "kanban_config.json"
    cfg.write_text(json.dumps({"kanban_backend": "sqlite"}), encoding="utf-8")
    monkeypatch.setattr(config_mod, "_KANBAN_CONFIG_FILE", cfg)
    config_mod._kanban_config_cache = None
    monkeypatch.setattr(mobile_auth, "PAIR_FILE", tmp_path / "data" / "mobile_pair.json")
    mobile_auth._state_cache.update(mtime=None, state={})
    mobile_auth.reset_rate_limit()
    mobile_auth._mobile_host.update(host="", checked=1e18, refreshing=False)   # never shell out

    application = create_app(testing=True)
    with application.test_client() as client:
        yield application, client, mobile_auth
    config_mod._kanban_config_cache = None


def _set_config(tmp_cfg_module, **values):
    import app.config as config_mod
    data = json.loads(config_mod._KANBAN_CONFIG_FILE.read_text(encoding="utf-8"))
    data.update(values)
    config_mod._KANBAN_CONFIG_FILE.write_text(json.dumps(data), encoding="utf-8")
    config_mod._kanban_config_cache = None


def _pair(client, mobile_auth):
    """Pair the test client's cookie jar through the real route."""
    resp = client.post("/api/pair", json={"code": mobile_auth.pair_code()}, headers=PHONE)
    assert resp.status_code == 200 and resp.get_json() == {"ok": True}
    return resp


# ── the gate ────────────────────────────────────────────────────────────────

def test_a_local_caller_passes_without_pairing(auth):
    _app, client, _ = auth
    assert client.get("/api/sessions").status_code == 200
    assert client.get("/").status_code == 200
    # a local browser page: same-origin fetch sends a loopback Origin
    assert client.get("/api/sessions", headers={"Origin": "http://localhost:5050"}).status_code == 200
    assert client.get("/api/sessions", headers={"Origin": "http://127.0.0.1:5050"}).status_code == 200


def test_a_page_on_another_site_in_the_local_browser_is_not_local(auth):
    _app, client, _ = auth
    resp = client.post("/api/open-file", json={"path": "x"}, headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403 and resp.get_json()["error"] == "local_only"
    resp = client.get("/api/sessions", headers={"Origin": "https://evil.example"})
    assert resp.status_code == 401


@pytest.mark.parametrize("headers", [
    {"X-Forwarded-For": "100.64.0.5"},                       # any proxy hop
    {"Tailscale-User-Login": "someone@example.com"},         # the tailscale serve identity header
    {"Host": "box.tail1234.ts.net"},                         # reached through the tailnet name
    {"X-Forwarded-Host": "box.tail1234.ts.net"},
    PHONE,
])
def test_an_unpaired_remote_api_call_gets_401(auth, headers):
    _app, client, _ = auth
    resp = client.get("/api/sessions", headers=headers)
    assert resp.status_code == 401
    assert resp.get_json() == {"ok": False, "error": "pairing_required"}


def test_another_machine_is_not_local_either(auth):
    _app, client, _ = auth
    resp = client.get("/api/sessions", environ_base={"REMOTE_ADDR": "192.168.1.20"})
    assert resp.status_code == 401


def test_an_unpaired_remote_page_load_redirects_to_pair(auth):
    _app, client, _ = auth
    resp = client.get("/", headers=PHONE)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/pair")
    resp = client.get("/chatgpt", headers=PHONE)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/pair")


def test_the_probes_and_the_pairing_routes_are_open(auth):
    _app, client, _ = auth
    assert client.get("/api/ping", headers=PHONE).get_json() == {"ok": True}
    assert client.get("/api/health", headers=PHONE).status_code == 200
    assert client.get("/pair", headers=PHONE).status_code == 200
    assert client.get("/manifest.webmanifest", headers=PHONE).status_code == 200
    # static assets: a 404 is fine, a 401/302 is not
    assert client.get("/static/js/app.js", headers=PHONE).status_code in (200, 304)


def test_a_paired_phone_passes(auth):
    _app, client, mobile_auth = auth
    _pair(client, mobile_auth)
    assert client.get("/api/sessions", headers=PHONE).status_code == 200
    assert client.get("/", headers=PHONE).status_code == 200
    assert client.get("/pair", headers=PHONE).status_code == 302, "already in: sent to the app"


def test_the_cookie_is_the_hmac_of_the_code_and_dies_with_it(auth):
    _app, client, mobile_auth = auth
    resp = _pair(client, mobile_auth)
    set_cookie = resp.headers.get("Set-Cookie", "")
    assert "vn_pair=" + mobile_auth.cookie_value() in set_cookie
    assert "HttpOnly" in set_cookie and "SameSite=Lax" in set_cookie
    assert "Secure" in set_cookie, "the phone always comes over HTTPS (.ts.net host)"
    assert "Max-Age=31536000" in set_cookie
    assert mobile_auth.pair_code() not in set_cookie, "the code itself never travels in the cookie"

    client.post("/api/pair/rotate")            # local caller
    assert client.get("/api/sessions", headers=PHONE).status_code == 401, "rotation logs the phone out"


def test_a_forged_or_stale_cookie_is_refused(auth):
    _app, client, mobile_auth = auth
    client.set_cookie("vn_pair", "0" * 64)
    assert client.get("/api/sessions", headers=PHONE).status_code == 401
    client.set_cookie("vn_pair", mobile_auth.pair_code())      # the code is not the cookie
    assert client.get("/api/sessions", headers=PHONE).status_code == 401


def test_a_cookie_set_on_localhost_is_not_secure(auth):
    _app, client, mobile_auth = auth
    resp = client.post("/api/pair", json={"code": mobile_auth.pair_code()})
    assert "Secure" not in resp.headers.get("Set-Cookie", "")


# ── pairing ─────────────────────────────────────────────────────────────────

def test_the_code_is_ten_unambiguous_characters_stored_under_data(auth, tmp_path):
    _app, _client, mobile_auth = auth
    code = mobile_auth.pair_code()
    assert len(code) == 10 and set(code) <= set(mobile_auth.CODE_ALPHABET)
    assert not set("01OI") & set(mobile_auth.CODE_ALPHABET)
    stored = json.loads((tmp_path / "data" / "mobile_pair.json").read_text(encoding="utf-8"))
    assert stored["token"] == code and len(stored["salt"]) == 64
    assert mobile_auth.pair_code() == code, "generated once, then stable"
    assert mobile_auth.display_code(code) == code[:5] + "-" + code[5:]


def test_the_pair_file_is_gitignored():
    rules = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "data/mobile_pair.json" in rules


def test_the_code_is_matched_ignoring_case_dashes_and_spaces(auth):
    _app, client, mobile_auth = auth
    code = mobile_auth.pair_code()
    typed = code[:5].lower() + " - " + code[5:]
    resp = client.post("/api/pair", json={"code": typed}, headers=PHONE)
    assert resp.status_code == 200


def test_a_wrong_code_is_refused_and_sets_no_cookie(auth):
    _app, client, mobile_auth = auth
    resp = client.post("/api/pair", json={"code": "NOPE"}, headers=PHONE)
    assert resp.status_code == 403 and resp.get_json()["error"] == "bad_code"
    assert "Set-Cookie" not in resp.headers
    assert client.get("/api/sessions", headers=PHONE).status_code == 401


def test_code_attempts_are_rate_limited_per_address(auth):
    _app, client, mobile_auth = auth
    for _ in range(5):
        assert client.post("/api/pair", json={"code": "NOPE"}, headers=PHONE).status_code == 403
    resp = client.post("/api/pair", json={"code": mobile_auth.pair_code()}, headers=PHONE)
    assert resp.status_code == 429 and resp.get_json()["error"] == "rate_limited"
    assert "Set-Cookie" not in resp.headers, "the right code does not get through a locked window"
    other = dict(PHONE, **{"X-Forwarded-For": "100.64.0.6"})
    assert client.post("/api/pair", json={"code": mobile_auth.pair_code()}, headers=other).status_code == 200


def test_one_tap_pairing_sets_the_cookie_and_opens_the_app(auth):
    _app, client, mobile_auth = auth
    resp = client.get("/pair?code=" + mobile_auth.pair_code(), headers=PHONE)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/")
    assert "vn_pair=" in resp.headers.get("Set-Cookie", "")
    assert client.get("/api/sessions", headers=PHONE).status_code == 200

    fresh = _app.test_client()
    resp = fresh.get("/pair?code=WRONG", headers=PHONE)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/pair?error=1")
    page = fresh.get("/pair?error=1", headers=PHONE)
    assert page.status_code == 200 and b"did not match" in page.data


def test_the_pair_page_posts_as_a_form_without_javascript(auth):
    _app, client, mobile_auth = auth
    resp = client.post("/api/pair", data={"code": "WRONG"}, headers=PHONE)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/pair?error=1")
    resp = client.post("/api/pair", data={"code": mobile_auth.pair_code()}, headers=PHONE)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/")
    assert client.get("/api/sessions", headers=PHONE).status_code == 200


def test_only_a_local_caller_rotates_the_code(auth):
    _app, client, mobile_auth = auth
    before = mobile_auth.pair_code()
    _pair(client, mobile_auth)
    resp = client.post("/api/pair/rotate", headers=PHONE)
    assert resp.status_code == 403 and resp.get_json()["error"] == "local_only"
    assert mobile_auth.pair_code() == before

    resp = client.post("/api/pair/rotate")
    body = resp.get_json()
    assert resp.status_code == 200 and body["ok"] is True
    assert body["pair_code"] == mobile_auth.pair_code() != before
    assert body["pair_code_display"] == mobile_auth.display_code(body["pair_code"])


def test_the_modal_sees_the_code_only_from_this_machine(auth, monkeypatch):
    _app, client, mobile_auth = auth
    from app import mobile_command
    status = {"enabled": True, "installed": True, "logged_in": True, "serving": True,
              "url": "https://box.tail1234.ts.net/", "needs": None, "device_name": "Box"}
    monkeypatch.setattr(mobile_command, "status", lambda port=None: dict(status))

    local = client.get("/api/mobile/status").get_json()
    assert local["pair_code"] == mobile_auth.pair_code() and local["auth_enabled"] is True
    assert local["pair_url"] == "https://box.tail1234.ts.net/pair?code=" + mobile_auth.pair_code()

    _pair(client, mobile_auth)
    remote = client.get("/api/mobile/status", headers=PHONE).get_json()
    assert "pair_code" not in remote and "pair_url" not in remote
    assert remote["auth_enabled"] is True and remote["url"] == status["url"]


# ── the emergency switch ────────────────────────────────────────────────────

def test_mobile_command_auth_false_opens_the_gate_but_not_local_only(auth):
    _app, client, mobile_auth = auth
    _set_config(None, mobile_command_auth=False)
    assert mobile_auth.auth_enabled() is False
    assert client.get("/api/sessions", headers=PHONE).status_code == 200
    assert client.get("/", headers=PHONE).status_code == 200
    assert client.get("/pair", headers=PHONE).status_code == 302
    resp = client.post("/api/open-file", json={"path": "x"}, headers=PHONE)
    assert resp.status_code == 403 and resp.get_json()["error"] == "local_only"
    resp = client.post("/api/shutdown", headers=PHONE)
    assert resp.status_code == 403 and resp.get_json()["error"] == "local_only"


# ── local-only routes ───────────────────────────────────────────────────────

def test_install_pack_and_open_file_are_refused_to_a_paired_phone(auth):
    _app, client, mobile_auth = auth
    _pair(client, mobile_auth)
    with patch("app.routes.live_api.subprocess", create=True) as sp:
        resp = client.post("/api/workforce/install-pack",
                           json={"pack_id": "x", "git_url": "https://example.com/x.git", "setup_cmd": "calc"},
                           headers=PHONE_PAGE)
        assert resp.status_code == 403 and resp.get_json()["error"] == "local_only"
        assert not sp.Popen.called and not sp.run.called
    resp = client.post("/api/open-file", json={"path": str(Path.home() / "x.txt")}, headers=PHONE_PAGE)
    assert resp.status_code == 403 and resp.get_json()["error"] == "local_only"


def test_shutdown_and_engine_restart_are_local_only_web_restart_is_not(auth):
    _app, client, mobile_auth = auth
    _pair(client, mobile_auth)
    with patch("app.routes.main.subprocess.Popen") as popen:
        assert client.post("/api/shutdown", headers=PHONE_PAGE).status_code == 403
        for scope in ("daemon", "both"):
            resp = client.post("/api/restart", json={"scope": scope}, headers=PHONE_PAGE)
            assert resp.status_code == 403 and resp.get_json()["error"] == "local_only", scope
        assert not popen.called
        resp = client.post("/api/restart", json={"scope": "web"}, headers=PHONE_PAGE)
        assert resp.status_code == 200 and resp.get_json()["ok"] is True
        assert popen.call_count == 1


def test_local_callers_still_use_the_local_only_routes(auth):
    _app, client, _ = auth
    with patch("app.routes.main.subprocess.Popen") as popen:
        assert client.post("/api/restart", json={"scope": "daemon"}).get_json()["ok"] is True
        assert client.post("/api/shutdown").get_json()["ok"] is True
        assert popen.call_count == 2
    resp = client.post("/api/open-file", json={"path": str(Path.home() / "nope.txt")})
    assert resp.status_code == 404, "past the guard: the route's own answer"


def test_a_paired_phone_may_drop_files_only_into_the_project_or_uploads(auth, tmp_path, monkeypatch):
    import io
    _app, client, mobile_auth = auth
    _pair(client, mobile_auth)
    project = Path.home() / "proj"          # projects live under home; the home check still runs first
    project.mkdir()
    monkeypatch.setattr("app.routes.live_api._get_project_path", lambda: project)
    elsewhere = Path.home() / "elsewhere"
    elsewhere.mkdir()

    def drop(target):
        return client.post("/api/file-drop", data={"file": (io.BytesIO(b"x"), "a.txt"), "target_dir": str(target)},
                           content_type="multipart/form-data", headers=PHONE_PAGE)

    resp = drop(elsewhere)
    assert resp.status_code == 403 and "paired device" in resp.get_json()["error"]
    assert list(elsewhere.iterdir()) == []
    resp = drop(project)
    assert resp.status_code == 200 and (project / "a.txt").read_bytes() == b"x"
    # data/uploads: checked on the helper (in tests the repo is outside the
    # sandboxed home, so the route's home check would refuse it first).
    from app.routes.live_api import _is_remote_drop_target
    assert _is_remote_drop_target((ROOT / "data" / "uploads" / "sub").resolve()) is True
    assert _is_remote_drop_target(elsewhere.resolve()) is False
    # a local caller keeps the folder picker
    resp = client.post("/api/file-drop", data={"file": (io.BytesIO(b"y"), "b.txt"), "target_dir": str(elsewhere)},
                       content_type="multipart/form-data")
    assert resp.status_code == 200


def test_the_script_rule_is_unchanged(auth):
    """``_hook_caller_is_local`` moved to mobile_auth; the routes that import
    it from live_api still get the same function and the same answers."""
    import app.routes.live_api as live_api
    _app, client, mobile_auth = auth
    assert live_api._hook_caller_is_local is mobile_auth.caller_is_local_script
    for kwargs in ({"headers": {"Origin": "http://127.0.0.1:5050"}}, {"headers": PHONE},
                   {"environ_base": {"REMOTE_ADDR": "10.0.0.5"}}):
        resp = client.post("/api/hook/message", json={"session_id": "s", "text": "t"}, **kwargs)
        assert resp.status_code == 403 and resp.get_json()["error"] == "local callers only", kwargs


# ── the secret key ──────────────────────────────────────────────────────────

def test_the_supabase_secret_is_masked_on_get_and_a_masked_put_keeps_it(auth):
    _app, client, _ = auth
    import app.config as config_mod
    assert client.get("/api/kanban/config").get_json()["supabase_secret_key"] == ""
    assert client.get("/api/kanban/config").get_json()["supabase_secret_key_set"] is False

    with patch("app.routes.kanban_api.reset_repository", create=True), \
         patch("app.routes.kanban_api.invalidate_ensured_cache", create=True):
        resp = client.put("/api/kanban/config", json={"supabase_secret_key": "sb_secret_ABCDEFGH1234"})
        assert resp.status_code == 200
        body = client.get("/api/kanban/config").get_json()
        assert body["supabase_secret_key"] == "••••1234" and body["supabase_secret_key_set"] is True
        assert "sb_secret_ABCDEFGH1234" not in json.dumps(body)

        # the settings form round-trips the mask: the stored key must survive
        client.put("/api/kanban/config", json={"supabase_secret_key": body["supabase_secret_key"], "depth_limit": 3})
        stored = json.loads(config_mod._KANBAN_CONFIG_FILE.read_text(encoding="utf-8"))
        assert stored["supabase_secret_key"] == "sb_secret_ABCDEFGH1234" and stored["kanban_depth_limit"] == 3

        # a new key replaces it, an empty string clears it
        client.put("/api/kanban/config", json={"supabase_secret_key": "sb_secret_NEW000009999"})
        assert json.loads(config_mod._KANBAN_CONFIG_FILE.read_text(encoding="utf-8"))["supabase_secret_key"] == "sb_secret_NEW000009999"
        client.put("/api/kanban/config", json={"supabase_secret_key": ""})
        assert json.loads(config_mod._KANBAN_CONFIG_FILE.read_text(encoding="utf-8"))["supabase_secret_key"] == ""


def test_the_settings_form_never_carries_the_key_value():
    """modals.js fills the Secret Key field from this GET: the field must start
    empty, with the mask as its placeholder."""
    src = (ROOT / "static" / "js" / "modals.js").read_text(encoding="utf-8")
    field = re.search(r'<input type="password" id="kb-supa-key"[^>]*>', src).group(0)
    assert 'value=""' in field
    assert "supabase_secret_key_set" in field and "placeholder=" in field
    assert "value=\"${" not in field


# ── Socket.IO ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("origin,allowed", [
    (None, True),
    ("", True),
    ("http://localhost:5050", True),
    ("http://127.0.0.1:5050", True),
    ("https://localhost", True),
    ("http://[::1]:5050", True),
    ("https://box.tail1234.ts.net", True),          # the Mobile Command host (same as the request Host)
    ("https://other.tail1234.ts.net", False),       # another node on the tailnet
    ("https://evil.example", False),
    ("http://localhost.evil.example", False),
    ("file://", False),
    ("null", False),
])
def test_the_origin_predicate(auth, origin, allowed):
    _app, _client, mobile_auth = auth
    environ = {"HTTP_HOST": "box.tail1234.ts.net"}
    assert mobile_auth.origin_allowed(origin, environ) is allowed


def test_the_origin_predicate_knows_the_mobile_host_without_the_request(auth):
    _app, _client, mobile_auth = auth
    mobile_auth.set_known_mobile_host("box.tail1234.ts.net.")
    assert mobile_auth.origin_allowed("https://box.tail1234.ts.net", {"HTTP_HOST": "127.0.0.1:5050"}) is True
    assert mobile_auth.origin_allowed("https://other.tail1234.ts.net", {"HTTP_HOST": "127.0.0.1:5050"}) is False


def test_the_socketio_server_uses_the_origin_predicate(auth):
    _app, _client, mobile_auth = auth
    from app import socketio
    assert socketio.server.eio.cors_allowed_origins is mobile_auth.origin_allowed


def _ws_client(app, headers):
    from app import socketio
    from app.routes.ws_events import register_ws_events
    register_ws_events(socketio, app)
    # flask_test_client: builds the environ like a real request (REMOTE_ADDR
    # 127.0.0.1, as every connection through the local server has).
    client = socketio.test_client(app, headers=headers, flask_test_client=app.test_client())
    try:
        return client.is_connected()
    finally:
        if client.is_connected():
            client.disconnect()


def test_the_socket_handshake_refuses_a_bad_origin_and_an_unpaired_phone(auth):
    app, client, mobile_auth = auth
    assert _ws_client(app, {"Origin": "http://localhost:5050"}) is True
    assert _ws_client(app, {"Origin": "https://evil.example"}) is False
    assert _ws_client(app, PHONE_PAGE) is False, "a phone without the cookie"

    _pair(client, mobile_auth)
    from app import socketio
    from app.routes.ws_events import register_ws_events
    register_ws_events(socketio, app)
    paired = socketio.test_client(app, headers=PHONE_PAGE, flask_test_client=client)
    try:
        assert paired.is_connected() is True
    finally:
        if paired.is_connected():
            paired.disconnect()


def test_the_socket_pairing_rule_is_off_with_the_gate(auth):
    app, _client, _ = auth
    _set_config(None, mobile_command_auth=False)
    assert _ws_client(app, PHONE_PAGE) is True
    assert _ws_client(app, {"Origin": "https://evil.example"}) is False, "the Origin rule still holds"


# ── no command runs behind the paired tier ──────────────────────────────────

# "module.function": why a handler that runs a command may stay on the paired
# tier. Every entry is a deliberate decision; a new handler that runs a command
# must either take @local_only or be added here with its reason.
EXEMPT = {
    # Inline `require_local()` for every scope but "web"; a paired phone may
    # restart the web server (its own UI), never the Session Engine.
    "main.restart_server": "inline require_local() for scope != web",
    # Fixed argv (`claude auth status --json`), read-only, no caller input.
    "auth_api.api_auth_status": "fixed argv, read-only",
    # Read-only git argv (`rev-parse`, `branch`, `status`, `log`) on the
    # active project; no shell, no caller input in the command.
    "git_api.project_git_status": "read-only git argv",
    # `claude -p <prompt>` as an argv list: the prompt is data, not shell.
    # The AI planner is a phone-facing feature.
    "kanban_api.planner_chat": "argv list, prompt is data",
    # Headless Chrome / Playwright on a caller URL, argv list: the mobile
    # visual channel exists for the phone.
    "preview_api.render": "argv list, the phone is the intended caller",
    # pytest as an argv list; owned by another workstream. The pairing gate
    # covers it.
    "test_api.run_tests": "argv list (pytest); covered by the gate",
    "test_api.api_run_tests": "argv list (pytest); covered by the gate",
}

_COMMAND_RE = re.compile(r"shell=True|os\.startfile|subprocess\.")


def _route_handlers(module_path: Path):
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    source = module_path.read_text(encoding="utf-8")
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = []
        is_route = False
        for deco in node.decorator_list:
            target = deco.func if isinstance(deco, ast.Call) else deco
            name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            names.append(name)
            if name == "route":
                is_route = True
        if is_route:
            yield node.name, names, ast.get_source_segment(source, node) or ""


def test_every_route_that_runs_a_command_is_local_only_or_exempt():
    offenders = []
    for module_path in sorted((ROOT / "app" / "routes").glob("*.py")):
        module = module_path.stem
        for name, decorators, body in _route_handlers(module_path):
            if not _COMMAND_RE.search(body):
                continue
            if "local_only" in decorators or f"{module}.{name}" in EXEMPT:
                continue
            offenders.append(f"{module}.{name}")
    assert offenders == [], (
        "route handlers run a command without @local_only (add the decorator, "
        "or an EXEMPT entry with the reason): " + ", ".join(offenders))


def test_the_known_command_routes_are_marked():
    marked = {}
    for module_path in (ROOT / "app" / "routes").glob("*.py"):
        for name, decorators, _body in _route_handlers(module_path):
            marked[f"{module_path.stem}.{name}"] = "local_only" in decorators
    for required in ("live_api.install_pack", "live_api.api_open_file", "main.shutdown_server", "mobile_api.api_pair_rotate"):
        assert marked.get(required) is True, required


# ── Supabase credentials: the masked key is never needed in the browser ──────

def test_supabase_credentials_reuse_saved_key_only_for_the_saved_url(monkeypatch):
    """The settings form never holds the saved key (the GET masks it). An empty
    or masked key for the SAME url uses the stored key; a different url never
    inherits it, so a key is never sent to a server it was not saved for."""
    from app.routes import kanban_api
    import app.config as cfgmod
    saved = {"supabase_url": "https://abc.supabase.co/", "supabase_secret_key": "sk-real-secret-1234"}
    monkeypatch.setattr(cfgmod, "get_kanban_config", lambda: dict(saved))
    creds = kanban_api._supabase_credentials
    assert creds({"supabase_url": "https://abc.supabase.co", "supabase_secret_key": ""}) == \
        ("https://abc.supabase.co", "sk-real-secret-1234")
    masked = kanban_api._mask_secret(saved["supabase_secret_key"])
    assert creds({"supabase_url": "https://abc.supabase.co", "supabase_secret_key": masked})[1] == "sk-real-secret-1234"
    assert creds({"supabase_url": "https://evil.example", "supabase_secret_key": ""}) == ("https://evil.example", "")
    assert creds({"supabase_url": "https://abc.supabase.co", "supabase_secret_key": "new-key"})[1] == "new-key"


def test_auth_login_is_local_only():
    from app import create_app
    app = create_app(testing=True)
    c = app.test_client()
    r = c.post("/api/auth-login", environ_base={"REMOTE_ADDR": "100.64.0.7"},
               headers={"X-Forwarded-For": "100.64.0.7", "Tailscale-User-Login": "x@y"})
    assert r.status_code in (401, 403)
