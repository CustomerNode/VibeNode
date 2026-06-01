"""Tests for lossless large-session media eviction + change-gated resume pass.

Covers Workstreams 1-3 of docs/plans/large-session-perf.md:

- WS1: OLD inline image tool_results are externalized (content-addressed by
  sha256 to ~/.claude/session-env/<sid>/vibenode-media/) and replaced in the
  replayed transcript by a text placeholder; recent-K images stay inline.
- WS2: the byte-identical ``toolUseResult.file.base64`` duplicate is externalized
  to the same file and replaced by a reconstructable marker.
- WS3: ``prepare_for_resume`` combines repair + eviction in ONE conditional
  rewrite, is change-gated (no write when nothing changed), and ``repair_*``
  parity is preserved.

All fixtures are synthetic: tiny in-test PNGs, never real screenshots
(public-repo safety).  Externalized media is redirected into ``tmp_path`` by
monkeypatching ``Path.home`` — the real ``~/.claude`` is never touched.
"""

import base64
import hashlib
import json
import struct
import zlib
from pathlib import Path

import pytest

from daemon.backends.claude_store import (
    ClaudeJsonlStore,
    _media_ext_for,
    _media_dir_for,
)


# ---------------------------------------------------------------------------
# Helpers: build tiny, unique, valid PNGs in-test (no real screenshots)
# ---------------------------------------------------------------------------

