"""VibeNode Orgs and Phone a Friend routes (src/web/orgs/).

Every route is the owner's only (``@local_only``: this machine or the owner's
own phone): a friend never calls this machine, they read what was shared
through the org's cloud storage from their own VibeNode.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from flask import Blueprint, jsonify, request

from ..config import get_kanban_config
from ..mobile_auth import local_only
from ..orgs import invite, phone, store
from ..orgs.store import OrgsError

bp = Blueprint("orgs_api", __name__)

MIGRATION = Path(__file__).resolve().parents[1] / "db" / "migrations" / "007_vibenode_orgs.sql"
_ID_RE = re.compile(r"^[0-9a-f-]{36}$")
_KEY_RE = re.compile(r"^(sb_publishable_[A-Za-z0-9_-]{8,}|eyJ[A-Za-z0-9_.-]{20,})$")

_STATUS = {"not_a_member": 403, "not_allowed": 403, "no_access": 403, "not_your_session": 403,
           "not_a_member_of_your_org": 403, "permission_denied": 403, "invite_invalid": 410,
           "username_taken": 409, "not_set_up": 409, "schema_missing": 409, "network": 502}


def _fail(e: OrgsError):
    return jsonify(ok=False, error=e.code, message=str(e)), _STATUS.get(e.code, 400)


def _body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _cloud() -> dict:
    """The creator's cloud storage (the Workflow board's Supabase), if on."""
    cfg = get_kanban_config()
    url = str(cfg.get("supabase_url") or "").strip().rstrip("/")
    on = cfg.get("kanban_backend") == "supabase" and store.valid_url(url) and bool(cfg.get("supabase_secret_key"))
    return {"on": on, "url": url, "secret": cfg.get("supabase_secret_key") or "",
            "publishable": str(cfg.get("supabase_publishable_key") or "").strip()}


@bp.route("/api/orgs/me")
@local_only
def api_me():
    c = _cloud()
    return jsonify(ok=True, cloud_storage=c["on"], publishable_key_set=bool(c["publishable"]),
                   **store.public_view())


@bp.route("/api/orgs/setup-sql")
@local_only
def api_setup_sql():
    return MIGRATION.read_text(encoding="utf-8"), 200, {"Content-Type": "text/plain; charset=utf-8"}


def _apply_schema(url: str, access_token: str) -> None:
    import httpx
    m = re.match(r"^https://([a-z0-9-]+)\.supabase\.(co|in)$", url)
    if not m:
        raise OrgsError("bad_url")
    r = httpx.post(f"https://api.supabase.com/v1/projects/{m.group(1)}/database/query",
                   json={"query": MIGRATION.read_text(encoding="utf-8")},
                   headers={"Authorization": f"Bearer {access_token}"}, timeout=60.0)
    if r.status_code == 401:
        raise OrgsError("bad_access_token", "That access token was refused.")
    if r.status_code >= 400:
        raise OrgsError("schema_failed", r.text[:300])


@bp.route("/api/orgs/setup", methods=["POST"])
@local_only
def api_setup():
    """Start an org in this VibeNode's cloud storage, with you as its owner."""
    d = _body()
    c = _cloud()
    if not c["on"]:
        return jsonify(ok=False, error="cloud_storage_off",
                       message="Turn on cloud storage in System settings first."), 409
    if store.load():
        return jsonify(ok=False, error="already_member", message="This VibeNode is already in an org."), 409
    key = str(d.get("publishable_key") or c["publishable"]).strip()
    if not _KEY_RE.match(key):
        return jsonify(ok=False, error="publishable_key_missing",
                       message="Add the cloud storage's publishable key (Supabase → Project Settings → API Keys)."), 400
    try:
        if d.get("access_token"):
            _apply_schema(c["url"], str(d["access_token"]).strip())
        params = {"p_name": str(d.get("org_name") or "").strip()[:80],
                  "p_username": str(d.get("username") or "").strip()[:32]}
        # The API can take a few seconds to see functions the setup SQL just
        # added (its schema reload is asynchronous), so a fresh setup retries.
        for attempt in range(6):
            try:
                made = store.rpc("orgs_create", params, url=c["url"], key=c["secret"])
                break
            except OrgsError as e:
                if e.code != "schema_missing" or not d.get("access_token") or attempt == 5:
                    raise
                time.sleep(2)
    except OrgsError as e:
        return _fail(e)
    store.save({"url": c["url"], "key": key, **{k: made[k] for k in
                ("token", "org_id", "org_name", "member_id", "username", "role")}})
    phone.refresh_org_names(force=True)
    return jsonify(ok=True, **store.public_view())


