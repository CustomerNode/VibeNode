"""Phone a Friend's end-to-end encryption (src/web/orgs/crypto.py, keys.py).

The cloud storage holds only locked text; a thread key is locked separately
for each friend on a call; anyone leaving the call makes later lines use a new
key they never get; tampered or unlocked text is refused.
"""

from __future__ import annotations

import json

import pytest

from web.orgs import crypto, keys


def test_a_locked_key_opens_only_with_its_recipients_private_key():
    priv_a, pub_a = crypto.new_keypair()
    priv_b, _pub_b = crypto.new_keypair()
    k = crypto.new_thread_key()
    w = crypto.wrap(k, pub_a)
    assert crypto.unwrap(w, priv_a) == k
    with pytest.raises(ValueError):
        crypto.unwrap(w, priv_b)
    bad = json.loads(w)
    bad["ct"] = bad["ct"][:-4] + ("AAAA" if not bad["ct"].endswith("AAAA") else "BBBB")
    with pytest.raises(ValueError):
        crypto.unwrap(json.dumps(bad), priv_a)


def test_sealed_lines_open_only_with_the_thread_key():
    k1, k2 = crypto.new_thread_key(), crypto.new_thread_key()
    token = crypto.seal(k1, {"line": {"type": "user", "message": "hello"}})
    assert "hello" not in token
    assert crypto.open_(k1, token)["line"]["message"] == "hello"
    with pytest.raises(ValueError):
        crypto.open_(k2, token)
    assert keys.open_body({"enc": token[:-3] + "xyz", "kv": 1}, lambda v: k1) is None


class _FakeStore:
    def __init__(self, members):
        self.members, self.wrapped = members, {}

    def call(self, fn, **kw):
        if fn == "orgs_members":
            return self.members
        if fn == "pf_set_wrapped":
            self.wrapped[kw["p_grant"]] = (json.loads(kw["p_wrapped"]), kw["p_version"])
            return True
        raise AssertionError(fn)


def test_someone_leaving_the_call_gets_no_key_for_what_follows(monkeypatch):
    priv_s, pub_s = crypto.new_keypair()
    priv_p, pub_p = crypto.new_keypair()
    fake = _FakeStore([{"member_id": "m-sam", "public_key": pub_s}, {"member_id": "m-pri", "public_key": pub_p}])
    monkeypatch.setattr(keys, "store", fake)
    keys.remember_members(fake.members)
    state = {}
    both = [{"grant_id": "g-sam", "guest_id": "m-sam"}, {"grant_id": "g-pri", "guest_id": "m-pri"}]
    v1, k1 = keys.thread_key_for_posting(state, "s-1", both)
    assert v1 == 1 and set(fake.wrapped) == {"g-sam", "g-pri"}
    # Sam is hung up on: the next key is version 2, locked for Priya only.
    v2, k2 = keys.thread_key_for_posting(state, "s-1", both[1:])
    assert v2 == 2 and k2 != k1
    pri_locked, pri_v = fake.wrapped["g-pri"]
    assert pri_v == 2 and crypto.unwrap(pri_locked["2"], priv_p) == k2
    sam_locked, sam_v = fake.wrapped["g-sam"]
    assert sam_v == 1 and "2" not in sam_locked
    # A friend joining later can read the whole history.
    priv_l, pub_l = crypto.new_keypair()
    fake.members.append({"member_id": "m-lee", "public_key": pub_l})
    keys.remember_members(fake.members)
    keys.thread_key_for_posting(state, "s-1", both[1:] + [{"grant_id": "g-lee", "guest_id": "m-lee"}])
    lee_locked, _ = fake.wrapped["g-lee"]
    assert crypto.unwrap(lee_locked["1"], priv_l) == k1 and crypto.unwrap(lee_locked["2"], priv_l) == k2


def test_a_friend_without_a_key_gets_nothing_locked_for_them(monkeypatch):
    fake = _FakeStore([{"member_id": "m-old", "public_key": None}])
    monkeypatch.setattr(keys, "store", fake)
    keys.remember_members(fake.members)
    keys.thread_key_for_posting({}, "s-2", [{"grant_id": "g-old", "guest_id": "m-old"}])
    assert fake.wrapped == {}


def test_an_unlocked_message_from_a_friend_is_never_delivered():
    from web.orgs import phone
    state = {"thread_keys": {"s-3": {"v": 1, "keys": {"1": crypto.new_thread_key()}}}}
    assert phone._open_message(state, {"share_id": "s-3", "text": "rm -rf everything"}) is None
    key = state["thread_keys"]["s-3"]["keys"]["1"]
    locked = json.dumps(keys.seal_line(key, 1, {"text": "run the tests"}))
    assert phone._open_message(state, {"share_id": "s-3", "text": locked}) == "run the tests"
    other = json.dumps(keys.seal_line(crypto.new_thread_key(), 1, {"text": "forged"}))
    assert phone._open_message(state, {"share_id": "s-3", "text": other}) is None