def _tiny_png(seed: int) -> bytes:
    """A minimal but valid 1x1 PNG whose bytes vary with ``seed``.

    Returns DISTINCT bytes per seed so each image has a distinct sha256.
    """
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(ctype: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + ctype + data
            + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    # one RGB pixel, color varies with seed
    raw = bytes([0]) + bytes([seed & 0xFF, (seed >> 8) & 0xFF, (seed >> 16) & 0xFF])
    idat = zlib.compress(raw)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _image_tool_result_user(uuid, parent, tool_id, png_b64,
                            media_type="image/png", with_tooluseresult=True,
                            extra_blocks=None):
    """A user entry whose message carries an image tool_result (the real shape)."""
    content = []
    if extra_blocks:
        content.extend(extra_blocks)
    content.append({
        "type": "tool_result",
        "tool_use_id": tool_id,
        "content": [
            {"type": "image",
             "source": {"type": "base64", "data": png_b64,
                        "media_type": media_type}},
        ],
    })
    entry = {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "message": {"content": content},
    }
    if with_tooluseresult:
        entry["toolUseResult"] = {
            "type": "image",
            "file": {"base64": png_b64, "type": media_type,
                     "originalSize": 1234,
                     "dimensions": {"width": 1, "height": 1}},
        }
    return entry


def _assistant_tool_use(uuid, parent, tool_id):
    return {
        "type": "assistant", "uuid": uuid, "parentUuid": parent,
        "message": {"id": "M", "stop_reason": "end_turn", "content": [
            {"type": "tool_use", "id": tool_id, "name": "Read", "input": {}}]},
    }


def _human_user(uuid, parent, text):
    return {"type": "user", "uuid": uuid, "parentUuid": parent,
            "message": {"content": text}}


def _write_jsonl(path, entries):
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _read_objs(path):
    return [json.loads(l) for l in
            Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


# ---------------------------------------------------------------------------
# Fixture: a store whose find_session_path → tmp file and whose media dir →
# tmp (Path.home redirected so the real ~/.claude is never touched).
# ---------------------------------------------------------------------------

@pytest.fixture
def store_env(tmp_path, monkeypatch):
    jsonl_path = tmp_path / "session.jsonl"
    home = tmp_path / "home"
    home.mkdir()
    # Redirect Path.home so _media_dir_for writes under tmp, never real ~/.claude
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    store = ClaudeJsonlStore()
    monkeypatch.setattr(store, "find_session_path", lambda sid, cwd="": jsonl_path)

    def media_dir(sid):
        return home / ".claude" / "session-env" / sid / "vibenode-media"

    return {
        "store": store,
        "jsonl_path": jsonl_path,
        "home": home,
        "media_dir": media_dir,
    }


# ===========================================================================
# Helper-function unit tests
# ===========================================================================

class TestMediaHelpers:
    def test_media_ext_known(self):
        assert _media_ext_for("image/jpeg") == "jpg"
        assert _media_ext_for("image/png") == "png"
        assert _media_ext_for("image/gif") == "gif"
        assert _media_ext_for("image/webp") == "webp"

    def test_media_ext_unknown_falls_back(self):
        assert _media_ext_for("") == "bin"
        assert _media_ext_for("application/octet-stream") == "bin"
        assert _media_ext_for("image/x-weird") == "bin"  # hyphen → not alnum

    def test_media_ext_strips_params(self):
        assert _media_ext_for("image/png; charset=binary") == "png"
        assert _media_ext_for("image/svg+xml") == "svg"

    def test_media_dir_under_home(self, tmp_path, monkeypatch):
        home = tmp_path / "h"
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        d = _media_dir_for("abc-123")
        assert d == home / ".claude" / "session-env" / "abc-123" / "vibenode-media"
        # Path is derived from Path.home() — never a hardcoded user path.
        assert str(tmp_path) in str(d)


# ===========================================================================
# WS1 — Stale-media eviction round-trip
# ===========================================================================

class TestEvictionRoundTrip:
    def _build(self):
        """Old image (turn 1) + recent image (turn 2, the last user turn).

        With keep_recent_turns=1, the turn-2 image stays inline; the turn-1
        image is evicted.
        """
        old_png = _tiny_png(1)
        new_png = _tiny_png(2)
        entries = [
            _human_user("u1", None, "first question"),
            _assistant_tool_use("a1", "u1", "t1"),
            _image_tool_result_user("r1", "a1", "t1", _b64(old_png)),
            _human_user("u2", "r1", "second question"),
            _assistant_tool_use("a2", "u2", "t2"),
            _image_tool_result_user("r2", "a2", "t2", _b64(new_png)),
        ]
        return entries, old_png, new_png

    def test_old_image_evicted_recent_preserved(self, store_env):
        entries, old_png, new_png = self._build()
        _write_jsonl(store_env["jsonl_path"], entries)
        store = store_env["store"]

        changed = store.prepare_for_resume(
            "sid", evict_media=True, keep_recent_turns=1,
            dedup_recent_tooluseresult=True)
        assert changed is True

        objs = _read_objs(store_env["jsonl_path"])
        by = {o["uuid"]: o for o in objs}

        # (a) Valid JSONL — _read_objs already parsed every line without error.
        assert len(objs) == len(entries)

        # (d) recent-K image preserved inline (still a base64 image sub-block)
        r2_sub = by["r2"]["message"]["content"][-1]["content"]
        assert any(b.get("type") == "image"
                   and b.get("source", {}).get("data") == _b64(new_png)
                   for b in r2_sub)

        # (e) old image replaced by text placeholder
        r1_sub = by["r1"]["message"]["content"][-1]["content"]
        assert all(b.get("type") != "image" for b in r1_sub)
        placeholder = [b for b in r1_sub if b.get("type") == "text"]
        assert placeholder, "old image must become a text placeholder"
        ptxt = placeholder[0]["text"]
        assert ptxt.startswith("[image evicted from replayed context")
        assert "vibenode-media/" in ptxt

        # (b) tool_use_id pairing intact for the evicted tool_result
        assert by["r1"]["message"]["content"][-1]["tool_use_id"] == "t1"
        assert by["r2"]["message"]["content"][-1]["tool_use_id"] == "t2"

        # (e/f) on-disk file exists, sha matches, bytes are lossless
        sha = _sha(old_png)
        media_file = store_env["media_dir"]("sid") / f"{sha}.png"
        assert media_file.exists()
        assert media_file.read_bytes() == old_png
        assert sha in ptxt  # placeholder references the sha-named relpath

    def test_parent_uuid_chain_intact(self, store_env):
        entries, _o, _n = self._build()
        _write_jsonl(store_env["jsonl_path"], entries)
        store_env["store"].prepare_for_resume(
            "sid", evict_media=True, keep_recent_turns=1)
        objs = _read_objs(store_env["jsonl_path"])
        # Eviction adds/removes NO lines → chain identical to input.
        chain = [(o["uuid"], o.get("parentUuid")) for o in objs]
        assert chain == [
            ("u1", None), ("a1", "u1"), ("r1", "a1"),
            ("u2", "r1"), ("a2", "u2"), ("r2", "a2"),
        ]

    def test_idempotent_second_run_is_noop(self, store_env):
        entries, _o, _n = self._build()
        _write_jsonl(store_env["jsonl_path"], entries)
        store = store_env["store"]
        assert store.prepare_for_resume(
            "sid", evict_media=True, keep_recent_turns=1) is True
        after_first = store_env["jsonl_path"].read_text(encoding="utf-8")
        # (g) second run writes nothing
        assert store.prepare_for_resume(
            "sid", evict_media=True, keep_recent_turns=1) is False
        assert store_env["jsonl_path"].read_text(encoding="utf-8") == after_first

    def test_recoverable_bytes_match_sha(self, store_env):
        entries, old_png, _n = self._build()
        _write_jsonl(store_env["jsonl_path"], entries)
        store_env["store"].prepare_for_resume(
            "sid", evict_media=True, keep_recent_turns=1)
        sha = _sha(old_png)
        media_file = store_env["media_dir"]("sid") / f"{sha}.png"
        recovered = media_file.read_bytes()
        assert hashlib.sha256(recovered).hexdigest() == sha
        assert recovered == old_png

    def test_mixed_text_and_image_tool_result(self, store_env):
        """A tool_result with BOTH a text and an image sub-block: text kept
        verbatim, image swapped, ordering preserved."""
        png = _tiny_png(7)
        entries = [
            _human_user("u1", None, "q"),
            _assistant_tool_use("a1", "u1", "t1"),
            {"type": "user", "uuid": "r1", "parentUuid": "a1",
             "message": {"content": [{
                 "type": "tool_result", "tool_use_id": "t1", "content": [
                     {"type": "text", "text": "PRE"},
                     {"type": "image", "source": {
                         "type": "base64", "data": _b64(png),
                         "media_type": "image/png"}},
                     {"type": "text", "text": "POST"},
                 ]}]}},
            _human_user("u2", "r1", "next"),
            _human_user("u3", "u2", "next2"),
        ]
        _write_jsonl(store_env["jsonl_path"], entries)
        store_env["store"].prepare_for_resume(
            "sid", evict_media=True, keep_recent_turns=1)
        objs = _read_objs(store_env["jsonl_path"])
        sub = objs[2]["message"]["content"][0]["content"]
        kinds = [b["type"] for b in sub]
        assert kinds == ["text", "text", "text"]  # image → text, order kept
        assert sub[0]["text"] == "PRE"
        assert sub[2]["text"] == "POST"
        assert sub[1]["text"].startswith("[image evicted")

    def test_multi_image_tool_result(self, store_env):
        """A tool_result with TWO images: each externalized under its own sha."""
        p1, p2 = _tiny_png(11), _tiny_png(12)
        entries = [
            _human_user("u1", None, "q"),
            _assistant_tool_use("a1", "u1", "t1"),
            {"type": "user", "uuid": "r1", "parentUuid": "a1",
             "message": {"content": [{
                 "type": "tool_result", "tool_use_id": "t1", "content": [
                     {"type": "image", "source": {
                         "type": "base64", "data": _b64(p1),
                         "media_type": "image/png"}},
                     {"type": "image", "source": {
                         "type": "base64", "data": _b64(p2),
                         "media_type": "image/png"}},
                 ]}]}},
            _human_user("u2", "r1", "next"),
            _human_user("u3", "u2", "next2"),
        ]
        _write_jsonl(store_env["jsonl_path"], entries)
        store_env["store"].prepare_for_resume(
            "sid", evict_media=True, keep_recent_turns=1)
        sub = _read_objs(store_env["jsonl_path"])[2]["message"]["content"][0]["content"]
        assert [b["type"] for b in sub] == ["text", "text"]
        for png in (p1, p2):
            mf = store_env["media_dir"]("sid") / f"{_sha(png)}.png"
            assert mf.exists() and mf.read_bytes() == png

    def test_keep_recent_turns_window(self, store_env):
        """With K=2, the two most recent image turns stay inline; older evict."""
        pngs = [_tiny_png(100 + i) for i in range(3)]
        entries = []
        prev = None
        for i, png in enumerate(pngs):
            u = f"u{i}"; a = f"a{i}"; r = f"r{i}"; t = f"t{i}"
            entries.append(_human_user(u, prev, f"q{i}"))
            entries.append(_assistant_tool_use(a, u, t))
            entries.append(_image_tool_result_user(r, a, t, _b64(png)))
            prev = r
        _write_jsonl(store_env["jsonl_path"], entries)
        store_env["store"].prepare_for_resume(
            "sid", evict_media=True, keep_recent_turns=2)
        objs = _read_objs(store_env["jsonl_path"])
        by = {o["uuid"]: o for o in objs}

        def has_inline_image(uuid):
            sub = by[uuid]["message"]["content"][-1]["content"]
            return any(b.get("type") == "image" for b in sub)

        # turn 0 evicted; turns 1 & 2 (the last 2 user turns) preserved inline
        assert has_inline_image("r0") is False
        assert has_inline_image("r1") is True
        assert has_inline_image("r2") is True


# ===========================================================================
# WS2 — toolUseResult de-dup
# ===========================================================================

class TestToolUseResultDedup:
    def test_old_entry_tooluseresult_externalized(self, store_env):
        png = _tiny_png(21)
        entries = [
            _human_user("u1", None, "q"),
            _assistant_tool_use("a1", "u1", "t1"),
            _image_tool_result_user("r1", "a1", "t1", _b64(png)),
            _human_user("u2", "r1", "next"),
            _human_user("u3", "u2", "next2"),
        ]
        _write_jsonl(store_env["jsonl_path"], entries)
        store_env["store"].prepare_for_resume(
            "sid", evict_media=True, keep_recent_turns=1,
            dedup_recent_tooluseresult=True)
        r1 = _read_objs(store_env["jsonl_path"])[2]
        f = r1["toolUseResult"]["file"]
        assert "base64" not in f
        assert f["externalized"] is True
        assert f["sha256"] == _sha(png)
        assert f["relpath"] == f"vibenode-media/{_sha(png)}.png"
        assert f["media_type"] == "image/png"
        # reconstructable from disk
        mf = store_env["media_dir"]("sid") / f"{_sha(png)}.png"
        assert mf.read_bytes() == png

    def test_recent_tooluseresult_deduped_but_inline_source_kept(self, store_env):
        """For a recent-K image we KEEP the inline source.data (model needs it)
        but still de-dup the redundant toolUseResult duplicate."""
        png = _tiny_png(22)
        entries = [
            _human_user("u1", None, "q"),
            _assistant_tool_use("a1", "u1", "t1"),
            _image_tool_result_user("r1", "a1", "t1", _b64(png)),
        ]
        _write_jsonl(store_env["jsonl_path"], entries)
        store_env["store"].prepare_for_resume(
            "sid", evict_media=True, keep_recent_turns=4,
            dedup_recent_tooluseresult=True)
        r1 = _read_objs(store_env["jsonl_path"])[2]
        # inline image still present (recent — model needs it)
        sub = r1["message"]["content"][-1]["content"]
        assert any(b.get("type") == "image"
                   and b["source"]["data"] == _b64(png) for b in sub)
        # but the toolUseResult duplicate is externalized
        assert "base64" not in r1["toolUseResult"]["file"]
        assert r1["toolUseResult"]["file"]["externalized"] is True

    def test_dedup_recent_off_leaves_recent_tooluseresult(self, store_env):
        """With dedup_recent_tooluseresult=False and the image inside recent-K,
        the toolUseResult duplicate is left intact."""
        png = _tiny_png(23)
        entries = [
            _human_user("u1", None, "q"),
            _assistant_tool_use("a1", "u1", "t1"),
            _image_tool_result_user("r1", "a1", "t1", _b64(png)),
        ]
        _write_jsonl(store_env["jsonl_path"], entries)
        changed = store_env["store"].prepare_for_resume(
            "sid", evict_media=True, keep_recent_turns=4,
            dedup_recent_tooluseresult=False)
        # nothing old, dedup_recent off → no change at all
        assert changed is False
        r1 = _read_objs(store_env["jsonl_path"])[2]
        assert r1["toolUseResult"]["file"]["base64"] == _b64(png)


# ===========================================================================
# WS3 — change-gating + repair parity
# ===========================================================================

class TestChangeGating:
    def test_no_change_no_write(self, store_env):
        """A clean transcript with no media and nothing to repair: no write,
        returns False, mtime unchanged."""
        entries = [
            _human_user("u1", None, "hi"),
            {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
             "message": {"id": "M", "stop_reason": "end_turn",
                         "content": [{"type": "text", "text": "done"}]}},
        ]
        _write_jsonl(store_env["jsonl_path"], entries)
        mtime_before = store_env["jsonl_path"].stat().st_mtime_ns
        import time
        time.sleep(0.01)
        assert store_env["store"].prepare_for_resume(
            "sid", evict_media=True, keep_recent_turns=4) is False
        assert store_env["jsonl_path"].stat().st_mtime_ns == mtime_before

    def test_eviction_disabled_is_repair_only(self, store_env):
        """evict_media=False must not touch images; only repairs run."""
        png = _tiny_png(31)
        entries = [
            _human_user("u1", None, "q"),
            _assistant_tool_use("a1", "u1", "t1"),
            _image_tool_result_user("r1", "a1", "t1", _b64(png)),
            _human_user("u2", "r1", "next"),
            _human_user("u3", "u2", "next2"),
        ]
        _write_jsonl(store_env["jsonl_path"], entries)
        # No repair needed + eviction off → no change.
        assert store_env["store"].prepare_for_resume(
            "sid", evict_media=False, keep_recent_turns=1) is False
        r1 = _read_objs(store_env["jsonl_path"])[2]
        sub = r1["message"]["content"][-1]["content"]
        assert any(b.get("type") == "image" for b in sub)  # untouched
        assert "base64" in r1["toolUseResult"]["file"]  # untouched

    def test_repair_runs_even_when_no_media(self, store_env):
        """An interrupted trailing turn is repaired even with eviction on and
        no evictable media present."""
        entries = [
            _human_user("u1", None, "q"),
            {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
             "message": {"stop_reason": None, "content": [
                 {"type": "text", "text": "partial"}]}},
        ]
        _write_jsonl(store_env["jsonl_path"], entries)
        assert store_env["store"].prepare_for_resume(
            "sid", evict_media=True, keep_recent_turns=4) is True
        a1 = _read_objs(store_env["jsonl_path"])[1]
        assert a1["message"]["stop_reason"] == "end_turn"
        assert any("interrupted" in b.get("text", "").lower()
                   for b in a1["message"]["content"])


class TestRepairParity:
    """The combined pass's repair behavior must match repair_incomplete_turn
    on representative fixtures (Pass 1 + Pass 2)."""

    def _fixtures(self):
        # (1) interrupted trailing turn
        f1 = [
            _human_user("u1", None, "q"),
            {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
             "message": {"stop_reason": None, "content": [
                 {"type": "thinking", "thinking": "x", "signature": "s"},
                 {"type": "text", "text": "partial"},
                 {"type": "tool_use", "id": "t1", "name": "Bash",
                  "input": {}}]}},
        ]
        # (2) empty-thinking spanning NON-tail lines (the back-half case)
        f2 = [_human_user("u0", None, "go")]
        prev = "u0"
        for i in range(3):
            tk = f"k{i}"
            f2.append({"type": "assistant", "uuid": tk, "parentUuid": prev,
                       "message": {"id": "M", "stop_reason": "end_turn",
                                   "content": [{"type": "thinking",
                                                "thinking": "",
                                                "signature": f"S{i}"}]}})
            tu = f"x{i}"
            f2.append({"type": "assistant", "uuid": tu, "parentUuid": tk,
                       "message": {"id": "M", "stop_reason": "end_turn",
                                   "content": [{"type": "text",
                                                "text": f"real{i}"}]}})
            prev = tu
        # trailing real content so the strip targets are NOT the tail line
        f2.append({"type": "assistant", "uuid": "ans", "parentUuid": prev,
                   "message": {"id": "M", "stop_reason": "end_turn",
                               "content": [{"type": "text",
                                            "text": "final"}]}})
        # (3) mixed empty-thinking + real content in one entry
        f3 = [
            _human_user("u1", None, "hi"),
            {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
             "message": {"id": "M", "stop_reason": "end_turn", "content": [
                 {"type": "thinking", "thinking": "", "signature": "S"},
                 {"type": "text", "text": "kept"}]}},
        ]
        return {"interrupted": f1, "backhalf_thinking": f2, "mixed": f3}

    @pytest.mark.parametrize("name", ["interrupted", "backhalf_thinking", "mixed"])
    def test_combined_repair_matches_repair_only(self, store_env, name):
        fixtures = self._fixtures()
        entries = fixtures[name]

        # Run repair_incomplete_turn on one copy.
        p_repair = store_env["jsonl_path"]
        _write_jsonl(p_repair, entries)
        store_env["store"].repair_incomplete_turn("sid")
        repair_out = _read_objs(p_repair)

        # Run prepare_for_resume (eviction on, but no media present) on a fresh
        # copy of the SAME input.
        p_combined = store_env["home"].parent / "combined.jsonl"
        _write_jsonl(p_combined, entries)
        store2 = ClaudeJsonlStore()
        store2.find_session_path = lambda sid, cwd="": p_combined
        store2.prepare_for_resume(
            "sid2", evict_media=True, keep_recent_turns=4)
        combined_out = _read_objs(p_combined)

        assert combined_out == repair_out

    def test_backhalf_thinking_is_not_tail_only(self, store_env):
        """Confirms the fixture actually exercises NON-tail empty-thinking (the
        case that proves Pass 2 cannot be tail-only)."""
        entries = self._fixtures()["backhalf_thinking"]
        _write_jsonl(store_env["jsonl_path"], entries)
        store_env["store"].prepare_for_resume(
            "sid", evict_media=True, keep_recent_turns=4)
        objs = _read_objs(store_env["jsonl_path"])
        uuids = [o["uuid"] for o in objs]
        assert not any(u.startswith("k") for u in uuids)  # all empty-think gone
        # real content + relinked chain survives
        assert uuids == ["u0", "x0", "x1", "x2", "ans"]
        by = {o["uuid"]: o for o in objs}
        assert by["x0"]["parentUuid"] == "u0"
        assert by["x1"]["parentUuid"] == "x0"


# ===========================================================================
# Contract-level replay-safety
# ===========================================================================

class TestReplaySafetyContract:
    def test_evicted_shape_is_replayable(self, store_env):
        """After eviction: no inline image outside recent-K, every tool_result
        keeps its tool_use_id, no stop_reason=null, no empty-signed thinking,
        and every line is valid JSON (a replayable message shape)."""
        png_old = _tiny_png(41)
        png_new = _tiny_png(42)
        entries = [
            _human_user("u1", None, "q"),
            {"type": "assistant", "uuid": "athink", "parentUuid": "u1",
             "message": {"id": "M", "stop_reason": "end_turn", "content": [
                 {"type": "thinking", "thinking": "", "signature": "S"}]}},
            _assistant_tool_use("a1", "athink", "t1"),
            _image_tool_result_user("r1", "a1", "t1", _b64(png_old)),
            _human_user("u2", "r1", "more"),
            _assistant_tool_use("a2", "u2", "t2"),
            _image_tool_result_user("r2", "a2", "t2", _b64(png_new)),
        ]
        _write_jsonl(store_env["jsonl_path"], entries)
        store_env["store"].prepare_for_resume(
            "sid", evict_media=True, keep_recent_turns=1)

        raw_lines = store_env["jsonl_path"].read_text(
            encoding="utf-8").splitlines()
        objs = []
        for line in raw_lines:
            if line.strip():
                objs.append(json.loads(line))  # valid JSONL or raises

        # every tool_result keeps tool_use_id
        tool_use_ids = set()
        for o in objs:
            msg = o.get("message", {})
            for b in (msg.get("content") or []):
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    assert b.get("tool_use_id")
                    tool_use_ids.add(b["tool_use_id"])
        assert {"t1", "t2"} <= tool_use_ids

        # no stop_reason=null anywhere
        for o in objs:
            if o.get("type") == "assistant":
                assert o["message"].get("stop_reason") is not None

        # no empty-signed thinking survives
        for o in objs:
            if o.get("type") == "assistant":
                for b in o["message"].get("content", []):
                    if b.get("type") == "thinking":
                        assert (b.get("thinking") or "").strip()

        # exactly one inline image remains (the recent r2)
        inline_images = 0
        for o in objs:
            for b in (o.get("message", {}).get("content") or []):
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    for sub in (b.get("content") or []):
                        if isinstance(sub, dict) and sub.get("type") == "image":
                            inline_images += 1
        assert inline_images == 1

    def test_roundtrips_through_load_session(self, store_env, tmp_path):
        """The evicted transcript loads through app.sessions.load_session
        without error and surfaces the placeholder text (image sub-blocks were
        already invisible in the UI)."""
        from app.sessions import load_session
        png_old = _tiny_png(51)
        png_new = _tiny_png(52)
        entries = [
            _human_user("u1", None, "q"),
            _assistant_tool_use("a1", "u1", "t1"),
            _image_tool_result_user("r1", "a1", "t1", _b64(png_old)),
            _human_user("u2", "r1", "more"),
            _assistant_tool_use("a2", "u2", "t2"),
            _image_tool_result_user("r2", "a2", "t2", _b64(png_new)),
        ]
        _write_jsonl(store_env["jsonl_path"], entries)
        store_env["store"].prepare_for_resume(
            "sid", evict_media=True, keep_recent_turns=1)
        result = load_session(store_env["jsonl_path"])
        assert "messages" in result
        # placeholder text is now surfaced as tool_result text
        joined = json.dumps(result)
        assert "[image evicted from replayed context" in joined


# ===========================================================================
# Robustness / error isolation
# ===========================================================================

class TestRobustness:
    def test_unparseable_line_passed_through(self, store_env):
        png = _tiny_png(61)
        _write_jsonl(store_env["jsonl_path"], [
            _human_user("u1", None, "q"),
            _assistant_tool_use("a1", "u1", "t1"),
            _image_tool_result_user("r1", "a1", "t1", _b64(png)),
            _human_user("u2", "r1", "more"),
            _human_user("u3", "u2", "more2"),
        ])
        # Inject a corrupt line in the middle.
        text = store_env["jsonl_path"].read_text(encoding="utf-8").splitlines()
        text.insert(3, "{ this is not valid json")
        store_env["jsonl_path"].write_text("\n".join(text) + "\n",
                                           encoding="utf-8")
        # Must not raise; the corrupt line survives verbatim.
        store_env["store"].prepare_for_resume(
            "sid", evict_media=True, keep_recent_turns=1)
        out = store_env["jsonl_path"].read_text(encoding="utf-8")
        assert "{ this is not valid json" in out

    def test_bad_base64_left_inline(self, store_env):
        """An undecodable base64 image is left inline (per-image isolation),
        never crashes the pass."""
        entries = [
            _human_user("u1", None, "q"),
            _assistant_tool_use("a1", "u1", "t1"),
            {"type": "user", "uuid": "r1", "parentUuid": "a1",
             "message": {"content": [{
                 "type": "tool_result", "tool_use_id": "t1", "content": [
                     {"type": "image", "source": {
                         "type": "base64", "data": "!!!not-base64!!!",
                         "media_type": "image/png"}}]}]}},
            _human_user("u2", "r1", "more"),
            _human_user("u3", "u2", "more2"),
        ]
        _write_jsonl(store_env["jsonl_path"], entries)
        # The corrupt base64 cannot be decoded, so the image is LEFT inline
        # (per-image isolation) and the pass must not crash.
        store_env["store"].prepare_for_resume(
            "sid", evict_media=True, keep_recent_turns=1)
        objs = _read_objs(store_env["jsonl_path"])  # still valid JSONL
        assert len(objs) == 5
        # The undecodable image survives inline (not turned into a placeholder).
        r1_sub = objs[2]["message"]["content"][0]["content"]
        assert any(b.get("type") == "image" for b in r1_sub)

    def test_missing_file_returns_false(self, store_env, monkeypatch):
        monkeypatch.setattr(store_env["store"], "find_session_path",
                            lambda sid, cwd="": None)
        assert store_env["store"].prepare_for_resume("sid") is False
