"""Only the owner's tailnet login counts as the owner (src/web/mobile_auth.py).

``tailscale serve`` names the signed-in person of each device it relays. A
device shared in from someone else's tailnet carries their login; before this
check it counted as the owner and reached every session and command route.
"""

from __future__ import annotations

import json

import pytest

OWNER = "owner@example.com"
FRIEND = "friend@example.org"


def _phone(login):
    return {"X-Forwarded-For": "100.64.0.9", "Tailscale-User-Login": login,
            "Host": "box.tail1234.ts.net"}


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    import web.config as config_mod
    from web import create_app, mobile_auth

    cfg = tmp_path / "kanban_config.json"
    cfg.write_text(json.dumps({"kanban_backend": "sqlite"}), encoding="utf-8")
    monkeypatch.setattr(config_mod, "_KANBAN_CONFIG_FILE", cfg)
    config_mod._kanban_config_cache = None
    monkeypatch.setattr(mobile_auth, "PAIR_FILE", tmp_path / "data" / "mobile_pair.json")
    monkeypatch.setattr(mobile_auth, "OWNER_FILE", tmp_path / "data" / "mobile_owner.json")
    mobile_auth._owner["login"] = None
    mobile_auth._state_cache.update(mtime=None, state={})
    mobile_auth._mobile_host.update(host="", checked=1e18, refreshing=False)
    application = create_app(testing=True)
    with application.test_client() as client:
        yield application, client, mobile_auth
    mobile_auth._owner["login"] = None
    config_mod._kanban_config_cache = None


def test_the_owners_phone_still_reaches_everything(app_client):
    app, client, ma = app_client
    ma.set_owner_login(OWNER)
    assert client.get("/api/ping", headers=_phone(OWNER)).status_code == 200
    with app.test_request_context("/api/shutdown", headers=_phone(OWNER.upper())):
        assert ma.request_is_owner()


def test_another_persons_device_is_refused_everywhere_but_the_open_paths(app_client):
    app, client, ma = app_client
    ma.set_owner_login(OWNER)
    r = client.get("/api/sessions", headers=_phone(FRIEND))
    assert r.status_code == 403 and r.get_json()["error"] == "not_owner"
    assert client.get("/", headers=_phone(FRIEND)).status_code == 403
    assert client.get("/api/ping", headers=_phone(FRIEND)).status_code == 200
    with app.test_request_context("/api/shutdown", headers=_phone(FRIEND)):
        assert not ma.request_is_owner()
    assert client.get("/api/orgs/me", headers=_phone(FRIEND)).status_code == 403


def test_the_socket_refuses_another_persons_device(app_client):
    app, _client, ma = app_client
    ma.set_owner_login(OWNER)
    with app.test_request_context("/socket.io/", headers=_phone(FRIEND)):
        assert ma.socket_connection_allowed(app) == (False, "not_owner")
    with app.test_request_context("/socket.io/", headers=_phone(OWNER)):
        assert ma.socket_connection_allowed(app)[0]


def test_the_owner_login_survives_a_restart(app_client, tmp_path):
    _app, _client, ma = app_client
    ma.set_owner_login(OWNER)
    ma._owner["login"] = None
    assert ma.owner_login() == OWNER


def test_an_unknown_owner_keeps_the_earlier_behaviour(app_client):
    _app, client, _ma = app_client
    assert client.get("/api/sessions", headers=_phone(FRIEND)).status_code != 403
