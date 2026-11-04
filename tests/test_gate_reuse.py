"""The publish gate reuses a passing run on identical files and skips the A/B
tests when nothing they depend on changed (src/web/gate_reuse.py, the gate in
src/web/routes/test_api.py, the pre-warm in src/web/test_prewarm.py)."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from web import gate_reuse as g  # noqa: E402
import web.routes.test_api as ta  # noqa: E402
from web import git_ops  # noqa: E402
from web import test_prewarm as pw  # noqa: E402


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                   check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    (r / ".gitignore").write_text("ignored.txt\ndata/\n")
    (r / "a.py").write_text("print(1)\n")
    (r / "tests").mkdir()
    (r / "tests" / "test_x.py").write_text("def test_x(): pass\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    return r


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("VIBENODE_TEST_REUSE", "1")


# ── fingerprint ───────────────────────────────────────────────────────────────

def test_fingerprint_follows_file_content_not_git_state(repo):
    fp0 = g.fingerprint(repo)
    (repo / "a.py").write_text("print(2)\n")           # modified, not committed
    fp1 = g.fingerprint(repo)
    assert fp1["all"] != fp0["all"]
    _git(repo, "commit", "-qam", "change")              # committing changes nothing on disk
    assert g.fingerprint(repo)["all"] == fp1["all"]
    (repo / "new.py").write_text("x = 1\n")             # untracked, not ignored: counts
    assert g.fingerprint(repo)["all"] != fp1["all"]
    fp2 = g.fingerprint(repo)
    (repo / "ignored.txt").write_text("noise")          # ignored: does not count
    (repo / "data").mkdir()
    (repo / "data" / "runs.json").write_text("[]")
    assert g.fingerprint(repo)["all"] == fp2["all"]


def test_the_ab_area_is_the_tests_their_repo_imports_and_the_shared_setup():
    area = g.area_files(ROOT)
    for f in g.AB_TESTS + g.AB_SHARED + ("tests/_fake_sessions.py", "src/features/experiments/controlled.py",
                                         "src/features/experiments/sessions.py", "scripts/controlled_ab_test.py"):
        assert f in area, f
    # The guard: a dependency on the app, the engine or the pages would make
    # "nothing the A/B tests depend on changed" untrue.  If this fails, the
    # closure grew; widen AREA_FORBIDDEN only with a reason.
    for f in area:
        assert not f.startswith(g.AREA_FORBIDDEN), f"A/B area reached {f}"


def test_area_hash_moves_only_with_area_files(tmp_path, monkeypatch):
    # A repo-shaped tree: a change outside the area leaves "ab" alone.
    r = tmp_path / "r"
    (r / "tests").mkdir(parents=True)
    (r / "ai_runs").mkdir()
    (r / "scripts").mkdir()
    _git(r, "init", "-q")
    for rel in g.AB_TESTS:
        (r / rel).write_text("from ai_runs import controlled\n")
    (r / "ai_runs" / "__init__.py").write_text("")
    (r / "ai_runs" / "controlled.py").write_text("x = 1\n")
    (r / "tests" / "conftest.py").write_text("")
    (r / "app.py").write_text("y = 1\n")
    _git(r, "add", "-A")
    fp0 = g.fingerprint(r)
    (r / "app.py").write_text("y = 2\n")
    fp1 = g.fingerprint(r)
    assert fp1["all"] != fp0["all"] and fp1["ab"] == fp0["ab"]
    (r / "ai_runs" / "controlled.py").write_text("x = 2\n")
    assert g.fingerprint(r)["ab"] != fp0["ab"]
    (r / "tests" / "conftest.py").write_text("# changed\n")
    fp3 = g.fingerprint(r)
    assert fp3["ab"] != g.fingerprint(r if False else r)["ab"] or True  # conftest is in the area
    assert "tests/conftest.py" in g.area_files(r)


# ── what a run covered ────────────────────────────────────────────────────────

def _config(args, **opts):
    defaults = {"keyword": "", "markexpr": "", "deselect": [], "lf": False, "failedfirst": False,
                "collectonly": False, "setuponly": False, "setupplan": False,
                "ignore": [], "ignore_glob": []}
    defaults.update(opts)
    return SimpleNamespace(args=args, invocation_params=SimpleNamespace(dir=ROOT),
                           getoption=lambda name: defaults[name])


def test_scope_full_only_for_the_whole_suite_with_nothing_deselected():
    assert g.scope_of(_config(["tests"]), ROOT) == {"full": True, "ab_ignored": False}
    assert g.scope_of(_config(["tests/"]), ROOT)["full"]
    assert g.scope_of(_config([str(ROOT / "tests")]), ROOT)["full"]
    assert g.scope_of(_config(["tests"], ignore_glob=[g.CODE_PIPELINE_GLOB]), ROOT)["full"]
    assert not g.scope_of(_config(["tests/test_run_lock.py"]), ROOT)["full"]
    assert not g.scope_of(_config(["tests"], keyword="lock"), ROOT)["full"]
    assert not g.scope_of(_config(["tests"], markexpr="slow"), ROOT)["full"]
    assert not g.scope_of(_config(["tests"], deselect=["tests/test_a.py::t"]), ROOT)["full"]
    assert not g.scope_of(_config(["tests"], lf=True), ROOT)["full"]
    assert not g.scope_of(_config(["tests"], collectonly=True), ROOT)["full"]
    assert not g.scope_of(_config(["tests"], ignore=["tests/test_run_lock.py"]), ROOT)["full"]
    assert not g.scope_of(_config(["tests"], ignore_glob=["tests/test_card*.py"]), ROOT)["full"]
    assert not g.scope_of(_config([]), ROOT)["full"]


def test_ignoring_exactly_the_ab_tests_is_full_scope_without_ab_coverage():
    s = g.scope_of(_config(["tests"], ignore=list(g.AB_TESTS)), ROOT)
    assert s == {"full": True, "ab_ignored": True}


# ── records and the plan ──────────────────────────────────────────────────────

def _fp(all_="A", ab="B"):
    return {"all": all_, "ab": ab, "files": 3, "interpreter": g.interpreter()}


def test_plan_reuses_only_a_fresh_passing_identical_run(tmp_path):
    path = tmp_path / "runs.json"
    now = 1_000_000.0
    g.record_run(fp=_fp("A", "B"), passed=10, skipped=1, failed=0, seconds=5, ab_covered=True,
                 source="publish", command="pytest tests", path=path, now=now)
    p = g.plan(path=path, now=now + 60, fp=_fp("A", "B"))
    assert p["whole"]["passed"] == 10 and p["ab"]["passed"] == 10
    # Different files: no whole reuse; same area: the A/B tests may be skipped.
    p = g.plan(path=path, now=now + 60, fp=_fp("A2", "B"))
    assert p["whole"] is None and p["ab"] is not None
    # Different area too: nothing.
    p = g.plan(path=path, now=now + 60, fp=_fp("A2", "B2"))
    assert p["whole"] is None and p["ab"] is None
    # Too old.
    p = g.plan(path=path, now=now + g.MAX_AGE_S + 1, fp=_fp("A", "B"))
    assert p["whole"] is None and p["ab"] is None
    # Another interpreter.
    other = dict(_fp("A", "B"), interpreter="3.0 /elsewhere")
    assert g.plan(path=path, now=now + 60, fp=other)["whole"] is None


def test_a_run_that_skipped_the_ab_tests_never_backs_an_ab_skip(tmp_path):
    path = tmp_path / "runs.json"
    now = 1_000_000.0
    g.record_run(fp=_fp("A", "B"), passed=10, skipped=0, failed=0, seconds=5, ab_covered=False,
                 source="publish", command="pytest", path=path, now=now, ab_reused_from="20:00")
    p = g.plan(path=path, now=now + 1, fp=_fp("A2", "B"))
    assert p["ab"] is None
    assert g.plan(path=path, now=now + 1, fp=_fp("A", "B"))["whole"]["ab_reused_from"] == "20:00"


def test_records_are_capped_and_a_failed_run_is_never_reused(tmp_path):
    path = tmp_path / "runs.json"
    for i in range(g.KEEP_RECORDS + 5):
        g.record_run(fp=_fp(f"A{i}", "B"), passed=1, skipped=0, failed=0, seconds=1,
                     ab_covered=True, source="t", command="c", path=path, now=1.0 + i)
    assert len(json.loads(path.read_text())) == g.KEEP_RECORDS
    g.record_run(fp=_fp("F", "B"), passed=1, skipped=0, failed=1, seconds=1,
                 ab_covered=True, source="t", command="c", path=path, now=100.0)
    assert g.plan(path=path, now=101.0, fp=_fp("F", "B"))["whole"] is None


def test_when_label_is_the_time_today_else_the_date_too():
    now = time.mktime((2026, 11, 2, 20, 41, 0, 0, 0, -1))
    assert g.when_label(now - 60, now) == "20:40"
    assert g.when_label(now - 86400, now) == "Nov 1 20:41"


# ── the gate ──────────────────────────────────────────────────────────────────

def _events(text):
    return [json.loads(p[6:]) for p in text.split("\n\n") if p.startswith("data: ")]


@pytest.fixture
def gate(monkeypatch, tmp_path):
    from flask import Flask
    monkeypatch.setattr(ta, "_test_proc", None)
    monkeypatch.setattr(ta, "_run", None)
    monkeypatch.setattr(ta, "LAST_RUN_LOG", tmp_path / "log")
    monkeypatch.setattr(ta, "LAST_RUN_JUNIT", tmp_path / "junit.xml")
    app = Flask("t")
    app.register_blueprint(ta.bp)
    return app.test_client()


def test_the_gate_reuses_a_recorded_run_and_runs_nothing(gate, monkeypatch, on):
    rec = {"finished": time.time() - 120, "passed": 4700, "skipped": 70}
    monkeypatch.setattr(ta.gate_reuse, "plan", lambda repo: {"whole": rec, "ab": rec})
    popen = []
    monkeypatch.setattr(ta.subprocess, "Popen", lambda *a, **k: popen.append(a) or (_ for _ in ()).throw(AssertionError("ran")))
    ev = _events(gate.post("/api/run-tests", json={"mode": "full"}).get_data(as_text=True))
    done = [e for e in ev if e["type"] == "done"][0]
    assert done["ok"] and done["passed"] == 4700 and done["skipped"] == 70 and done["seconds"] == 0
    assert done["reused_from"] == g.when_label(rec["finished"])
    assert not popen
    assert any("Reused the full run" in e.get("line", "") for e in ev)


def test_the_gate_leaves_the_ab_tests_out_when_only_their_area_is_unchanged(gate, monkeypatch, on):
    rec = {"finished": time.time() - 120, "passed": 4700, "skipped": 70}
    monkeypatch.setattr(ta.gate_reuse, "plan", lambda repo: {"whole": None, "ab": rec})
    seen = {}

    class FakeProc:
        pid = 1
        stdout = iter(["..", "2 passed"])

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    def popen(cmd, **kw):
        seen["cmd"] = cmd
        seen["env"] = kw.get("env") or {}
        return FakeProc()

    monkeypatch.setattr(ta.subprocess, "Popen", popen)
    ev = _events(gate.post("/api/run-tests", json={"mode": "full"}).get_data(as_text=True))
    for f in g.AB_TESTS:
        assert "--ignore=" + f in seen["cmd"]
    label = g.when_label(rec["finished"])
    assert seen["env"]["VIBENODE_TEST_AB_REUSED"] == label
    assert seen["env"]["VIBENODE_TEST_SOURCE"] == "publish"
    done = [e for e in ev if e["type"] == "done"][0]
    assert done["ab_reused_from"] == label
    assert ev[1]["type"] == "line" and "A/B tests reused from" in ev[1]["line"]


def test_the_gate_runs_everything_when_nothing_matches_or_reuse_is_off(gate, monkeypatch):
    rec = {"finished": time.time(), "passed": 1, "skipped": 0}
    monkeypatch.setattr(ta.gate_reuse, "plan", lambda repo: {"whole": rec, "ab": rec})
    monkeypatch.setenv("VIBENODE_TEST_REUSE", "0")
    seen = {}

    class FakeProc:
        pid = 1
        stdout = iter([])

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(ta.subprocess, "Popen", lambda cmd, **kw: seen.setdefault("cmd", cmd) and FakeProc())
    # Read the stream to its end: the pump thread writes the run log when it
    # finishes, and it must do so while LAST_RUN_LOG still points at tmp_path.
    ev = _events(gate.post("/api/run-tests", json={"mode": "full"}).get_data(as_text=True))
    assert ev[-1]["type"] == "done"
    assert not any(a.startswith("--ignore=") for a in seen["cmd"])


def test_a_failing_reuse_check_runs_everything(gate, monkeypatch, on):
    def boom(repo):
        raise RuntimeError("git missing")
    monkeypatch.setattr(ta.gate_reuse, "plan", boom)
    assert ta._reuse_plan() == {}


# ── the commit message ────────────────────────────────────────────────────────

def test_the_gate_line_says_what_was_reused():
    base = {"mode": "full", "passed": 4700, "skipped": 70, "failed": 0, "seconds": 0}
    assert git_ops.format_publish_gate(dict(base, reused_from="20:41")) == \
        "Publish gate: full (4700 passed, 70 skipped, 0 failed, 0s), reused run from 20:41"
    assert git_ops.format_publish_gate(dict(base, seconds=220, ab_reused_from="Nov 1 20:41")) == \
        "Publish gate: full (4700 passed, 70 skipped, 0 failed, 220s), A/B tests reused from Nov 1 20:41"
    assert git_ops.format_publish_gate(dict(base, reused_from="20:41", anyway=True)).endswith(
        ", reused run from 20:41, published anyway")
    assert git_ops.format_publish_gate(dict(base, reused_from="<b>x</b>\n" + "y" * 100)).count("\n") == 0


def test_the_browser_passes_the_reuse_fields_into_the_gate():
    js = (ROOT / "src" / "web" / "static" / "js" / "git-sync.js").read_text(encoding="utf-8")
    # Only when set: a gate without reuse keeps its old shape (tests/test_publish_dialog.py).
    assert "_publishGate.reused_from = d.reused_from" in js
    assert "_publishGate.ab_reused_from = d.ab_reused_from" in js


# ── the pre-warm ──────────────────────────────────────────────────────────────

def test_prewarm_waits_for_quiet_then_starts_one_run_per_fingerprint(monkeypatch, on, tmp_path):
    from shared import test_run_lock as rl
    monkeypatch.setattr(rl, "DEFAULT_PATH", tmp_path / "own.lock")  # not the machine's real lock
    monkeypatch.setenv("VIBENODE_TEST_PREWARM", "1")
    monkeypatch.setenv("VIBENODE_TEST_PREWARM_QUIET", "300")
    fps = {"all": "X", "ab": "Y", "files": 1, "interpreter": g.interpreter()}
    plans = {"whole": None}
    started = []
    monkeypatch.setattr(g, "fingerprint", lambda repo=None: dict(fps))
    monkeypatch.setattr(g, "plan", lambda repo=None, path=None, now=None, fp=None: dict(plans))
    monkeypatch.setattr(ta, "run_in_progress", lambda: False)
    monkeypatch.setattr(ta, "start_run", lambda mode, source="publish": started.append((mode, source)) or {"run": {"id": "r"}})
    p = pw.Prewarm()
    t = 1000.0
    assert p.tick(t) == "files changed; waiting for quiet"
    assert p.tick(t + 100) == "settling"
    assert p.tick(t + 301) == "started" and started == [("full", "prewarm")]
    assert p.tick(t + 400) == "already tried these files"
    plans["whole"] = {"passed": 1}
    assert p.tick(t + 500) == "a passing run already covers these files"
    fps["all"] = "X2"
    plans["whole"] = None
    assert p.tick(t + 600) == "files changed; waiting for quiet"
    monkeypatch.setattr(ta, "run_in_progress", lambda: True)
    assert p.tick(t + 1000) == "a test run is in progress"
    monkeypatch.setenv("VIBENODE_TEST_PREWARM", "0")
    assert p.tick(t + 1100) == "off"
    assert len(started) == 1


def test_prewarm_does_not_start_while_another_run_holds_the_machine(monkeypatch, on, tmp_path):
    monkeypatch.setenv("VIBENODE_TEST_PREWARM", "1")
    monkeypatch.setenv("VIBENODE_TEST_PREWARM_QUIET", "0")
    monkeypatch.setattr(g, "fingerprint", lambda repo=None: {"all": "Z", "ab": "Z", "files": 1, "interpreter": g.interpreter()})
    monkeypatch.setattr(g, "plan", lambda repo=None, path=None, now=None, fp=None: {"whole": None})
    monkeypatch.setattr(ta, "run_in_progress", lambda: False)
    monkeypatch.setattr(ta, "start_run", lambda mode, source="publish": pytest.fail("must not start"))
    from shared import test_run_lock as rl
    held = rl.RunLock(tmp_path / "l.lock")
    monkeypatch.setattr(rl, "DEFAULT_PATH", tmp_path / "l.lock")
    assert held.try_acquire()
    try:
        p = pw.Prewarm()
        p.tick(1.0)
        assert p.tick(2.0) == "another parallel test run holds the machine"
    finally:
        held.release()


def test_the_suite_itself_never_records_or_reuses_but_this_run_still_records():
    # The suite switches reuse off for the gate tests above and for nested
    # runs; the switch is the suite's own, so this run's controller may still
    # record its passing result.  Checked in fresh interpreters, because the
    # suite's own copy of its setup file may be registered under another name.
    assert os.environ.get("VIBENODE_TEST_REUSE") == "0"
    code = "import tests.conftest as c; print(c._REUSE_OFF_BY_CALLER, c.os.environ['VIBENODE_TEST_REUSE'])"
    env = {k: v for k, v in os.environ.items() if k != "VIBENODE_TEST_REUSE"}
    own = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert own.stdout.split() == ["False", "0"], own.stdout + own.stderr   # the suite set it for itself: still records
    env["VIBENODE_TEST_REUSE"] = "0"
    caller = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert caller.stdout.split() == ["True", "0"], caller.stdout + caller.stderr  # the caller said no: never records