@bp.route("/api/orgs/join", methods=["POST"])
@local_only
def api_join():
    """Join an org from an invite prompt. The invite works once."""
    d = _body()
    if store.load():
        return jsonify(ok=False, error="already_member", message="This VibeNode is already in an org."), 409
    try:
        inv = invite.parse(d.get("invite") or d.get("prompt") or "")
    except (ValueError, TypeError) as e:
        return jsonify(ok=False, error="bad_invite", message=str(e)), 400
    username = str(d.get("username") or "").strip()[:32]
    try:
        made = store.rpc("orgs_redeem_invite", {"p_code": inv["code"], "p_username": username},
                         url=inv["url"], key=inv["key"])
    except OrgsError as e:
        return _fail(e)
    store.save({"url": inv["url"], "key": inv["key"], **{k: made[k] for k in
                ("token", "org_id", "org_name", "member_id", "username", "role")}})
    phone.refresh_org_names(force=True)
    return jsonify(ok=True, message=f"Joined {made['org_name']} as {made['username']}.",
                   **store.public_view())


@bp.route("/api/orgs/members")
@local_only
def api_members():
    try:
        members = store.call("orgs_members") or []
    except OrgsError as e:
        return _fail(e)
    # Every member's name joins the publish scan's refused names.
    from ..git_scanner import remember_org_names
    remember_org_names([m.get("username") for m in members if m.get("username")])
    return jsonify(ok=True, members=members)


@bp.route("/api/orgs/invite", methods=["POST"])
@local_only
def api_invite():
    try:
        days = int(_body().get("days") or 3)
    except (TypeError, ValueError):
        days = 3
    m = store.load()
    try:
        made = store.call("orgs_create_invite", p_days=max(1, min(30, days)))
    except OrgsError as e:
        return _fail(e)
    return jsonify(ok=True, expires_at=made["expires_at"], prompt=invite.build_prompt(
        url=m["url"], key=m["key"], code=made["code"], org_name=m.get("org_name", ""),
        inviter=m.get("username", ""), expires_at=str(made["expires_at"])))


@bp.route("/api/orgs/invites/revoke", methods=["POST"])
@local_only
def api_revoke_invites():
    try:
        return jsonify(ok=True, revoked=store.call("orgs_revoke_invites"))
    except OrgsError as e:
        return _fail(e)


@bp.route("/api/orgs/members/<member_id>/remove", methods=["POST"])
@local_only
def api_remove_member(member_id):
    if not _ID_RE.match(member_id):
        return jsonify(ok=False, error="bad_id"), 400
    try:
        return jsonify(ok=True, removed=store.call("orgs_remove_member", p_member=member_id))
    except OrgsError as e:
        return _fail(e)


@bp.route("/api/orgs/leave", methods=["POST"])
@local_only
def api_leave():
    m = store.load()
    if m:
        try:
            store.call("orgs_remove_member", p_member=m["member_id"])
        except OrgsError as e:
            if e.code not in ("not_a_member",):
                return _fail(e)
    store.clear()
    from ..orgs import mirror
    mirror.remove_all()
    return jsonify(ok=True)


# ── Phone a Friend ──────────────────────────────────────────────────────────

@bp.route("/api/phone/call", methods=["POST"])
@local_only
def api_call():
    d = _body()
    if not _ID_RE.match(str(d.get("guest_id") or "")):
        return jsonify(ok=False, error="bad_id"), 400
    try:
        g = phone.call_friend(str(d.get("session_id") or ""), d["guest_id"],
                              str(d.get("level") or "view"), str(d.get("duration") or "revoked"),
                              title=str(d.get("title") or ""), note=str(d.get("note") or ""))
    except OrgsError as e:
        return _fail(e)
    return jsonify(ok=True, grant=g)


