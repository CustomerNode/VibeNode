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

    def transcript(self, sid, lines, cwd="/work/Acme"):
        p = self.projects / "-proj" / f"{sid}.jsonl"
        with p.open("a", encoding="utf-8") as fh:
            for text in lines:
                fh.write(json.dumps({"type": "user", "cwd": cwd,
                                     "message": {"role": "user", "content": text}}) + "\n")


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
    # What the cloud storage holds is locked: neither the words nor the hidden
    # key appear in the stored posts.
    stored = json.dumps(phone.read(g["share_id"]))
    assert "why does the build fail" not in stored and '"enc"' in stored
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


def test_the_note_travels_both_ways_and_stays_editable(world):
    app, client, mods, dana, sam = world
    phone = mods[2]
    sam_id = _org(client, mods, dana, sam, "Notes")
    dana.use(mods)
    dana.transcript("s-9", ["hello"])
    g = _ok(client.post("/api/phone/call", json={"session_id": "s-9", "guest_id": sam_id, "level": "view",
                                                  "duration": "1h", "note": "CI fails on Node 20"}))["grant"]
    phone.notes_once()
    sam.use(mods)
    assert _ok(client.get("/api/phone/in?v=2"))["grants"][0]["note"] == "CI fails on Node 20"
    phone.notes_once()
    _ok(client.post(f"/api/phone/note/{g['grant_id']}", json={"note": "Lockfile, fixed"}))
    dana.use(mods)
    seen = []
    phone.notes_once(notify=lambda e, d: seen.append((e, d["note"], d["note_by"])))
    assert seen == [("pf_note", "Lockfile, fixed", "Sam")]
    _ok(client.post(f"/api/phone/revoke/{g['grant_id']}"))
    assert _ok(client.post(f"/api/phone/note/{g['grant_id']}", json={"note": "Done, thanks"}))["grant"]["note"] == "Done, thanks"


def test_the_friend_sees_the_project_the_session_is_in(world):
    app, client, mods, dana, sam = world
    sam_id = _org(client, mods, dana, sam, "Projects")
    dana.use(mods)
    dana.transcript("s-10", ["hello"], cwd="/home/someone/code/CustomerNode")
    g = _ok(client.post("/api/phone/call", json={"session_id": "s-10", "guest_id": sam_id,
                                                  "level": "view", "duration": "1h", "title": "Login bug"}))["grant"]
    assert g["project"] == "CustomerNode"
    sam.use(mods)
    assert _ok(client.get("/api/phone/in?v=2"))["grants"][0]["project"] == "CustomerNode"


def _use_mirror(node, monkeypatch):
    import web.config as cfg
    import web.routes.project_api as project_api
    from web.orgs import mirror
    monkeypatch.setattr(cfg, "_CLAUDE_PROJECTS", node.projects)
    monkeypatch.setattr(project_api, "_CLAUDE_PROJECTS", node.projects, raising=False)
    monkeypatch.setattr(cfg, "_PROJECT_NAMES_FILE", node.dir / "project_names.json")
    # The project list shows only folders under home (each test has its own).
    from pathlib import Path
    monkeypatch.setattr(mirror, "ROOT", Path.home() / node.name / "vibenode-phoned")
    return mirror


def test_the_friend_gets_a_real_session_that_never_runs(world, monkeypatch):
    import web.config as cfg
    app, client, mods, dana, sam = world
    sam_id = _org(client, mods, dana, sam, "Mirror")
    dana.use(mods)
    dana.transcript("s-11", ["why does login fail", "the key is " + FAKE_API], cwd="/home/someone/code/Acme")
    g = _ok(client.post("/api/phone/call", json={"session_id": "s-11", "guest_id": sam_id, "level": "drive",
                                                  "duration": "1h", "title": "Login bug"}))["grant"]
    # Sam's VibeNode writes the thread into a session of its own.
    sam.use(mods)
    mirror = _use_mirror(sam, monkeypatch)
    pushed = []
    assert mirror.sync_once(notify=lambda e, d: pushed.append(e)) >= 2
    path = sam.projects / mirror.project_encoded("Dana") / f"{g['share_id']}.jsonl"
    text = path.read_text()
    assert '"customTitle": "Acme · Login bug"' in text and "why does login fail" in text and FAKE_API not in text
    assert "session_entry" in pushed
    # It is a normal project, first in the list, named after who phoned.
    projects = _ok(client.get("/api/projects"))
    assert projects[0]["encoded"] == mirror.project_encoded("Dana")
    assert projects[0]["custom_name"] == "Dana phoned you" and projects[0]["session_count"] == 1
    assert mirror.is_mirror(g["share_id"])
    # A reply typed in it goes to Dana through the call, never to a session here.
    sent, words = mirror.reply(g["share_id"], "try the tests")
    assert sent and "Dana" in words
    dana.use(mods)
    sm = MagicMock(); sm.has_session.return_value = True
    assert mods[2].inbox_once(sm=sm) == 1
    # New lines arrive on the next sync; once the call is gone, so is the project.
    dana.transcript("s-11", ["second question"], cwd="/home/someone/code/Acme")
    mods[2].publish_once()
    sam.use(mods); _use_mirror(sam, monkeypatch)
    mirror.sync_once()
    assert "second question" in path.read_text()
    dana.use(mods)
    _ok(client.post(f"/api/phone/revoke/{g['grant_id']}"))
    sam.use(mods); _use_mirror(sam, monkeypatch)
    mirror.sync_once()
    sent, words = mirror.reply(g["share_id"], "late")
    assert not sent and "ended" in words
    # Once the call is gone from the list, the project goes entirely, even with
    # VibeNode's own bookkeeping files in its folder.
    d = sam.projects / mirror.project_encoded("Dana")
    (d / "_session_holds.json").write_text("{}")
    monkeypatch.setattr(mods[2], "incoming", lambda days=7: [])
    mirror.sync_once()
    assert not d.exists()
    assert all(not p["encoded"].startswith(mirror.project_prefix()) for p in _ok(client.get("/api/projects")))


