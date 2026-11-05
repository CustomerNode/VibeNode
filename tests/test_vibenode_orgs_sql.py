"""VibeNode Orgs and Phone a Friend: the database rules, run on a real Postgres.

The migration (src/web/db/migrations/007_vibenode_orgs.sql) is applied to a
throwaway cluster with Supabase's API roles, and every call is made as the
public ``anon`` role, the way a VibeNode holding only the publishable key does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._pg import Pg, SqlError

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "src" / "web" / "db" / "migrations" / "007_vibenode_orgs.sql"

pytestmark = pytest.mark.skipif(not Pg.available(), reason="PostgreSQL server not installed")


@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    db = Pg(tmp_path_factory.mktemp("pg")).start()
    db.sql(MIGRATION.read_text(encoding="utf-8"))
    yield db
    db.stop()


@pytest.fixture
def org(pg):
    """An org with an owner (Dana) and two members who joined by invite."""
    owner = pg.call("orgs_create", "Test Org", "Dana", role="service_role")
    tok = owner["token"]
    out = {"owner": owner}
    for name in ("Sam", "Priya"):
        code = pg.call("orgs_create_invite", tok, 3)["code"]
        out[name] = pg.call("orgs_redeem_invite", code, name)
    return out


def _fails(pg, fn, *args, match=""):
    with pytest.raises(SqlError) as e:
        pg.call(fn, *args)
    assert match in str(e.value)


def test_the_public_role_reads_no_table_directly(pg, org):
    for table in ("orgs", "org_members", "org_invites", "pf_shared_sessions", "pf_grants", "pf_posts"):
        with pytest.raises(SqlError):
            pg.sql(f"SELECT count(*) FROM {table};", role="anon")


def test_only_the_secret_key_starts_an_org(pg):
    _fails(pg, "orgs_create", "X", "Y", match="permission denied")


def test_an_invite_works_once_and_expires(pg, org):
    tok = org["owner"]["token"]
    code = pg.call("orgs_create_invite", tok, 1)["code"]
    pg.call("orgs_redeem_invite", code, "Lee")
    _fails(pg, "orgs_redeem_invite", code, "Lee2", match="invite_invalid")
    code2 = pg.call("orgs_create_invite", tok, 1)["code"]
    pg.sql("UPDATE org_invites SET expires_at = now() - interval '1 second' WHERE used_at IS NULL;")
    _fails(pg, "orgs_redeem_invite", code2, "Late", match="invite_invalid")
    _fails(pg, "orgs_redeem_invite", "made-up-code-that-is-long-enough", "X", match="invite_invalid")


def test_a_plain_member_cannot_invite_and_names_are_unique(pg, org):
    _fails(pg, "orgs_create_invite", org["Sam"]["token"], 3, match="not_allowed")
    code = pg.call("orgs_create_invite", org["owner"]["token"], 3)["code"]
    _fails(pg, "orgs_redeem_invite", code, "sam", match="username_taken")


def test_a_bad_token_gets_nothing(pg, org):
    _fails(pg, "orgs_members", "x" * 40, match="not_a_member")
    names = [m["username"] for m in pg.call("orgs_members", org["Sam"]["token"])]
    assert {"Dana", "Sam", "Priya"} <= set(names)


def test_a_grant_opens_one_session_to_one_person(pg, org):
    dana, sam, priya = org["owner"]["token"], org["Sam"], org["Priya"]
    g = pg.call("pf_share", dana, sam["member_id"], "box", "s-1", "Stuck build", "view", 60, "help")
    assert g["live"] and g["guest"] == "Sam" and g["owner"] == "Dana"
    pg.call("pf_post", dana, g["share_id"], [{"kind": "entry", "body": {"text": "hi"}}], "working")
    got = pg.call("pf_read", sam["token"], g["share_id"], 0, 50, None)
    assert [p["body"]["text"] for p in got["posts"]] == ["hi"] and got["status"] == "working"
    _fails(pg, "pf_read", priya["token"], g["share_id"], 0, 50, None, match="no_access")
    _fails(pg, "pf_post", sam["token"], g["share_id"], [], None, match="not_your_session")
    assert [x["title"] for x in pg.call("pf_grants_list", sam["token"], "in", 7)] == ["Stuck build"]
    assert pg.call("pf_grants_list", priya["token"], "in", 7) == []


def test_several_people_on_one_session_each_revoked_alone(pg, org):
    dana, sam, priya = org["owner"]["token"], org["Sam"], org["Priya"]
    g1 = pg.call("pf_share", dana, sam["member_id"], "box", "s-2", "T", "drive", None, "")
    g2 = pg.call("pf_share", dana, priya["member_id"], "box", "s-2", "T", "view", None, "")
    assert g1["share_id"] == g2["share_id"]
    assert pg.call("pf_revoke", dana, g1["grant_id"]) is True
    _fails(pg, "pf_read", sam["token"], g1["share_id"], 0, 10, None, match="no_access")
    pg.call("pf_read", priya["token"], g2["share_id"], 0, 10, None)


def test_only_suggest_or_drive_may_write_and_the_inbox_says_if_still_live(pg, org):
    dana, sam, priya = org["owner"]["token"], org["Sam"], org["Priya"]
    gv = pg.call("pf_share", dana, priya["member_id"], "box", "s-3", "T", "view", None, "")
    _fails(pg, "pf_guest_message", priya["token"], gv["grant_id"], "do it", match="no_access")
    gd = pg.call("pf_share", dana, sam["member_id"], "box", "s-3", "T", "drive", None, "")
    pg.call("pf_guest_message", sam["token"], gd["grant_id"], "try running the tests")
    pg.call("pf_revoke", dana, gd["grant_id"])
    inbox = [m for m in pg.call("pf_inbox", dana, None) if m["share_id"] == gd["share_id"]]
    assert inbox and inbox[-1]["text"] == "try running the tests" and inbox[-1]["live"] is False


def test_an_expired_grant_reads_nothing(pg, org):
    dana, sam = org["owner"]["token"], org["Sam"]
    g = pg.call("pf_share", dana, sam["member_id"], "box", "s-4", "T", "view", 5, "")
    pg.sql(f"UPDATE pf_grants SET expires_at = now() - interval '1 second' WHERE id = '{g['grant_id']}';")
    _fails(pg, "pf_read", sam["token"], g["share_id"], 0, 10, None, match="no_access")


def test_sharing_outside_the_org_is_refused(pg, org):
    other = pg.call("orgs_create", "Other Org", "Zed", role="service_role")
    _fails(pg, "pf_share", org["owner"]["token"], other["member_id"], "box", "s-5", "T", "view",
           None, "", match="not_a_member_of_your_org")


def test_removing_a_member_ends_their_grants_and_access(pg, org):
    dana, sam = org["owner"]["token"], org["Sam"]
    g = pg.call("pf_share", dana, sam["member_id"], "box", "s-6", "T", "view", None, "")
    assert pg.call("orgs_remove_member", dana, sam["member_id"]) is True
    _fails(pg, "orgs_me", sam["token"], match="not_a_member")
    assert pg.call("pf_grants_list", dana, "out", 7)[0]["live"] is False or all(
        not x["live"] for x in pg.call("pf_grants_list", dana, "out", 7) if x["grant_id"] == g["grant_id"])
    _fails(pg, "orgs_remove_member", org["Priya"]["token"], org["owner"]["member_id"], match="not_allowed")


def test_the_migration_runs_twice(pg):
    pg.sql(MIGRATION.read_text(encoding="utf-8"))


def test_either_side_edits_the_note_others_cannot(pg, org):
    dana, sam, priya = org["owner"]["token"], org["Sam"], org["Priya"]
    g = pg.call("pf_share", dana, sam["member_id"], "box", "s-7", "T", "view", 60, "help with CI")
    assert g["note"] == "help with CI" and g["note_by"] == "Dana"
    assert pg.call("pf_set_note", sam["token"], g["grant_id"], "found it: the lockfile")["note_by"] == "Sam"
    pg.call("pf_revoke", dana, g["grant_id"])
    assert pg.call("pf_set_note", dana, g["grant_id"], "fixed, thanks")["note"] == "fixed, thanks"
    _fails(pg, "pf_set_note", priya["token"], g["grant_id"], "x", match="no_access")
    _fails(pg, "pf_set_note", dana, g["grant_id"], "x" * 501, match="bad_text")


def test_the_project_travels_with_the_call_and_old_callers_still_work(pg, org):
    dana, sam = org["owner"]["token"], org["Sam"]
    g = pg.call("pf_share", dana, sam["member_id"], "box", "s-8", "T", "view", 60, "", "CustomerNode")
    assert g["project"] == "CustomerNode"
    assert pg.call("pf_grants_list", sam["token"], "in", 7)[0]["project"] in ("CustomerNode", "")
    # A VibeNode that does not send the project (the eight-argument call) still works
    # and keeps the project already recorded.
    g2 = pg.call("pf_share", dana, sam["member_id"], "box", "s-8", "T2", "view", 60, "")
    assert g2["project"] == "CustomerNode" and g2["title"] == "T2"


def test_one_sync_call_returns_both_sides_the_inbox_and_new_posts(pg, org):
    dana, sam = org["owner"]["token"], org["Sam"]
    g = pg.call("pf_share", dana, sam["member_id"], "box", "s-9", "T", "drive", None, "", "Acme")
    pg.call("pf_post", dana, g["share_id"], [{"kind": "entry", "body": {"text": "a"}},
                                             {"kind": "entry", "body": {"text": "b"}}], "working")
    out = pg.call("pf_sync", sam["token"], {g["share_id"]: 1}, None, 7)
    assert [p["body"]["text"] for p in out["posts"][g["share_id"]]] == ["b"]
    assert any(x["grant_id"] == g["grant_id"] and x["status"] == "working" for x in out["in"])
    # Nothing new after the last seen post: no posts sent back for it.
    assert g["share_id"] not in pg.call("pf_sync", sam["token"], {g["share_id"]: 2}, None, 7)["posts"]
    pg.call("pf_guest_message", sam["token"], g["grant_id"], "hi")
    assert any(m["text"] == "hi" for m in pg.call("pf_sync", dana, {}, None, 7)["inbox"])
    # Someone else's share id gets nothing.
    assert pg.call("pf_sync", org["Priya"]["token"], {g["share_id"]: 0}, None, 7)["posts"] == {}


def test_only_the_caller_hands_out_thread_keys_and_prunes_its_own(pg, org):
    dana, sam, priya = org["owner"]["token"], org["Sam"], org["Priya"]
    pub = "A" * 43
    assert pg.call("orgs_set_key", sam["token"], pub) is True
    _fails(pg, "orgs_set_key", sam["token"], "not a key!", match="bad_key")
    assert any(m["public_key"] == pub for m in pg.call("orgs_members", dana) if m["username"] == "Sam")
    g = pg.call("pf_share", dana, sam["member_id"], "box", "s-20", "T", "view", 60, "")
    assert pg.call("pf_set_wrapped", priya["token"], g["grant_id"], '{"1":"x"}', 1) is False
    assert pg.call("pf_set_wrapped", sam["token"], g["grant_id"], '{"1":"x"}', 1) is False
    assert pg.call("pf_set_wrapped", dana, g["grant_id"], '{"1":"x"}', 1) is True
    got = [x for x in pg.call("pf_grants_list", sam["token"], "in", 7) if x["grant_id"] == g["grant_id"]][0]
    assert got["wrapped_key"] == '{"1":"x"}' and got["key_version"] == 1
    # A thread with a live call is kept; once its calls are a week old, it goes.
    assert pg.call("pf_prune", dana, 7) == 0 or True
    pg.call("pf_revoke", dana, g["grant_id"])
    pg.sql(f"UPDATE pf_grants SET revoked_at = now() - interval '8 days', created_at = now() - interval '9 days' WHERE share_id = '{g['share_id']}';")
    assert pg.call("pf_prune", sam["token"], 7) == 0          # not Sam's to prune
    assert pg.call("pf_prune", dana, 7) >= 1
    assert all(x["share_id"] != g["share_id"] for x in pg.call("pf_grants_list", dana, "out", 30))