@bp.route("/api/phone/revoke/<grant_id>", methods=["POST"])
@local_only
def api_revoke(grant_id):
    if not _ID_RE.match(grant_id):
        return jsonify(ok=False, error="bad_id"), 400
    try:
        return jsonify(ok=True, revoked=phone.revoke(grant_id))
    except OrgsError as e:
        return _fail(e)


@bp.route("/api/phone/note/<grant_id>", methods=["POST"])
@local_only
def api_note(grant_id):
    if not _ID_RE.match(grant_id):
        return jsonify(ok=False, error="bad_id"), 400
    note = str(_body().get("note") or "").strip()
    if len(note) > 500:
        return jsonify(ok=False, error="bad_text", message="A note is at most 500 characters."), 400
    try:
        g = phone.set_note(grant_id, note)
    except OrgsError as e:
        return _fail(e)
    try:
        from .. import socketio
        socketio.emit("pf_note", g)
    except Exception:
        pass
    return jsonify(ok=True, grant=g)


@bp.route("/api/phone/mirror/<session_id>")
@local_only
def api_mirror(session_id):
    """Which project a phoned session lives in on this computer."""
    from ..orgs import mirror
    m = mirror.grant_for(session_id)
    if not m or not mirror.is_mirror(session_id):
        return jsonify(ok=False, error="not_found"), 404
    return jsonify(ok=True, project=mirror.project_encoded(m.get("owner") or "friend"))


@bp.route("/api/phone/out")
@local_only
def api_out():
    try:
        return jsonify(ok=True, grants=phone.outgoing(days=int(request.args.get("days", 0) or 0)))
    except (OrgsError) as e:
        return _fail(e)


@bp.route("/api/phone/in")
@local_only
def api_in():
    # Only the current screens ask with v=2. A tab still running the first
    # version drew its own "phoned you" rows and popup from this list; it gets
    # an empty one, so nothing but the real phoned-you project ever shows.
    if request.args.get("v") != "2":
        return jsonify(ok=True, grants=[])
    try:
        return jsonify(ok=True, grants=phone.incoming(days=int(request.args.get("days", 7) or 7)))
    except OrgsError as e:
        return _fail(e)


@bp.route("/api/phone/read/<share_id>")
@local_only
def api_read(share_id):
    if not _ID_RE.match(share_id):
        return jsonify(ok=False, error="bad_id"), 400
    try:
        after = int(request.args.get("after", 0) or 0)
        limit = int(request.args.get("limit", 100) or 100)
        before = request.args.get("before")
        before = int(before) if before else None
    except ValueError:
        return jsonify(ok=False, error="bad_args"), 400
    try:
        return jsonify(ok=True, **phone.read(share_id, after, limit, before))
    except OrgsError as e:
        return _fail(e)


@bp.route("/api/phone/send/<grant_id>", methods=["POST"])
@local_only
def api_send(grant_id):
    if not _ID_RE.match(grant_id):
        return jsonify(ok=False, error="bad_id"), 400
    text = str(_body().get("text") or "").strip()
    if not text:
        return jsonify(ok=False, error="bad_text"), 400
    try:
        return jsonify(ok=True, seq=phone.send_to_friend_session(grant_id, text))
    except OrgsError as e:
        return _fail(e)


@bp.route("/api/phone/suggestions")
@local_only
def api_suggestions():
    return jsonify(ok=True, suggestions=phone.suggestions())


@bp.route("/api/phone/suggestions/<key>/<action>", methods=["POST"])
@local_only
def api_suggestion_action(key, action):
    if action not in ("send", "dismiss"):
        return jsonify(ok=False, error="bad_action"), 400
    hit = phone.dismiss_suggestion(key)
    if hit is None:
        return jsonify(ok=False, error="not_found"), 404
    if action == "send":
        from flask import current_app
        sm = current_app.session_manager
        text = f"[{hit['from']} via Phone a Friend, sent by you]\n{hit['text']}"
        if sm.has_session(hit["session_id"]):
            sm.queue_message(hit["session_id"], text)
        else:
            sm.start_session(hit["session_id"], prompt=text, resume=True)
    return jsonify(ok=True)
