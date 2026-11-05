"""Who holds which key for Phone a Friend's end-to-end encryption (crypto.py).

* ``my_private_key`` makes this member's key pair on first use, keeps the
  private half in the membership file and registers the public half.
* ``thread_key_for_posting`` is the caller's side: the current thread key of a
  shared session, rotated when anyone left the call, and locked for every
  friend still on it (every version they may read, so a new friend can read
  the history and a removed one cannot read what follows).
* ``thread_key_for_reading`` is the friend's side: the thread key of one
  version, opened from what the caller locked for them.

Thread keys live in the Phone a Friend state file (mode 600), never in the
cloud storage unlocked.
"""

from __future__ import annotations

import json
import logging
import threading

from . import crypto, store
from .store import OrgsError

log = logging.getLogger("web.orgs.keys")
_lock = threading.Lock()
_members: dict = {"by_id": {}}


def my_private_key() -> str:
    """This member's private key; the pair is made and registered on first use."""
    with _lock:
        m = store.load()
        if not m:
            raise OrgsError("not_set_up")
        if not m.get("private_key"):
            priv, pub = crypto.new_keypair()
            m.update(private_key=priv, public_key=pub, key_registered=False)
            store.save(m)
        if not m.get("key_registered"):
            store.call("orgs_set_key", p_public=m["public_key"])
            m["key_registered"] = True
            store.save(m)
        return m["private_key"]


def remember_members(members: list) -> None:
    _members["by_id"] = {x["member_id"]: x for x in members or [] if x.get("member_id")}


def public_key_of(member_id: str, refresh: bool = True) -> str | None:
    m = _members["by_id"].get(member_id)
    if (not m or not m.get("public_key")) and refresh:
        try:
            remember_members(store.call("orgs_members") or [])
        except OrgsError:
            return None
        m = _members["by_id"].get(member_id)
    return (m or {}).get("public_key") or None


def thread_key_for_posting(state: dict, share_id: str, live_grants: list) -> tuple[int, str]:
    """(version, key) to lock new lines of ``share_id`` with; ``state`` is the
    Phone a Friend state (saved by the caller)."""
    tks = state.setdefault("thread_keys", {})
    tk = tks.get(share_id)
    live_ids = {g["grant_id"] for g in live_grants}
    if tk is None:
        tk = {"v": 1, "keys": {"1": crypto.new_thread_key()}, "wrapped": {}, "guests": []}
    elif any(gid not in live_ids for gid in tk.get("guests", [])):
        v = tk["v"] + 1                                  # someone left: a new key from here on
        tk["keys"][str(v)] = crypto.new_thread_key()
        tk["v"] = v
    v = tk["v"]
    for g in live_grants:
        gid = g["grant_id"]
        if tk["wrapped"].get(gid) == v:
            continue
        pub = public_key_of(g["guest_id"])
        if not pub:
            continue                                     # an older VibeNode: it cannot read until it updates
        locked = {k: crypto.wrap(key, pub) for k, key in tk["keys"].items()}
        try:
            store.call("pf_set_wrapped", p_grant=gid, p_wrapped=json.dumps(locked), p_version=v)
            tk["wrapped"][gid] = v
        except OrgsError:
            log.warning("could not hand the thread key to one guest; retrying next tick")
    tk["guests"] = sorted(live_ids)
    tks[share_id] = tk
    return v, tk["keys"][str(v)]


def owner_key(state: dict, share_id: str, version) -> str | None:
    return ((state.get("thread_keys") or {}).get(share_id) or {}).get("keys", {}).get(str(version))


def thread_key_for_reading(state: dict, grant: dict, version) -> str | None:
    """The thread key of ``version`` for a call made to this member."""
    cache = state.setdefault("guest_keys", {}).setdefault(grant["share_id"], {})
    if str(version) in cache:
        return cache[str(version)]
    try:
        locked = json.loads(grant.get("wrapped_key") or "{}")
        key = crypto.unwrap(locked[str(version)], my_private_key())
    except (ValueError, KeyError, TypeError, OrgsError):
        return None
    cache[str(version)] = key
    return key


def seal_line(key: str, version: int, obj) -> dict:
    return {"enc": crypto.seal(key, obj), "kv": version}


def open_body(body: dict, key_for) -> dict | None:
    """A post or message body, opened; plain bodies (an older caller) pass
    through. ``key_for(version)`` returns the key or None."""
    if not isinstance(body, dict) or "enc" not in body:
        return body
    key = key_for(body.get("kv"))
    if not key:
        return None
    try:
        return crypto.open_(key, body["enc"])
    except ValueError:
        return None
