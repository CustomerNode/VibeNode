"""Context readout for an idle session: /api/session-context/<id>.

The live context number comes from ``message_start`` stream events, which only
arrive while a session is answering.  After a page load an idle session had no
reading at all, so the composer's context bar and the status panel's Context
row stayed blank.  The endpoint reads the same number from the transcript's
last reply.  Every case here is a shape seen in real transcripts (2026-10-05).
"""

import json

import pytest

import app.routes.live_api as live_api


def _line(**kw):
    return json.dumps(kw)


def _asst(read, create=0, inp=2, model="claude-opus-5-5", **extra):
    return _line(type="assistant", isSidechain=False, message={
        "model": model, "usage": {"input_tokens": inp,
                                  "cache_read_input_tokens": read,
                                  "cache_creation_input_tokens": create}}, **extra)


def _write(tmp_path, *lines, name="sess-1"):
    p = tmp_path / f"{name}.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


class TestLastContextUsage:
    def test_newest_main_thread_reply_wins(self, tmp_path):
        p = _write(tmp_path,
                   _asst(100_000),
                   _line(type="user", message={"content": "next"}),
                   _asst(258_000, create=1_862))
        u = live_api._last_context_usage(p)
        assert u["cache_read_input_tokens"] == 258_000
        assert u["cache_creation_input_tokens"] == 1_862
        assert u["model"] == "claude-opus-5-5"

    def test_sidechain_reply_is_not_the_sessions_context(self, tmp_path):
        p = _write(tmp_path,
                   _asst(300_000),
                   _line(type="assistant", isSidechain=True, message={
                       "usage": {"input_tokens": 5, "cache_read_input_tokens": 9_000}}))
        assert live_api._last_context_usage(p)["cache_read_input_tokens"] == 300_000

    def test_zero_usage_error_stub_is_skipped(self, tmp_path):
        p = _write(tmp_path,
                   _asst(400_000),
                   _line(type="assistant", isApiErrorMessage=True, message={
                       "usage": {"input_tokens": 0, "cache_read_input_tokens": 0,
                                 "cache_creation_input_tokens": 0}}))
        assert live_api._last_context_usage(p)["cache_read_input_tokens"] == 400_000

    def test_compaction_after_last_reply_reports_post_compact_size(self, tmp_path):
        p = _write(tmp_path,
                   _asst(898_000),
                   _line(type="system", subtype="compact_boundary", isSidechain=False,
                         compactMetadata={"preTokens": 898_455, "postTokens": 23_669}))
        u = live_api._last_context_usage(p)
        assert u["input_tokens"] == 23_669 and u["source"] == "compact"

    def test_no_reply_yet_is_none_not_zero(self, tmp_path):
        p = _write(tmp_path, _line(type="user", message={"content": "hi"}))
        assert live_api._last_context_usage(p) is None

    def test_reply_beyond_first_tail_window_is_found(self, tmp_path, monkeypatch):
        # A huge trailing line (e.g. a big tool result) must not hide the reply.
        monkeypatch.setattr(live_api, "_CTX_TAIL_STEPS", (64, 1 << 20))
        filler = _line(type="user", message={"content": "x" * 5_000})
        p = _write(tmp_path, _asst(123_456), filler)
        assert live_api._last_context_usage(p)["cache_read_input_tokens"] == 123_456


class TestSessionContextRoute:
    def test_route_registers_once_and_serves(self, kanban_app, tmp_path, monkeypatch):
        application, client, _ = kanban_app
        rules = [r.rule for r in application.url_map.iter_rules()]
        assert rules.count("/api/session-context/<session_id>") == 1
        _write(tmp_path, _asst(200_500), name="abc-123")
        monkeypatch.setattr(live_api, "_sessions_dir", lambda project="": tmp_path)
        d = client.get("/api/session-context/abc-123").get_json()
        assert d["usage"]["cache_read_input_tokens"] == 200_500

    @pytest.mark.parametrize("bad", ["..%2F..%2Fsecret", "a b", "x" * 200])
    def test_rejects_ids_that_are_not_session_ids(self, kanban_client, bad):
        r = kanban_client.get("/api/session-context/" + bad)
        assert r.status_code in (400, 404)

    def test_unknown_session_is_none(self, kanban_app, tmp_path, monkeypatch):
        _, client, _ = kanban_app
        monkeypatch.setattr(live_api, "_sessions_dir", lambda project="": tmp_path)
        monkeypatch.setattr(live_api, "_CLAUDE_PROJECTS", tmp_path / "none")
        assert client.get("/api/session-context/missing-1").get_json() == {"usage": None}
