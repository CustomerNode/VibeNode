"""
Server-side naming of untitled sessions (app/auto_namer.py).

Every test builds its own project directories under tmp_path with synthetic
transcripts and patches ``app.config._CLAUDE_PROJECTS`` there (the autouse
``_isolate_daemon_home`` fixture in conftest already moves ``Path.home()``
away from the real ``~/.claude``).  The LLM titler is always a stub: no test
here launches Claude, the daemon, or the Anthropic API.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _line(kind, text, ts, sid="s"):
    if kind == "custom-title":
        return json.dumps({"type": "custom-title", "customTitle": text, "sessionId": sid})
    return json.dumps({
        "type": kind,
        "message": {"role": kind, "content": text},
        "timestamp": ts,
        "sessionId": sid,
    })


def _ts(day, hour=10, minute=0):
    return datetime(2026, 3, day, hour, minute, tzinfo=timezone.utc).isoformat()


def _write(proj: Path, sid: str, lines, mtime: float | None = None) -> Path:
    p = proj / f"{sid}.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def _conversation(sid, day, prompt="Please refactor the websocket reconnect logic in socket.js",
                  reply=True):
    lines = [_line("user", prompt, _ts(day), sid)]
    if reply:
        lines.append(_line("assistant", "Sure, starting now.", _ts(day, 10, 1), sid))
    return lines


@pytest.fixture()
def projects_root(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    import app.config as cfg
    import app.routes.sessions_api as sa
    monkeypatch.setattr(cfg, "_CLAUDE_PROJECTS", root)
    monkeypatch.setattr(sa, "_CLAUDE_PROJECTS", root)
    # Caches are process-global; start each test clean.
    cfg._summary_cache.clear()
    cfg._summary_cache_index.clear()
    import app.sessions as sess
    sess._orphan_cache.clear()
    return root


@pytest.fixture()
def project(projects_root):
    name = "C--Users-test-Documents-proj"
    (projects_root / name).mkdir()
    return name


@pytest.fixture()
def app():
    from app import create_app
    return create_app(testing=True)


def _stub_titler(calls: list):
    def titler(messages):
        calls.append([m.get("content", "")[:40] for m in messages])
        first = next((m["content"] for m in messages if m.get("role") == "user"), "")
        return "Stub " + first.split()[1].capitalize() + " title"
    return titler


# ---------------------------------------------------------------------------
# name_transcript: the shared persist step
# ---------------------------------------------------------------------------

class TestNameTranscript:
    def test_persists_like_the_autoname_route(self, projects_root, project, monkeypatch):
        from app import auto_namer
        import app.routes.sessions_api as sa
        proj = projects_root / project
        _write(proj, "a1", _conversation("a1", 1))
        emitted = []
        monkeypatch.setattr(sa, "_emit_session_renamed",
                            lambda sid, title, p: emitted.append((sid, title, p)))
        calls = []
        r = auto_namer.name_transcript(project, "a1", titler=_stub_titler(calls))
        assert r["ok"] is True
        assert r["title"] == "Stub Refactor title"
        # A custom-title line was appended to the transcript ...
        tail = (proj / "a1.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1]
        assert json.loads(tail) == {"type": "custom-title", "customTitle": "Stub Refactor title",
                                    "sessionId": "a1"}
        # ... the names file carries it ...
        names = json.loads((proj / "_session_names.json").read_text(encoding="utf-8"))
        assert names["a1"] == "Stub Refactor title"
        # ... and every other client was told.
        assert emitted == [("a1", "Stub Refactor title", project)]
        assert calls, "the titler was called with the transcript's messages"

    def test_user_set_name_is_never_overridden(self, projects_root, project):
        from app import auto_namer
        proj = projects_root / project
        _write(proj, "a1", _conversation("a1", 1))
        (proj / "_session_names.json").write_text(json.dumps({"a1": "Mine"}), encoding="utf-8")
        r = auto_namer.name_transcript(project, "a1", titler=lambda m: "Robot title here")
        assert r["ok"] is False and r["skipped"] is True
        assert "User-set" in r["reason"]

    def test_existing_custom_title_line_is_respected(self, projects_root, project):
        from app import auto_namer
        proj = projects_root / project
        _write(proj, "a1", _conversation("a1", 1) + [_line("custom-title", "Already", "", "a1")])
        r = auto_namer.name_transcript(project, "a1", titler=lambda m: "Robot title here")
        assert r["skipped"] is True and r["reason"] == "already titled"

    def test_untitled_placeholder_is_not_saved(self, projects_root, project):
        from app import auto_namer
        proj = projects_root / project
        _write(proj, "a1", _conversation("a1", 1))
        r = auto_namer.name_transcript(project, "a1", titler=lambda m: "Untitled Session")
        assert r["ok"] is False
        assert not (proj / "_session_names.json").exists()
        assert "custom-title" not in (proj / "a1.jsonl").read_text(encoding="utf-8")

    def test_cheap_path_skips_the_llm_when_the_heuristic_is_tight(self, projects_root, project):
        from app import auto_namer
        proj = projects_root / project
        _write(proj, "a1", _conversation("a1", 1, prompt="Fix the login redirect bug"))
        llm_calls = []

        def llm(messages):
            llm_calls.append(1)
            return "LLM title here"
        r = auto_namer.name_transcript(project, "a1", cheap_first=True, titler=llm)
        assert r["ok"] is True and r["cheap"] is True
        assert r["title"] == "Fix the login redirect bug"
        assert llm_calls == []

    def test_cheap_path_falls_back_to_the_llm_on_a_loose_title(self, projects_root, project):
        from app import auto_namer
        proj = projects_root / project
        loose = ("You are the drafting agent of a mail-forwarding pipeline that "
                 "turns inbound email into structured tickets for the team")
        _write(proj, "a1", _conversation("a1", 1, prompt=loose))
        llm_calls = []

        def llm(messages):
            llm_calls.append(1)
            return "Drafting agent pipeline"
        r = auto_namer.name_transcript(project, "a1", cheap_first=True, titler=llm)
        assert r["ok"] is True and r["cheap"] is False
        assert r["title"] == "Drafting agent pipeline"
        assert llm_calls == [1]


# ---------------------------------------------------------------------------
# AutoNamer: watermark semantics
# ---------------------------------------------------------------------------

class TestAutoNamerWatermark:
    def _namer(self, projects_root, state_path, titler, clock, **kw):
        from app.auto_namer import AutoNamer
        return AutoNamer(None, state_path=state_path, projects_root=projects_root,
                         titler=titler, clock=clock, min_age_seconds=0, **kw)

    def test_backlog_is_not_named_on_first_run(self, projects_root, project, tmp_path):
        proj = projects_root / project
        base = time.time() - 3600
        for i in range(5):
            _write(proj, f"old{i}", _conversation(f"old{i}", 1), mtime=base + i)
        calls = []
        now = [base + 1000]
        namer = self._namer(projects_root, tmp_path / "state.json", _stub_titler(calls),
                            lambda: now[0])
        assert namer.tick() == []
        assert calls == []
        state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        assert state["watermarks"][project] == now[0]
        # A second tick with nothing new still names nothing.
        now[0] += 60
        assert namer.tick() == []
        assert calls == []

    def test_sessions_modified_after_the_watermark_are_named(self, projects_root, project, tmp_path):
        proj = projects_root / project
        base = time.time() - 3600
        _write(proj, "old", _conversation("old", 1), mtime=base)
        calls = []
        now = [base + 100]
        namer = self._namer(projects_root, tmp_path / "state.json", _stub_titler(calls),
                            lambda: now[0])
        namer.tick()  # first sight: watermark = now
        _write(proj, "fresh", _conversation("fresh", 2, prompt="Please audit the test suite"),
               mtime=base + 200)
        now[0] = base + 300
        results = namer.tick()
        assert [r["session_id"] for r in results if r["ok"]] == ["fresh"]
        assert json.loads((proj / "_session_names.json").read_text(encoding="utf-8")) == {
            "fresh": "Stub Audit title"}
        # The watermark advanced to the processed transcript's mtime, not to now.
        assert namer.watermarks[project] == pytest.approx(base + 200)
        # "old" was never touched.
        assert "custom-title" not in (proj / "old.jsonl").read_text(encoding="utf-8")

    def test_at_most_three_per_tick_and_the_rest_next_tick(self, projects_root, project, tmp_path):
        proj = projects_root / project
        base = time.time() - 3600
        calls = []
        now = [base]
        namer = self._namer(projects_root, tmp_path / "state.json", _stub_titler(calls),
                            lambda: now[0])
        namer.tick()
        for i in range(5):
            _write(proj, f"n{i}", _conversation(f"n{i}", 3), mtime=base + 10 + i)
        now[0] = base + 100
        first = [r["session_id"] for r in namer.tick()]
        assert first == ["n0", "n1", "n2"]
        assert namer.watermarks[project] == pytest.approx(base + 12)
        second = [r["session_id"] for r in namer.tick()]
        assert second == ["n3", "n4"]
        assert namer.tick() == []

    def test_requires_an_assistant_reply_and_comes_back_after_one(self, projects_root, project, tmp_path):
        proj = projects_root / project
        base = time.time() - 3600
        calls = []
        now = [base]
        namer = self._namer(projects_root, tmp_path / "state.json", _stub_titler(calls),
                            lambda: now[0])
        namer.tick()
        _write(proj, "pending", _conversation("pending", 4, reply=False), mtime=base + 10)
        now[0] = base + 100
        assert namer.tick() == []
        assert calls == []
        # The reply lands: mtime moves past the watermark and the session is named.
        _write(proj, "pending", _conversation("pending", 4, reply=True), mtime=base + 150)
        now[0] = base + 200
        results = namer.tick()
        assert [r["session_id"] for r in results if r["ok"]] == ["pending"]

    def test_titled_sessions_and_hidden_ids_are_skipped(self, projects_root, project, tmp_path):
        from app.session_store import _mark_deleted
        proj = projects_root / project
        base = time.time() - 3600
        calls = []
        now = [base]
        namer = self._namer(projects_root, tmp_path / "state.json", _stub_titler(calls),
                            lambda: now[0])
        namer.tick()
        _write(proj, "titled", _conversation("titled", 5) + [_line("custom-title", "Has one", "", "titled")],
               mtime=base + 10)
        _write(proj, "gone", _conversation("gone", 5), mtime=base + 11)
        _mark_deleted("gone", project)
        now[0] = base + 100
        assert namer.tick() == []
        assert calls == []

    def test_min_age_defers_a_transcript_still_being_written(self, projects_root, project, tmp_path):
        from app.auto_namer import AutoNamer
        proj = projects_root / project
        base = time.time() - 3600
        calls = []
        now = [base]
        namer = AutoNamer(None, state_path=tmp_path / "state.json", projects_root=projects_root,
                          titler=_stub_titler(calls), clock=lambda: now[0], min_age_seconds=30)
        namer.tick()
        _write(proj, "live", _conversation("live", 6), mtime=base + 10)
        now[0] = base + 20  # only 10 s old
        assert namer.tick() == []
        now[0] = base + 60
        assert [r["session_id"] for r in namer.tick()] == ["live"]

    def test_state_survives_a_new_instance(self, projects_root, project, tmp_path):
        proj = projects_root / project
        base = time.time() - 3600
        calls = []
        namer = self._namer(projects_root, tmp_path / "state.json", _stub_titler(calls),
                            lambda: base)
        namer.tick()
        # New process: the watermark is read back, so the backlog stays untouched
        # and a newer transcript is still found.
        _write(proj, "old", _conversation("old", 1), mtime=base - 10)
        _write(proj, "new", _conversation("new", 2), mtime=base + 10)
        namer2 = self._namer(projects_root, tmp_path / "state.json", _stub_titler(calls),
                             lambda: base + 100)
        assert namer2.watermarks[project] == base
        assert [r["session_id"] for r in namer2.tick()] == ["new"]

    def test_system_and_subagent_dirs_are_ignored(self, projects_root, tmp_path):
        from app.config import _encode_cwd, _SYSTEM_UTILITY_CWD
        (projects_root / _encode_cwd(_SYSTEM_UTILITY_CWD)).mkdir()
        (projects_root / "subagents-x").mkdir()
        (projects_root / "real").mkdir()
        namer = self._namer(projects_root, tmp_path / "state.json", lambda m: "x", time.time)
        namer.tick()
        assert set(namer.watermarks) == {"real"}

    def test_not_started_in_a_test_app(self):
        """create_app(testing=True) must not start the namer thread."""
        import threading
        from app import create_app
        before = {t.name for t in threading.enumerate()}
        create_app(testing=True)
        after = {t.name for t in threading.enumerate()}
        assert "auto-namer" not in (after - before)


# ---------------------------------------------------------------------------
# Bulk namer
# ---------------------------------------------------------------------------

class TestBulkNamer:
    @pytest.fixture(autouse=True)
    def _fresh_jobs(self):
        from app import auto_namer
        auto_namer._bulk_jobs.clear()
        yield
        auto_namer._bulk_jobs.clear()

    def _seed(self, proj, n, start_day=1):
        # Oldest first by first-message timestamp; file mtimes deliberately
        # reversed so the order cannot come from the filesystem.
        base = time.time() - 10_000
        for i in range(n):
            _write(proj, f"b{i:02d}",
                   _conversation(f"b{i:02d}", start_day + i,
                                 prompt=f"You are the drafting agent of a mail-forwarding pipeline number {i} that handles mail"),
                   mtime=base + (n - i))

    def test_names_oldest_first_and_emits_progress(self, projects_root, project, app, monkeypatch):
        from app import auto_namer
        import app.routes.sessions_api as sa
        monkeypatch.setattr(sa, "_emit_session_renamed", lambda *a: None)
        proj = projects_root / project
        self._seed(proj, 12)
        order = []

        def titler(messages):
            first = next(m["content"] for m in messages if m.get("role") == "user")
            n = first.split("number ")[1].split()[0]
            order.append(int(n))
            return f"Pipeline {n} title"
        events = []
        status, started = auto_namer.start_bulk(app, project, titler=titler,
                                                emit=events.append, threaded=False)
        assert started is True
        assert order == list(range(12)), "oldest first"
        final = auto_namer.bulk_status(project)
        assert final["running"] is False
        assert final["total"] == 12 and final["done"] == 12 and final["named"] == 12
        assert final["current_title"] == "Pipeline 11 title"
        # Progress: once at start, every 5, and at the end.
        dones = [e["done"] for e in events]
        assert dones == [0, 5, 10, 12]
        assert events[-1]["finished"] is True
        assert all(e["project"] == project and e["total"] == 12 for e in events)
        assert events[1]["current_title"] == "Pipeline 4 title"
        names = json.loads((proj / "_session_names.json").read_text(encoding="utf-8"))
        assert len(names) == 12

    def test_limit_names_only_the_oldest_n(self, projects_root, project, app, monkeypatch):
        from app import auto_namer
        import app.routes.sessions_api as sa
        monkeypatch.setattr(sa, "_emit_session_renamed", lambda *a: None)
        proj = projects_root / project
        self._seed(proj, 6)
        events = []
        auto_namer.start_bulk(app, project, limit=2, titler=lambda m: "Two word title",
                              emit=events.append, threaded=False)
        names = json.loads((proj / "_session_names.json").read_text(encoding="utf-8"))
        assert set(names) == {"b00", "b01"}
        assert events[-1]["total"] == 2 and events[-1]["done"] == 2

    def test_refuses_to_start_twice_for_the_same_project(self, projects_root, project, app, monkeypatch):
        import threading
        from app import auto_namer
        import app.routes.sessions_api as sa
        monkeypatch.setattr(sa, "_emit_session_renamed", lambda *a: None)
        proj = projects_root / project
        self._seed(proj, 3)
        gate = threading.Event()
        entered = threading.Event()

        def slow_titler(messages):
            entered.set()
            gate.wait(5)
            return "Slow robot title"
        status, started = auto_namer.start_bulk(app, project, titler=slow_titler,
                                                emit=lambda p: None, threaded=True)
        assert started is True
        assert entered.wait(5)
        again, started_again = auto_namer.start_bulk(app, project, titler=slow_titler,
                                                     emit=lambda p: None, threaded=True)
        assert started_again is False
        assert again["running"] is True
        # A different project is independent.
        (projects_root / "other").mkdir()
        s2, started2 = auto_namer.start_bulk(app, "other", emit=lambda p: None, threaded=False)
        assert started2 is True and s2["total"] == 0
        gate.set()
        for _ in range(100):
            if not auto_namer.bulk_status(project)["running"]:
                break
            time.sleep(0.05)
        assert auto_namer.bulk_status(project)["running"] is False
        # Once finished, a new run may start.
        s3, started3 = auto_namer.start_bulk(app, project, titler=lambda m: "Robot title two",
                                             emit=lambda p: None, threaded=False)
        assert started3 is True

    def test_skips_empty_and_titled_sessions(self, projects_root, project, app, monkeypatch):
        from app import auto_namer
        import app.routes.sessions_api as sa
        monkeypatch.setattr(sa, "_emit_session_renamed", lambda *a: None)
        proj = projects_root / project
        self._seed(proj, 2)
        _write(proj, "empty", [json.dumps({"type": "file-history-snapshot", "snapshot": {}})])
        _write(proj, "titled", _conversation("titled", 9) + [_line("custom-title", "Done", "", "titled")])
        counts = auto_namer.count_untitled(project)
        assert counts == {"total": 4, "untitled": 3, "nameable": 2}
        auto_namer.start_bulk(app, project, titler=lambda m: "Robot made title",
                              emit=lambda p: None, threaded=False)
        st = auto_namer.bulk_status(project)
        assert st["total"] == 2 and st["named"] == 2

    def test_cheap_title_is_never_duplicated(self, projects_root, project, app, monkeypatch):
        """Scripted sessions share an opening line; the heuristic would give
        them all one title.  Only the first may take it; repeats go to the LLM."""
        from app import auto_namer
        import app.routes.sessions_api as sa
        monkeypatch.setattr(sa, "_emit_session_renamed", lambda *a: None)
        proj = projects_root / project
        for i in range(3):
            _write(proj, f"d{i}", _conversation(f"d{i}", 1 + i, prompt="Fix the login redirect bug"))
        llm = []

        def titler(messages):
            llm.append(1)
            return f"Login fix variant {len(llm)}"
        auto_namer.start_bulk(app, project, titler=titler, emit=lambda p: None, threaded=False)
        names = json.loads((proj / "_session_names.json").read_text(encoding="utf-8"))
        assert sorted(names.values()) == ["Fix the login redirect bug",
                                          "Login fix variant 1", "Login fix variant 2"]
        assert len(llm) == 2
        assert auto_namer.bulk_status(project)["cheap"] == 1

    def test_a_titler_exception_counts_as_failed_and_the_run_finishes(self, projects_root, project, app, monkeypatch):
        from app import auto_namer
        import app.routes.sessions_api as sa
        monkeypatch.setattr(sa, "_emit_session_renamed", lambda *a: None)
        proj = projects_root / project
        self._seed(proj, 2)

        def boom(messages):
            raise RuntimeError("api down")
        events = []
        auto_namer.start_bulk(app, project, titler=boom, emit=events.append, threaded=False)
        st = auto_namer.bulk_status(project)
        assert st["running"] is False and st["failed"] == 2 and st["named"] == 0
        assert events[-1]["finished"] is True


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

class TestBulkRoutes:
    @pytest.fixture(autouse=True)
    def _fresh_jobs(self):
        from app import auto_namer
        auto_namer._bulk_jobs.clear()
        yield
        auto_namer._bulk_jobs.clear()

    def test_untitled_count(self, projects_root, project, app):
        proj = projects_root / project
        _write(proj, "u1", _conversation("u1", 1))
        _write(proj, "t1", _conversation("t1", 2) + [_line("custom-title", "T", "", "t1")])
        r = app.test_client().get(f"/api/sessions/untitled-count?project={project}")
        assert r.status_code == 200
        assert r.get_json() == {"project": project, "total": 2, "untitled": 1, "nameable": 1}

    def test_start_status_and_409(self, projects_root, project, app, monkeypatch):
        from app import auto_namer
        proj = projects_root / project
        _write(proj, "u1", _conversation("u1", 1))
        captured = {}

        def fake_start(app_, proj_, limit=None, **kw):
            captured.update(project=proj_, limit=limit)
            return {"project": proj_, "running": True}, True
        monkeypatch.setattr(auto_namer, "start_bulk", fake_start)
        c = app.test_client()
        r = c.post(f"/api/sessions/name-untitled?project={project}", json={"limit": 7})
        assert r.status_code == 202
        assert r.get_json()["ok"] is True and r.get_json()["started"] is True
        assert captured == {"project": project, "limit": 7}

        r = c.post(f"/api/sessions/name-untitled?project={project}", json={"limit": 0})
        assert r.status_code == 400
        r = c.post(f"/api/sessions/name-untitled?project={project}", json={"limit": "7"})
        assert r.status_code == 400

        monkeypatch.setattr(auto_namer, "start_bulk",
                            lambda *a, **k: ({"project": project, "running": True}, False))
        r = c.post(f"/api/sessions/name-untitled?project={project}")
        assert r.status_code == 409
        assert r.get_json()["error"] == "already running"

        r = c.get(f"/api/sessions/name-untitled/status?project={project}")
        assert r.status_code == 200
        assert r.get_json()["running"] is False and r.get_json()["project"] == project

    def test_traversal_guard_applies(self, app):
        c = app.test_client()
        assert c.post("/api/sessions/name-untitled?project=..%2Fevil").status_code == 400
        assert c.get("/api/sessions/untitled-count?project=a/b").status_code == 400
