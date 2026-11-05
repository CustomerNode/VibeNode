"""Phone a Friend end to end: two VibeNodes, one org, a real Postgres.

The org's cloud storage is the migration on a throwaway Postgres behind a
stand-in for Supabase's function endpoint (``POST /rest/v1/rpc/<fn>``): the
project's secret key calls as ``service_role``, any other key as ``anon``,
exactly the roles a real project uses. Each VibeNode is the real app with its
own membership and state files; the test switches between them.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests._pg import Pg, SqlError

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "src" / "web" / "db" / "migrations" / "007_vibenode_orgs.sql"
URL = "https://made-up-project.supabase.co"
SERVICE = "sb_" + "secret_" + "madeupmadeupmadeup"
PUBLISHABLE = "sb_" + "publishable_" + "madeupmadeupmadeup"
# Secret-shaped test text, assembled so no file holds a key-looking string.
FAKE_API = "sk" + "-" + "abcdefghijklmnopqrstuv1234"
FAKE_BEARER = "Bear" + "er " + "abcdefghijklmnopqrstuvwxyz0123"

pytestmark = pytest.mark.skipif(not Pg.available(), reason="PostgreSQL server not installed")


@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    db = Pg(tmp_path_factory.mktemp("pg")).start()
    db.sql(MIGRATION.read_text(encoding="utf-8"))
    yield db
    db.stop()


def _lit(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, (dict, list)):
        return "'" + json.dumps(v).replace("'", "''") + "'::jsonb"
    return "'" + str(v).replace("'", "''") + "'"


class _Resp:
    def __init__(self, code, body):
        self.status_code = code
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return json.loads(self.text)


def _fake_post(pg):
    def post(url, json=None, headers=None, timeout=None):
        assert url.startswith(URL + "/rest/v1/rpc/")
        fn = url.rsplit("/", 1)[1]
        role = "service_role" if headers["apikey"] == SERVICE else "anon"
        args = ", ".join(f"{k} => {_lit(v)}" for k, v in (json or {}).items())
        try:
            out = pg.sql(f"SELECT to_jsonb({fn}({args}));", role=role)
        except SqlError as e:
            return _Resp(400, {"message": str(e)})
        return _Resp(200, out)
    return post


class Node:
    """One VibeNode: its files and its transcripts folder."""

    def __init__(self, tmp: Path, name: str, monkeypatch):
        self.dir = tmp / name
        self.dir.mkdir()
        self.projects = self.dir / "projects"
        (self.projects / "-proj").mkdir(parents=True)
        self.name, self.mp = name, monkeypatch

    def use(self, mods):
        cfg_mod, store, phone = mods
        self.mp.setattr(store, "MEMBERSHIP_FILE", self.dir / "vibenode_orgs.json")
        self.mp.setattr(phone, "STATE_FILE", self.dir / "phone.json")
        self.mp.setattr(phone, "_CLAUDE_PROJECTS", self.projects)
        self.mp.setattr(phone, "source_name", lambda: self.name)
        cfgf = self.dir / "kanban_config.json"
        if not cfgf.exists():
            cfgf.write_text(json.dumps({"kanban_backend": "supabase", "supabase_url": URL,
                                        "supabase_secret_key": SERVICE,
                                        "supabase_publishable_key": PUBLISHABLE}), encoding="utf-8")
        self.mp.setattr(cfg_mod, "_KANBAN_CONFIG_FILE", cfgf)
        cfg_mod._kanban_config_cache = None
        return self

    def transcript(self, sid, lines):
        p = self.projects / "-proj" / f"{sid}.jsonl"
        with p.open("a", encoding="utf-8") as fh:
            for text in lines:
                fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")


@pytest.fixture
def world(pg, tmp_path, monkeypatch):
    import httpx
    import web.config as cfg_mod
    from web import create_app
    from web.orgs import phone, store

    monkeypatch.setattr(httpx, "post", _fake_post(pg))
    mods = (cfg_mod, store, phone)
    app = create_app(testing=True)
    dana = Node(tmp_path, "dana-box", monkeypatch)
    sam = Node(tmp_path, "sam-box", monkeypatch)
    with app.test_client() as client:
        yield app, client, mods, dana, sam
    cfg_mod._kanban_config_cache = None


def _ok(r):
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


def _org(client, mods, dana, sam, name):
    dana.use(mods)
    _ok(client.post("/api/orgs/setup", json={"org_name": name, "username": "Dana"}))
    prompt = _ok(client.post("/api/orgs/invite", json={"days": 1}))["prompt"]
    sam.use(mods)
    return _ok(client.post("/api/orgs/join", json={"username": "Sam", "prompt": prompt}))["member_id"]


def test_set_up_invite_join_call_read_drive_and_revoke(world):
    app, client, mods, dana, sam = world
    _cfg, store, phone = mods

    # Dana starts the org in her cloud storage and copies an invite prompt.
    dana.use(mods)
    me = _ok(client.post("/api/orgs/setup", json={"org_name": "Pair Up", "username": "Dana"}))
    assert me["role"] == "owner" and "token" not in me and "key" not in me
    prompt = _ok(client.post("/api/orgs/invite", json={"days": 3}))["prompt"]
    assert SERVICE not in prompt and PUBLISHABLE in prompt

    # Sam's VibeNode joins with it.
    sam.use(mods)
    joined = _ok(client.post("/api/orgs/join", json={"username": "Sam", "prompt": prompt}))
    assert joined["org_name"] == "Pair Up"
    sam_id = joined["member_id"]

    # Dana phones Sam on one session, at Drive, with a secret in the thread.
    dana.use(mods)
    dana.transcript("s-1", ["why does the build fail", "the key is " + FAKE_API])
    members = _ok(client.get("/api/orgs/members"))["members"]
    assert {m["username"] for m in members} == {"Dana", "Sam"}
    g = _ok(client.post("/api/phone/call", json={"session_id": "s-1", "guest_id": sam_id,
                                                  "level": "drive", "duration": "1h",
                                                  "title": "Stuck build"}))["grant"]
    assert [x["guest"] for x in _ok(client.get("/api/phone/out"))["grants"]] == ["Sam"]

    # Sam gets one ping, reads the thread (secret hidden), and sends a message.
    sam.use(mods)
    pings = []
    phone.pings_once(notify=lambda e, d: pings.append(e))
    phone.pings_once(notify=lambda e, d: pings.append(e))
    assert pings == ["pf_incoming"]
    texts = json.dumps(_ok(client.get(f"/api/phone/read/{g['share_id']}"))["posts"])
    assert "why does the build fail" in texts and FAKE_API not in texts and "[hidden]" in texts
    _ok(client.post(f"/api/phone/send/{g['grant_id']}", json={"text": "run the tests"}))

    # Dana's VibeNode hands it to the session, labelled, once.
    dana.use(mods)
    sm = MagicMock()
    sm.has_session.return_value = True
    assert phone.inbox_once(sm=sm) == 1
    assert phone.inbox_once(sm=sm) == 0
    sid, text = sm.queue_message.call_args[0]
    assert sid == "s-1" and text.startswith("[Sam via Phone a Friend]") and "run the tests" in text

    # New lines are posted on the next tick.
    dana.transcript("s-1", ["try again"])
    assert phone.publish_once() >= 1

    # Revoke: Sam reads nothing more, and his tab is told.
    _ok(client.post(f"/api/phone/revoke/{g['grant_id']}"))
    sam.use(mods)
    assert client.get(f"/api/phone/read/{g['share_id']}").status_code == 403
    ended = []
    phone.pings_once(notify=lambda e, d: ended.append(e))
    assert ended == ["pf_ended"]
    assert client.post(f"/api/phone/send/{g['grant_id']}", json={"text": "late"}).status_code == 403


def test_a_view_call_cannot_write_and_suggest_waits_for_the_owner(world):
    app, client, mods, dana, sam = world
    phone = mods[2]
    sam_id = _org(client, mods, dana, sam, "Two")
    dana.use(mods)
    dana.transcript("s-2", ["hello"])
    gv = _ok(client.post("/api/phone/call", json={"session_id": "s-2", "guest_id": sam_id,
                                                   "level": "view", "duration": "4h"}))["grant"]
    sam.use(mods)
    assert client.post(f"/api/phone/send/{gv['grant_id']}", json={"text": "x"}).status_code == 403
    dana.use(mods)
    gs = _ok(client.post("/api/phone/call", json={"session_id": "s-2", "guest_id": sam_id,
                                                   "level": "suggest", "duration": "4h"}))["grant"]
    sam.use(mods)
    _ok(client.post(f"/api/phone/send/{gs['grant_id']}", json={"text": "maybe check the settings"}))
    dana.use(mods)
    sm = MagicMock()
    phone.inbox_once(sm=sm)
    sm.queue_message.assert_not_called()
    sug = _ok(client.get("/api/phone/suggestions"))["suggestions"]
    assert sug and sug[0]["text"] == "maybe check the settings" and sug[0]["from"] == "Sam"


def test_setup_needs_cloud_storage(world):
    app, client, mods, dana, _sam = world
    dana.use(mods)
    (dana.dir / "kanban_config.json").write_text(json.dumps({"kanban_backend": "sqlite"}))
    mods[0]._kanban_config_cache = None
    r = client.post("/api/orgs/setup", json={"org_name": "X", "username": "Dana"})
    assert r.status_code == 409 and r.get_json()["error"] == "cloud_storage_off"


def test_a_tampered_or_reused_invite_is_refused(world):
    app, client, mods, dana, sam = world
    dana.use(mods)
    _ok(client.post("/api/orgs/setup", json={"org_name": "Three", "username": "Dana"}))
    prompt = _ok(client.post("/api/orgs/invite", json={"days": 1}))["prompt"]
    sam.use(mods)
    bad = prompt.replace('"code": "', '"code": "x')
    assert client.post("/api/orgs/join", json={"username": "Sam", "prompt": bad}).status_code in (400, 410)
    assert client.post("/api/orgs/join", json={"username": "Sam", "prompt": prompt}).status_code == 200
    (sam.dir / "vibenode_orgs.json").unlink()
    assert client.post("/api/orgs/join", json={"username": "Sam2", "prompt": prompt}).status_code == 410


def test_the_routes_refuse_a_page_from_another_site(world):
    _app, client, _mods, _dana, _sam = world
    hdr = {"X-Forwarded-For": "100.64.0.9", "Host": "box.example.ts.net",
           "Origin": "https://elsewhere.example.com"}
    assert client.get("/api/orgs/me", headers=hdr).status_code == 403


def test_secrets_are_hidden_and_long_fields_trimmed():
    from web.orgs.phone import clean_entry
    e = clean_entry({"text": "password=hunter2abc and " + FAKE_BEARER + " ok", "output": "x" * 9000})
    assert "hunter2abc" not in e["text"] and "abcdefghijklmnop" not in e["text"]
    assert len(e["output"]) < 4200 and "not shared" in e["output"]