def test_the_socket_never_sends_a_phoned_session_to_the_session_engine(world, monkeypatch):
    from web.routes import ws_events
    from web.orgs import mirror
    monkeypatch.setattr(mirror, "is_mirror", lambda sid: sid == "m-1")
    monkeypatch.setattr(mirror, "reply", lambda sid, text: (True, "Sent to Dana's session."))
    emitted = []
    monkeypatch.setattr(ws_events, "emit", lambda ev, data=None, **k: emitted.append((ev, data)))
    assert ws_events._phoned_session_reply("m-1", "hello") is True
    assert ("pf_notice", {"session_id": "m-1", "ok": True, "message": "Sent to Dana's session."}) in emitted
    assert any(ev == "session_state" and d["state"] == "idle" for ev, d in emitted)
    assert ws_events._phoned_session_reply("other", "hello") is False


def test_an_old_tab_gets_no_calls_to_draw_its_own_rows(world):
    app, client, mods, dana, sam = world
    sam_id = _org(client, mods, dana, sam, "Old tab")
    dana.use(mods)
    dana.transcript("s-12", ["hello"])
    _ok(client.post("/api/phone/call", json={"session_id": "s-12", "guest_id": sam_id, "level": "view", "duration": "1h"}))
    sam.use(mods)
    assert _ok(client.get("/api/phone/in?days=7"))["grants"] == []
    assert len(_ok(client.get("/api/phone/in?days=7&v=2"))["grants"]) == 1


def test_a_phoned_project_is_marked_for_its_phone_icon(world, monkeypatch):
    app, client, mods, dana, sam = world
    sam_id = _org(client, mods, dana, sam, "Icon")
    dana.use(mods)
    dana.transcript("s-13", ["hello"])
    _ok(client.post("/api/phone/call", json={"session_id": "s-13", "guest_id": sam_id, "level": "view", "duration": "1h"}))
    sam.use(mods)
    mirror = _use_mirror(sam, monkeypatch)
    mirror.sync_once()
    projects = _ok(client.get("/api/projects"))
    assert projects[0].get("phoned") is True
    assert all("phoned" not in p for p in projects[1:])


def test_one_tick_is_one_round_trip_and_carries_status(world, monkeypatch):
    import httpx
    app, client, mods, dana, sam = world
    phone = mods[2]
    sam_id = _org(client, mods, dana, sam, "Tick")
    dana.use(mods)
    dana.transcript("s-14", ["hello"])
    _ok(client.post("/api/phone/call", json={"session_id": "s-14", "guest_id": sam_id, "level": "drive", "duration": "1h"}))
    calls = []
    real = httpx.post
    monkeypatch.setattr(httpx, "post", lambda url, **k: (calls.append(url.rsplit("/", 1)[1]), real(url, **k))[1])
    # Dana's side, session working: one sync, one post with the status.
    phone.tick(status_of=lambda sid: "working")
    assert calls.count("pf_sync") == 1 and calls.count("pf_post") == 1
    calls.clear()
    phone.tick(status_of=lambda sid: "working")            # nothing changed: only the sync
    assert calls == ["pf_sync"]
    # Sam's side: the copy shows Dana's session working, from the same one call.
    sam.use(mods)
    mirror = _use_mirror(sam, monkeypatch)
    seen = []
    calls.clear()
    phone.tick(notify=lambda e, d: seen.append((e, d.get("state"))))
    assert calls == ["pf_sync"]
    assert ("session_state", "working") in seen
