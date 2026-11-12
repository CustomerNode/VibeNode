"""Controlled A/B Test: validity, qualified acceptance, sealed hidden tests,
provenance with evidence, real stopping, and honest experiment status.

Real git repositories in a temporary folder, the real controller
(``features.experiments.controlled``), and a fake web server whose "contestants" write real
code into their trial's copy and real transcripts. Most tests are deliberate
failures: an experiment that should be refused or marked failed must be.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from features.experiments import controlled as X  # noqa: E402
from features.experiments import sessions as S  # noqa: E402
from tests._fake_sessions import FakeClient, assistant, user, write_transcript  # noqa: E402

GOOD = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
WRONG = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a + b\n"
CHECK = textwrap.dedent("""
    import os, sys
    sys.excepthook = sys.__excepthook__  # a failing Python on Ubuntu loads apport, 60 ms; same traceback
    sys.path.insert(0, os.getcwd())
    import calc
    assert calc.mul(2, 3) == 6, "mul(2, 3) should be 6"
    assert calc.mul(0, 5) == 0
    print("2 passed")  # HIDDEN-TEST-MARKER
""")
MARKER = b"HIDDEN-TEST-MARKER"

# NOT split across workers (tests/_xdist_schedule.py ``XDIST_SPLITTABLE``).
# Every test here builds its own repositories, so a split is safe for the
# tests themselves and faster on a quiet machine.  But four chunks of this
# file run ~2,000 git subprocesses at once, and under load that triggers the
# "native access violation inside subprocess._wait" crash tests/conftest.py
# describes, losing workers.  Whole, the file is one worker's steady ~3 minutes.


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


_BASELINE_REPO: dict = {}    # per process: (repository, its baseline commit)


@pytest.fixture
def lab(tmp_path, tmp_path_factory, monkeypatch):
    """A baseline repository, a reference solution, an approved-adapter folder,
    and isolated Claude folders. Each spec() gets its own hidden-tests folder,
    because a protected experiment removes it once sealed."""
    repo = tmp_path / "repo"
    if _BASELINE_REPO.get(os.getpid()) is None:
        built = tmp_path_factory.getbasetemp() / "vn-ab-baseline-repo"
        built.mkdir()
        git(built, "init", "-q")
        (built / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        git(built, "add", "-A")
        git(built, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "baseline")
        commit = git(built, "rev-parse", "HEAD")
        (built / "solution.txt").write_text("SECRET-SOLUTION mul is a*b", encoding="utf-8")
        git(built, "add", "-A")
        git(built, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "solution, after the baseline")
        _BASELINE_REPO[os.getpid()] = (built, commit)
    built, commit = _BASELINE_REPO[os.getpid()]
    shutil.copytree(built, repo, symlinks=True)     # the same repository, without repeating its git calls
    ref = tmp_path / "reference"
    ref.mkdir()
    (ref / "calc.py").write_text(GOOD, encoding="utf-8")
    home = tmp_path / "claude-home"
    (home / "projects").mkdir(parents=True)
    monkeypatch.setattr(X, "claude_home", lambda: home)
    monkeypatch.setattr(X, "claude_projects_dir", lambda: home / "projects")
    adapters = tmp_path / "adapters"
    adapters.mkdir()
    (adapters / "claim.py").write_text(textwrap.dedent("""
        import json, sys
        workdir, impl = sys.argv[1], sys.argv[2]
        extra = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
        out = {"impl": impl}
        for k, v in extra.items():
            out[k] = v.replace("{wd}", workdir) if isinstance(v, str) else [x.replace("{wd}", workdir) for x in v]
        print(json.dumps(out))
    """), encoding="utf-8")
    monkeypatch.setattr(X, "ADAPTER_DIR", adapters)
    count = {"n": 0}

    def hidden(content=CHECK):
        count["n"] += 1
        d = tmp_path / f"hidden-{count['n']}"
        d.mkdir()
        (d / "check.py").write_text(content, encoding="utf-8")
        return d

    def spec(**over):
        ev = {"kind": "command", "tests_dir": str(hidden()), "place_at": "hidden_tests",
              "hidden_tests": "protected",
              "reference_dir": str(ref), "command": ["{python}", "{tests}/check.py"], "timeout": 60}
        ev.update(over.pop("evaluation_over", {}))
        s = {"id": "mul-test", "question": "Which prompt implements mul correctly?",
             "baseline": {"repo": str(repo), "commit": commit},
             "assignment": "Add mul(a, b) to calc.py.",
             "variants": [{"name": "plain", "prompt_template": "{assignment}"},
                          {"name": "careful", "prompt_template": "Be careful. {assignment}"}],
             "differs": ["prompt_template"],
             "provenance_not_applicable": "only the prompt differs; the model is checked from the transcript",
             "evaluation": ev, "forbidden_material": ["SECRET-SOLUTION"], "trials": 1, "timeout": 5}
        s.update(over)
        return s

    yield {"repo": repo, "commit": commit, "spec": spec, "hidden": hidden, "tmp": tmp_path,
           "adapters": adapters, "home": home, "ref": ref}
    # A run that ended early can leave its work folder unlistable; pytest could
    # not remove it afterwards. Make every such folder listable again.
    from features.experiments import hideaway
    for d in (tmp_path / "work").glob("*"):
        if d.is_dir():
            hideaway.unhide(d)


def contestant(code_by_variant, say=None, finish=True, spy=None):
    """Each contestant writes ``calc.py`` in its own copy and ends its turn,
    optionally claiming success in a Done card. ``spy`` runs inside the
    contestant, with its folder, while it "works"."""
    def behave(name, prompt, model, cwd, launch_id, project_dir):
        variant = name.split(" · ")[1]
        if spy:
            spy(variant, Path(cwd), prompt)
        code = code_by_variant[variant]
        if code is not None:
            Path(cwd, "calc.py").write_text(code, encoding="utf-8")
        text = (say or {}).get(variant, 'Implemented.\n```handoff\n{"type": "done", "headline": "mul added, all tests pass"}\n```')
        recs = [user(prompt)]
        if finish:
            recs.append(assistant(text, mid=launch_id, model=model or "claude-sonnet-5-5"))
        else:
            recs.append(assistant("working on it", stop="tool_use", mid=launch_id, tools=["Read"]))
        write_transcript(project_dir / f"{launch_id}.jsonl", recs)
    return behave


@pytest.fixture(autouse=True)
def _short_stop_retry(monkeypatch):
    # terminate() retries a refused stop three times; the wait between tries
    # is only a courtesy to a real engine.
    monkeypatch.setattr(S, "STOP_RETRY_WAIT", 0.01)


def run(lab, spec, behave, **kw):
    fake = kw.pop("client", None) or FakeClient(lab["tmp"] / "sessions", behave)
    kw.setdefault("watch_interval", 0.05)
    rec = X.run_experiment(spec, client=fake, records_root=lab["tmp"] / "records", work_root=lab["tmp"] / "work",
                           out=lambda *_: None, poll=0.01, settle=0, catalog=fake.catalog, stop_quiet=0.05, **kw)
    return rec, fake


def last_record(lab):
    """The experiment record on disk, for runs that ended in an exception."""
    exps = sorted((lab["tmp"] / "records").glob("*"))
    return json.loads((exps[-1] / "experiment.json").read_text(encoding="utf-8"))


def by_variant(rec):
    return {t["variant"]: t for t in rec["trials"]}


# ── validity ────────────────────────────────────────────────────────────────

def test_a_sound_spec_validates(lab):
    assert X.validate(lab["spec"]()) == []


@pytest.mark.parametrize("change, problem", [
    ({"differs": []}, "name the field"),
    ({"differs": ["model"]}, "listed as the difference but is the same"),
    ({"model": None, "variants": [{"name": "a", "prompt_template": "{assignment}", "model": "opus"},
                                  {"name": "b", "prompt_template": "x {assignment}", "model": "sonnet"}]},
     "uncontrolled difference: 'model'"),
    ({"variants": [{"name": "a", "prompt_template": "do it"}, {"name": "b", "prompt_template": "{assignment}"}]},
     "must contain {assignment}"),
    ({"variants": [{"name": "a"}]}, "at least two"),
    ({"trials": 0}, "trials"),
    ({"evaluation": {"kind": "vibes"}}, "kind"),
    ({"provenance_not_applicable": None}, "no provenance adapter"),
])
def test_an_unsound_spec_is_refused(lab, change, problem):
    probs = X.validate(lab["spec"](**change))
    assert any(problem in p for p in probs), probs


def test_a_baseline_that_does_not_exist_is_refused(lab):
    s = lab["spec"](baseline={"repo": str(lab["repo"]), "commit": "0" * 40})
    assert any("not found" in p for p in X.validate(s))


def test_hidden_tests_inside_the_baseline_or_already_in_it_are_refused(lab):
    inside = lab["repo"] / "acceptance"
    inside.mkdir()
    (inside / "check.py").write_text(CHECK, encoding="utf-8")
    s = lab["spec"](evaluation_over={"tests_dir": str(inside)})
    assert any("inside the baseline" in p for p in X.validate(s))
    # the same test content committed into the baseline is caught by content, wherever it sits
    git(lab["repo"], "add", "-A")
    git(lab["repo"], "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "leak")
    s = lab["spec"](baseline={"repo": str(lab["repo"]), "commit": git(lab["repo"], "rev-parse", "HEAD")})
    assert any("already in the baseline" in p for p in X.validate(s))


# ── independent acceptance: the checks must mean something ──────────────────

def test_a_command_that_merely_exits_successfully_cannot_verify_anything(lab):
    s = lab["spec"](evaluation_over={"command": ["{python}", "-c", "pass"], "reference_dir": None})
    rec, fake = run(lab, s, contestant({"plain": None, "careful": None}))
    assert fake.launches == []  # no contestant ever started
    assert rec["summary"]["experiment_status"] == "invalid"
    assert any("PASS on the starting code" in r for r in rec["summary"]["invalid_reasons"])
    assert all(t.get("verdict") != X.VERIFIED for t in rec["trials"])


def test_checks_that_fail_even_on_the_reference_solution_are_refused(lab):
    always_fail = "raise SystemExit(1)  # HIDDEN-TEST-MARKER\n"
    s = lab["spec"]()
    Path(s["evaluation"]["tests_dir"], "check.py").write_text(always_fail, encoding="utf-8")
    rec, fake = run(lab, s, contestant({"plain": GOOD, "careful": GOOD}))
    assert fake.launches == [] and rec["summary"]["experiment_status"] == "invalid"
    assert any("reference solution" in r for r in rec["summary"]["invalid_reasons"])


def test_no_implementation_is_never_verified(lab):
    rec, _ = run(lab, lab["spec"](), contestant({"plain": None, "careful": GOOD}))
    plain = by_variant(rec)["plain"]
    assert plain["verdict"] == X.NOT_VERIFIED
    assert "no changes to evaluate: the contestant produced nothing" in plain["verdict_reasons"]


def test_missing_acceptance_evidence_prevents_verified():
    base = {"status": S.COMPLETED, "isolation_problems": [], "contamination": [], "changed_files": ["calc.py"],
            "requires_changes": True, "provenance": {"status": "not_applicable"},
            "evaluation": {"kind": "command", "acceptance": {"exit": 0}}}
    assert X.trial_verdict(base)[0] == X.VERIFIED
    for broken in ({"evaluation": {}},                                        # never evaluated
                   {"evaluation": {"kind": "command"}},                       # checks never ran
                   {"evaluation": {"kind": "command", "acceptance": {"exit": None}}},  # could not run
                   {"evaluation": {"kind": "command", "acceptance": {"exit": 0}, "regression": {"exit": 1}}},
                   {"changed_files": []},
                   {"provenance": {"status": "failed", "reason": "x"}},
                   {"status": S.CANCELLED},
                   {"termination": "unconfirmed: still writing"},
                   {"model_mismatch": True}):
        assert X.trial_verdict({**base, **broken})[0] == X.NOT_VERIFIED, broken
    assert X.trial_verdict({**base, "evaluation": {"kind": "rubric"}})[0] == X.SUBJECTIVE


def test_failed_checks_are_a_contestant_failure_and_valid_evidence_verifies(lab):
    rec, _ = run(lab, lab["spec"](trials=2), contestant({"plain": WRONG, "careful": GOOD}))
    s = rec["summary"]
    assert s["experiment_status"] == "completed"  # a failing contestant is a result, not a broken run
    assert s["by_variant"]["plain"]["claimed_done"] == 2 and s["by_variant"]["plain"]["verified"] == 0
    assert s["by_variant"]["careful"]["verified"] == 2
    assert s["verdict"] == "supported" and s["leader"] == "careful"
    for t in rec["trials"]:
        assert t["execution_completed"] and t["checks_executed"]
        assert t["outcome_verified"] == (t["variant"] == "careful")
        if t["variant"] == "plain":
            assert "acceptance checks did not pass" in t["verdict_reasons"]
    q = rec["qualification"]
    assert q["baseline"]["exit"] != 0 and q["reference"]["exit"] == 0


# ── hidden tests stay out of the contestants' reach ─────────────────────────

def _every_file_under(root):
    return [p for p in Path(root).rglob("*") if p.is_file()]


def test_hidden_tests_are_sealed_and_your_originals_are_never_touched(lab):
    s = lab["spec"]()
    source = Path(s["evaluation"]["tests_dir"])
    before = {f.name: f.read_bytes() for f in source.rglob("*") if f.is_file()}
    found = []

    def spy(variant, cwd, prompt):
        # Where a contestant could look: the record, the work folder, its own
        # copy, the prompt. The caller's original folder is left alone and is
        # one of the watched side doors instead.
        for root in (lab["tmp"] / "records", lab["tmp"] / "work"):
            for f in _every_file_under(root):
                try:
                    if MARKER in f.read_bytes():
                        found.append(str(f))
                except OSError:
                    pass
        assert MARKER.decode() not in prompt
    rec, _ = run(lab, s, contestant({"plain": GOOD, "careful": GOOD}, spy=spy))
    assert found == []
    assert {f.name: f.read_bytes() for f in source.rglob("*") if f.is_file()} == before  # untouched
    assert all(not (Path(t["workdir"]) / "hidden_tests").exists() for t in rec["trials"])
    assert all(t["verdict"] == X.VERIFIED for t in rec["trials"])
    exp = Path(rec["report"]).parent
    sealed = json.loads((exp / "sealed.json").read_text())
    assert Path(sealed["key_path"]).parent == Path(sealed["work"])   # the key lived in the unlistable folder
    assert MARKER in (exp / "sealed" / "tests_revealed" / "check.py").read_bytes()  # revealed at the end
    assert "never changed" in Path(rec["report"]).read_text(encoding="utf-8")


def test_an_experiment_whose_folders_cannot_be_hidden_does_not_start(lab, monkeypatch):
    monkeypatch.setattr(X.hideaway, "hide", lambda folder: {"ok": False, "method": "test", "detail": "denied"})
    rec, fake = run(lab, lab["spec"](), contestant({"plain": GOOD, "careful": GOOD}))
    assert fake.launches == [] and rec["summary"]["experiment_status"] == "invalid"
    assert any("could not be made unlistable" in r for r in rec["summary"]["invalid_reasons"])


def test_a_parent_made_listable_again_stops_the_next_round(lab, monkeypatch):
    s = lab["spec"](trials=2)
    real_hidden = X.hideaway.is_hidden
    state = {"round": 0}

    def flaky(folder):
        state["round"] += 1
        return False if state["round"] > 2 else real_hidden(folder)  # "someone" changed the permission later
    monkeypatch.setattr(X.hideaway, "is_hidden", flaky)
    rec, fake = run(lab, s, contestant({"plain": GOOD, "careful": GOOD}))
    assert len(fake.launches) <= 2  # the second round never started
    assert rec["summary"]["experiment_status"] == "invalid"
    assert any("listable again" in r for r in rec["summary"]["invalid_reasons"])


def test_not_secret_runs_but_says_so(lab):
    s = lab["spec"](evaluation_over={"hidden_tests": "not_secret"})
    rec, _ = run(lab, s, contestant({"plain": GOOD, "careful": GOOD}))
    assert Path(s["evaluation"]["tests_dir"]).exists()  # nothing of the user's was removed
    assert "NOT protected" in Path(rec["report"]).read_text(encoding="utf-8")


def test_a_contestant_that_found_the_hidden_tests_is_caught(lab):
    s = lab["spec"]()
    say = {"plain": f"I read {s['evaluation']['tests_dir']}/check.py first. Done.", "careful": "Done."}
    rec, _ = run(lab, s, contestant({"plain": GOOD, "careful": GOOD}, say=say))
    assert any("hidden tests' source folder" in c for c in by_variant(rec)["plain"]["contamination"])
    assert rec["summary"]["experiment_status"] == "invalid"


def test_a_contestant_that_saw_forbidden_material_makes_the_experiment_invalid(lab):
    say = {"plain": "I found SECRET-SOLUTION in history and used it. Done.", "careful": "Done."}
    rec, _ = run(lab, lab["spec"](), contestant({"plain": GOOD, "careful": GOOD}, say=say))
    assert by_variant(rec)["plain"]["verdict"] == X.NOT_VERIFIED
    assert rec["summary"]["experiment_status"] == "invalid"
    assert "INVALID EXPERIMENT" in Path(rec["report"]).read_text(encoding="utf-8")


def test_sealed_tests_changed_after_freezing_are_caught(lab):
    exp = X.freeze(lab["spec"](), lab["tmp"] / "records", work_root=lab["tmp"] / "work")
    assert X.check_frozen(exp) == []
    enc = json.loads((exp / "sealed" / "tests.enc").read_text())
    enc["check.py"] = enc["check.py"][:-8] + "AAAAAAAA"
    (exp / "sealed" / "tests.enc").write_text(json.dumps(enc))
    assert any("sealed hidden tests changed" in p for p in X.check_frozen(exp))


# ── provenance needs evidence, not a label ──────────────────────────────────

def _prov(lab, impl, expect, evidence=None, **extra):
    cmd = ["{python}", str(lab["adapters"] / "claim.py"), "{workdir}", impl]
    if evidence:
        cmd.append(json.dumps(evidence))
    return {"command": cmd, "expect": expect, **extra}


@pytest.fixture
def trial_copy(lab, tmp_path):
    wd = tmp_path / "work" / "p"
    X.make_workdir({"repo": str(lab["repo"]), "commit": lab["commit"]}, wd)
    return wd, set(X._hash_tree(wd)), time.time() - 1


def _check(prov, trial_copy, tmp_path):
    wd, pre, started = trial_copy
    return X.check_provenance(prov, wd, "run-1", started, pre, tmp_path / "prov.log")


def test_a_favourable_label_alone_is_not_evidence(lab, trial_copy, tmp_path):
    r = _check(_prov(lab, "code-pipeline:claimed", "code-pipeline"), trial_copy, tmp_path)
    assert r["status"] == "failed" and "missing evidence" in r["reason"]


def test_a_missing_record_is_rejected(lab, trial_copy, tmp_path):
    r = _check(_prov(lab, "code-pipeline:x", "code-pipeline", {"record": "{wd}/run.json"}), trial_copy, tmp_path)
    assert "missing evidence" in r["reason"]


def test_a_record_from_another_run_is_rejected(lab, trial_copy, tmp_path):
    other = tmp_path / "other-run.json"
    other.write_text("{}")
    r = _check(_prov(lab, "code-pipeline:x", "code-pipeline", {"record": str(other)}), trial_copy, tmp_path)
    assert "another run" in r["reason"]
    stale = _check(_prov(lab, "code-pipeline:x", "code-pipeline", {"record": "{wd}/calc.py"}), trial_copy, tmp_path)
    assert "stale record" in stale["reason"]


def test_an_incorrect_version_or_identity_is_rejected(lab, trial_copy, tmp_path):
    wd = trial_copy[0]
    (wd / "run.json").write_text("{}")
    ev = {"record": "{wd}/run.json"}
    old = _check(_prov(lab, "code-pipeline:v1", "code-pipeline", ev, expect_identity="code-pipeline:v2"),
                 trial_copy, tmp_path)
    assert "wrong implementation version" in old["reason"]
    other = _check(_prov(lab, "normal", "code-pipeline", ev), trial_copy, tmp_path)
    assert "wrong implementation" in other["reason"]
    lookalike = _check(_prov(lab, "code-pipeline-fork:x", "code-pipeline", ev), trial_copy, tmp_path)
    assert "wrong implementation" in lookalike["reason"]


def test_an_adapter_written_into_the_spec_is_not_approved(lab, trial_copy, tmp_path):
    rogue = tmp_path / "rogue.py"
    rogue.write_text('print(\'{"impl": "code-pipeline:x", "record": "x"}\')')
    prov = {"command": ["{python}", str(rogue), "{workdir}"], "expect": "code-pipeline"}
    assert any("not an approved adapter" in p for p in X.adapter_problems(prov))
    assert "not an approved adapter" in _check(prov, trial_copy, tmp_path)["reason"]
    assert any("expect" in p for p in X.adapter_problems({**_prov(lab, "x", "x"), "expect": ""}))


def test_valid_evidence_is_accepted(lab, trial_copy, tmp_path):
    wd = trial_copy[0]
    (wd / "run.json").write_text("{}")
    ok = _check(_prov(lab, "code-pipeline:v2", "code-pipeline", {"record": "{wd}/run.json"},
                      expect_identity="code-pipeline:v2"), trial_copy, tmp_path)
    assert ok["status"] == "verified" and ok["record_sha256"]
    none = _check(_prov(lab, "none", "none", {"absent": ["{wd}/docs/plans/runs"]}), trial_copy, tmp_path)
    assert none["status"] == "verified"
    (wd / "docs" / "plans" / "runs" / "r1").mkdir(parents=True)
    lie = _check(_prov(lab, "none", "none", {"absent": ["{wd}/docs/plans/runs"]}), trial_copy, tmp_path)
    assert "exists" in lie["reason"]


def test_a_wrong_implementation_in_a_run_makes_it_invalid(lab):
    s = lab["spec"](provenance_not_applicable=None, differs=["prompt_template", "provenance"])
    s["variants"][0]["provenance"] = _prov(lab, "none", "none", {"absent": ["{wd}/docs/plans/runs"]})
    s["variants"][1]["provenance"] = _prov(lab, "none", "pipeline", {"absent": ["{wd}/docs/plans/runs"]})
    rec, _ = run(lab, s, contestant({"plain": GOOD, "careful": GOOD}))
    by = by_variant(rec)
    assert by["plain"]["provenance"]["status"] == "verified" and by["plain"]["verdict"] == X.VERIFIED
    assert "wrong implementation" in by["careful"]["provenance"]["reason"]
    assert rec["summary"]["experiment_status"] == "invalid"


def test_code_pipeline_adapter_binds_to_the_trial(tmp_path):
    adapter = ROOT / "src" / "features" / "experiments" / "adapters" / "code_pipeline.py"
    wd = tmp_path / "wd"
    wd.mkdir()
    r = subprocess.run([sys.executable, str(adapter), str(wd), str(time.time())], capture_output=True, text=True)
    out = json.loads(r.stdout)
    assert r.returncode == 0 and out["impl"] == "none" and out["absent"]
    run_dir = wd / "docs" / "plans" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text('{"impl": "claimed"}')
    r = subprocess.run([sys.executable, str(adapter), str(wd), str(time.time() + 3600)], capture_output=True, text=True)
    assert r.returncode == 1 and "predates this trial" in json.loads(r.stdout)["reason"]
    # a fresh run record whose identity the approved checker cannot prove is refused
    r = subprocess.run([sys.executable, str(adapter), str(wd), "0"], capture_output=True, text=True)
    assert r.returncode == 1 and "not provably from the approved pipeline" in json.loads(r.stdout)["reason"]


# ── cancel and timeout stop the sessions for real ───────────────────────────

class Writer:
    """A contestant that keeps writing to its copy and transcript until
    VibeNode is asked to stop it (or, with a stuck engine, forever)."""

    def __init__(self):
        self.client = None
        self.threads = []
        self.stop_all = threading.Event()

    def behave(self, name, prompt, model, cwd, launch_id, project_dir):
        path = project_dir / f"{launch_id}.jsonl"
        write_transcript(path, [user(prompt), assistant("starting", stop="tool_use", mid=launch_id, tools=["Bash"])],
                         age=0)

        def work():
            n = 0
            while not self.stop_all.is_set():
                if self.client.close_ok and launch_id in self.client.closed:
                    return
                n += 1
                Path(cwd, "progress.txt").write_text(str(n))
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(user(f"tick {n}", isMeta=True)) + "\n")
                time.sleep(0.02)
        th = threading.Thread(target=work, daemon=True)
        th.start()
        self.threads.append(th)


def _writer_run(lab, close_ok=True, **spec_over):
    w = Writer()
    fake = FakeClient(lab["tmp"] / "sessions", w.behave, close_ok=close_ok)
    w.client = fake
    try:
        rec, _ = run(lab, lab["spec"](**spec_over), None, client=fake)
    finally:
        w.stop_all.set()
        for th in w.threads:
            th.join(2)
    return rec, fake


def test_timeout_stops_the_session_and_its_work_stops_changing(lab):
    rec, fake = _writer_run(lab, timeout=0.3)
    assert sorted(set(fake.closed)) == sorted(t["session_id"] for t in rec["trials"])  # a stop may be retried
    for t in rec["trials"]:
        assert t["status"] == S.TIMEOUT and t["termination"] == "confirmed"
        progress = Path(t["workdir"]) / "progress.txt"
        before = progress.read_text()
        time.sleep(0.2)
        assert progress.read_text() == before  # nothing writes after the stop
        assert t["verdict"] == X.NOT_VERIFIED
        assert Path(t["dir"], "transcript.jsonl").exists()  # partial evidence kept
    # a timeout is not an evaluation: the experiment is incomplete, not completed
    assert rec["summary"]["experiment_status"] == "incomplete"
    assert all("never evaluated" in r for r in rec["summary"]["incomplete_reasons"])


def test_cancel_stops_active_sessions(lab):
    s_over = {"timeout": 30}
    w = Writer()
    fake = FakeClient(lab["tmp"] / "sessions", w.behave)
    w.client = fake
    records = lab["tmp"] / "records"
    finished = threading.Event()

    def cancel_soon():
        # Cancel as soon as the sessions are running. Setup before the first
        # launch (qualification runs, four fresh git copies) takes well over
        # a few seconds under a parallel full-suite run, so wait for the
        # launches themselves, for as long as the experiment is running.
        while not finished.is_set():
            exps = list(records.glob("*")) if records.exists() else []
            if exps and fake.launches:
                (exps[0] / "CANCEL").write_text("")
                return
            time.sleep(0.02)
    threading.Thread(target=cancel_soon, daemon=True).start()
    try:
        rec, _ = run(lab, lab["spec"](**s_over), None, client=fake)
    finally:
        finished.set()
        w.stop_all.set()
    assert all(t["status"] == S.CANCELLED and t["termination"] == "confirmed" for t in rec["trials"])
    assert len(set(fake.closed)) == 2  # both stopped (a stop is retried until it holds)
    assert rec["summary"]["experiment_status"] == "incomplete"


def test_a_stop_that_cannot_be_confirmed_is_never_a_finished_result(lab):
    rec, fake = _writer_run(lab, close_ok=False, timeout=0.3)
    s = rec["summary"]
    assert s["experiment_status"] == "infrastructure_failure"
    for t in rec["trials"]:
        assert t["termination"].startswith("unconfirmed")
        assert t["evaluation"] == {}  # never evaluated while it may still be writing
        assert t["verdict"] == X.NOT_VERIFIED
    assert "INFRASTRUCTURE FAILURE" in Path(rec["report"]).read_text(encoding="utf-8")


# ── identity, records, isolation, measurements ──────────────────────────────

def test_similar_variant_names_never_share_a_folder(lab):
    s = lab["spec"](trials=2, variants=[{"name": "alpha", "prompt_template": "{assignment}"},
                                        {"name": "alpha!", "prompt_template": "Careful: {assignment}"}])
    rec, _ = run(lab, s, contestant({"alpha": GOOD, "alpha!": GOOD}))
    dirs = [t["dir"] for t in rec["trials"]]
    assert len(set(dirs)) == 4 and len({t["workdir"] for t in rec["trials"]}) == 4
    assert {(t["variant"], t["variant_id"]) for t in rec["trials"]} == {("alpha", "v01"), ("alpha!", "v02")}
    assert rec["summary"]["by_variant"]["alpha!"]["trials"] == 2


def test_repeated_trials_are_independent_and_keep_their_own_costs(lab):
    rec, fake = run(lab, lab["spec"](trials=2), contestant({"plain": GOOD, "careful": GOOD}))
    assert len({t["run_id"] for t in rec["trials"]}) == 4
    assert len({l["cwd"] for l in fake.launches}) == 4
    for t in rec["trials"]:
        assert t["cost_usd"] == fake.costs[t["session_id"]]
        saved = json.loads(Path(t["dir"], "trial.json").read_text(encoding="utf-8"))
        assert saved["session_id"] == t["session_id"] and saved["run_id"] == t["run_id"]
        assert Path(t["dir"], "changes.diff").read_text(encoding="utf-8").count("def mul") == 1


def test_experiments_never_reuse_a_record_folder(lab):
    r1, _ = run(lab, lab["spec"](), contestant({"plain": GOOD, "careful": GOOD}))
    r2, _ = run(lab, lab["spec"](), contestant({"plain": GOOD, "careful": GOOD}))
    assert r1["experiment"] != r2["experiment"]
    with pytest.raises(FileExistsError):
        X.freeze(lab["spec"](), lab["tmp"] / "records", run_id=r1["experiment"], work_root=lab["tmp"] / "work")


def test_each_copy_holds_exactly_the_baseline_and_nothing_else(lab):
    wd = lab["tmp"] / "work" / "one"
    info = X.make_workdir({"repo": str(lab["repo"]), "commit": lab["commit"]}, wd)
    assert X.verify_baseline(info, wd) == []
    assert (wd / "calc.py").exists() and not (wd / "solution.txt").exists()
    assert git(wd, "rev-list", "--count", "HEAD") == "1" and git(wd, "remote") == ""


def test_a_baseline_mismatch_is_rejected(lab):
    wd = lab["tmp"] / "work" / "two"
    info = X.make_workdir({"repo": str(lab["repo"]), "commit": lab["commit"]}, wd)
    (wd / "calc.py").write_text("tampered", encoding="utf-8")
    assert any("changes before the contestant started" in p for p in X.verify_baseline(info, wd))
    info["baseline_tree"] = "f" * 40
    assert any("not the baseline commit's" in p for p in X.verify_baseline(info, wd))


def test_project_instructions_or_memory_around_a_copy_count_as_contamination(lab):
    parent = lab["tmp"] / "with-instructions"
    parent.mkdir()
    (parent / "CLAUDE.md").write_text("the answer is a*b", encoding="utf-8")
    wd = parent / "trial"
    wd.mkdir()
    exp = lab["tmp"] / "records" / "x"
    exp.mkdir(parents=True)
    assert any("project instructions" in p for p in X.isolation_problems(wd, exp, []))
    clean = lab["tmp"] / "clean" / "trial"
    clean.mkdir(parents=True)
    from shared.paths import encode_cwd
    (lab["home"] / "projects" / encode_cwd(str(clean.resolve())) / "memory").mkdir(parents=True)
    assert any("project memory" in p for p in X.isolation_problems(clean, exp, []))


def test_a_server_that_does_not_isolate_the_session_is_stopped_and_refused(lab):
    fake = FakeClient(lab["tmp"] / "sessions", contestant({"plain": GOOD, "careful": GOOD}), applied="ignore_cwd")
    rec, _ = run(lab, lab["spec"](), None, client=fake)
    assert len(set(fake.closed)) == 2  # stopped at once, wherever it was running
    assert all(t["verdict"] == X.NOT_VERIFIED for t in rec["trials"])
    assert rec["summary"]["experiment_status"] == "invalid"


def test_a_session_that_cannot_start_is_an_infrastructure_failure(lab):
    class Refusing(FakeClient):
        def launch(self, *a, **k):
            return {"ok": False, "error": "engine down"}
    fake = Refusing(lab["tmp"] / "sessions", None)
    rec, _ = run(lab, lab["spec"](), None, client=fake)
    assert rec["summary"]["experiment_status"] == "infrastructure_failure"


def test_one_trial_is_a_pilot_and_a_tie_is_inconclusive(lab):
    rec, _ = run(lab, lab["spec"](), contestant({"plain": WRONG, "careful": GOOD}))
    assert rec["summary"]["verdict"] == "pilot"
    assert "not proof of general superiority" in Path(rec["report"]).read_text(encoding="utf-8")
    rec, _ = run(lab, lab["spec"](), contestant({"plain": GOOD, "careful": GOOD}))
    assert rec["summary"]["verdict"] == "inconclusive" and rec["summary"]["leader"] is None


def test_rubric_experiments_are_blinded_and_stay_subjective(lab):
    s = lab["spec"](evaluation={"kind": "rubric", "rubric": ["clarity", "correctness"]})
    rec, _ = run(lab, s, contestant({"plain": GOOD, "careful": GOOD}, say={"plain": "Answer P", "careful": "Answer C"}))
    packet = Path(rec["blinded"]["packet"]).read_text(encoding="utf-8")
    assert "plain" not in packet and "careful" not in packet and "Answer P" in packet
    assert rec["summary"]["verdict"] == "subjective"
    exp = Path(rec["report"]).parent
    mapped = X.import_scores(exp, {"Output 1": {"clarity": 4}, "Output 2": {"clarity": 2}})
    assert set(mapped) == {t["run_id"] for t in rec["trials"]}
    with pytest.raises(X.SpecError):
        X.import_scores(exp, {"Output 9": {}})


# ── your files, safe paths, and a controller that fails ─────────────────────

def test_place_at_cannot_leave_the_evaluation_copy(lab):
    bait = lab["tmp"] / "bait"
    bait.mkdir()
    (bait / "keep.txt").write_text("mine")
    for pa in ("..", "../bait", str(bait), "../../x"):
        s = lab["spec"](evaluation_over={"place_at": pa})
        assert any("place_at" in p for p in X.validate(s)), pa
    # even if a spec got past validation, the cleanup refuses to reach outside
    exp = X.freeze(lab["spec"](), lab["tmp"] / "records", work_root=lab["tmp"] / "work")
    with pytest.raises(X.SpecError):
        X._safe_rmtree(bait, exp)
    assert (bait / "keep.txt").exists()
    spec = json.loads((exp / "spec.json").read_text())
    spec["evaluation"]["place_at"] = "../../bait"
    code = lab["tmp"] / "code"
    code.mkdir()
    with pytest.raises(X.SpecError):
        X.run_acceptance(spec, exp, code, exp / "trials")
    assert (bait / "keep.txt").exists()


def test_a_controller_crash_stops_sessions_before_revealing_and_keeps_your_files(lab, monkeypatch):
    s = lab["spec"](timeout=30)
    source = Path(s["evaluation"]["tests_dir"])
    w = Writer()
    fake = FakeClient(lab["tmp"] / "sessions", w.behave)
    w.client = fake
    order = []
    real_reveal, real_wait = X.reveal, X.S.wait_all

    def crash(*a, **k):
        time.sleep(0.2)
        raise RuntimeError("controller bug")

    def reveal_spy(exp):
        order.append(("reveal", sorted(set(fake.closed))))
        return real_reveal(exp)
    monkeypatch.setattr(X.S, "wait_all", crash)
    monkeypatch.setattr(X, "reveal", reveal_spy)
    try:
        with pytest.raises(RuntimeError):
            run(lab, s, None, client=fake)
    finally:
        w.stop_all.set()
    launched = sorted(l["launch_id"] for l in fake.launches)
    assert order and order[0][1] == launched          # both stopped BEFORE anything was revealed
    rec = last_record(lab)
    assert rec["summary"]["experiment_status"] == "infrastructure_failure"
    assert all(x["termination"] == "confirmed" for x in rec["containment"]["shutdown"])
    assert source.exists() and (source / "check.py").read_bytes().count(MARKER) == 1   # your originals, intact
    exp = Path(lab["tmp"] / "records") / rec["experiment"]
    assert (exp / "sealed" / "key.txt").exists()        # recoverable after the crash


# ── hidden folders: enter, not list ──────────────────────────────────────────

def test_the_trial_folders_parent_cannot_be_listed_but_known_paths_open(tmp_path):
    from features.experiments import hideaway
    parent = tmp_path / "parent"
    child = parent / "a1b2c3"
    child.mkdir(parents=True)
    (child / "calc.py").write_text("x")
    res = hideaway.hide(parent)
    if not res["ok"]:
        pytest.skip(f"cannot hide a folder here: {res}")
    try:
        assert hideaway.is_hidden(parent) is True
        assert hideaway.listable(parent) is False          # a contestant cannot browse for siblings
        assert (child / "calc.py").read_text() == "x"       # but a path it knows still opens
        assert child.is_dir() and os.listdir(child) == ["calc.py"]
        (child / "new.txt").write_text("ok")                # and it can work inside its own folder
    finally:
        assert hideaway.unhide(parent)
    assert hideaway.listable(parent) and hideaway.is_hidden(parent) is False


def test_each_trial_runs_in_an_unguessable_folder_under_the_hidden_parent(lab):
    rec, fake = run(lab, lab["spec"](), contestant({"plain": GOOD, "careful": GOOD}))
    exp = Path(rec["report"]).parent
    work = X.work_dir(exp)
    for t in rec["trials"]:
        assert Path(t["workdir"]).parent == work
        assert re.fullmatch(r"[0-9a-f]{32}", Path(t["workdir"]).name)
        assert t["token"] not in [l["prompt"] for l in fake.launches]
    assert rec["isolation"]["ok"] is True
    assert rec["containment"]["integrity"] and all(i["ok"] for i in rec["containment"]["integrity"])


# ── the break-out watch ───────────────────────────────────────────────────────

class Prober:
    """A contestant that makes one tool call (``tool``, ``make_input(cwd, sibling)``)
    shortly after starting, then keeps running until VibeNode stops it. If the
    watch misses it, the run ends by timeout instead; the two are told apart."""

    def __init__(self, tool, make_input, only="plain", then_finish=False):
        self.tool, self.make_input, self.only, self.then_finish = tool, make_input, only, then_finish
        self.client = None
        self.stop_all = threading.Event()
        self.threads = []

    def behave(self, name, prompt, model, cwd, launch_id, project_dir):
        variant = name.split(" · ")[1]
        path = project_dir / f"{launch_id}.jsonl"
        write_transcript(path, [user(prompt)], age=0)

        def work():
            for _ in range(100):  # wait for the sibling to be launched
                if len(self.client.launches) >= 2 or self.stop_all.is_set():
                    break
                time.sleep(0.01)
            sibling = next((l["cwd"] for l in self.client.launches if l["cwd"] != cwd), "")
            recs = []
            if variant == self.only:
                inp = self.make_input(cwd, sibling)
                rec = {"type": "assistant", "uuid": f"{launch_id}-probe", "message": {
                    "role": "assistant", "id": f"{launch_id}-p", "model": "claude-sonnet-5-5", "stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "id": f"tu-{launch_id}-probe", "name": self.tool, "input": inp}],
                    "usage": {"input_tokens": 1, "output_tokens": 1}}}
                recs.append(rec)
            Path(cwd, "calc.py").write_text(GOOD, encoding="utf-8")
            if self.then_finish:
                recs.append(assistant("Implemented.", mid=launch_id))
            with open(path, "a", encoding="utf-8") as f:
                for r in recs:
                    f.write(json.dumps(r) + "\n")
            while not self.then_finish and not self.stop_all.is_set():
                if launch_id in self.client.closed or any(sid in self.client.closed for sid in (launch_id,)):
                    return
                # The sibling did nothing wrong: once the watch has stopped the
                # prober it finishes its turn, so the run does not sit out the
                # timeout.  A watch that misses the prober closes nothing, so
                # the prober still runs to the timeout and the test fails.
                if variant != self.only and self.client.closed:
                    with open(path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(assistant("Implemented.", mid=launch_id)) + "\n")
                    return
                time.sleep(0.02)
        th = threading.Thread(target=work, daemon=True)
        th.start()
        self.threads.append(th)


def _probe(lab, tool, make_input, **kw):
    pr = Prober(tool, make_input, **kw)
    fake = FakeClient(lab["tmp"] / "sessions", pr.behave)
    pr.client = fake
    try:
        rec, _ = run(lab, lab["spec"](timeout=5), None, client=fake)
    finally:
        pr.stop_all.set()
        for th in pr.threads:
            th.join(2)
    return rec, fake


def _assert_contained(rec, fake, rule_part):
    plain = by_variant(rec)["plain"]
    assert plain["contained"], plain.get("error")
    assert rule_part in plain["contained"]["rule"], plain["contained"]
    assert plain["status"] == S.CANCELLED and plain["termination"] == "confirmed"
    assert plain["session_id"] in fake.closed            # stopped at once, not merely marked
    assert plain["verdict"] == X.NOT_VERIFIED
    assert rec["summary"]["experiment_status"] == "invalid" and rec["summary"]["break_outs"] == 1
    report = Path(rec["report"]).read_text(encoding="utf-8")
    assert "**Break-outs:**" in report and "session stopped: confirmed" in report


def test_watch_reading_a_siblings_work(lab):
    rec, fake = _probe(lab, "Read", lambda cwd, sib: {"file_path": str(Path(sib) / "calc.py")})
    _assert_contained(rec, fake, "another trial's folder")


def test_watch_listing_the_parent(lab):
    rec, fake = _probe(lab, "Bash", lambda cwd, sib: {"command": f'dir "{Path(cwd).parent}"'})
    _assert_contained(rec, fake, "parent")


def test_watch_calling_vibenodes_session_list(lab):
    rec, fake = _probe(lab, "Bash", lambda cwd, sib: {"command": "curl -s http://127.0.0.1:5050/api/sessions"})
    _assert_contained(rec, fake, "VibeNode's own routes")


def test_watch_reading_the_reference_or_the_tests(lab):
    ref = str(lab["ref"] / "calc.py")
    rec, fake = _probe(lab, "Read", lambda cwd, sib: {"file_path": ref})
    _assert_contained(rec, fake, "reference solution")


def test_watch_a_path_outside_the_trial(lab):
    outside = str(Path(Path.cwd().anchor) / "elsewhere-for-tests" / "notes.txt")  # not under temp (allowed)
    rec, fake = _probe(lab, "Grep", lambda cwd, sib: {"pattern": "mul", "path": outside})
    _assert_contained(rec, fake, "outside its trial folder")


def test_watch_a_permission_command(lab):
    rec, fake = _probe(lab, "Bash", lambda cwd, sib: {"command": "icacls . /remove:d someone"})
    _assert_contained(rec, fake, "permission")


def test_watch_a_canary_token_in_the_transcript(lab):
    # A tool RESULT that contains the canary: the contestant opened a protected file.
    def make(cwd, sib):
        return {"file_path": str(Path(cwd) / "calc.py")}
    pr = Prober("Read", make)
    fake = FakeClient(lab["tmp"] / "sessions", pr.behave)
    pr.client = fake
    real = pr.behave

    def behave(name, prompt, model, cwd, launch_id, project_dir):
        real(name, prompt, model, cwd, launch_id, project_dir)
        if name.split(" · ")[1] == "plain":
            exp = sorted((lab["tmp"] / "records").glob("*"))[-1]
            canary = (exp / "sealed" / "CANARY").read_text()
            time.sleep(0.05)
            with open(project_dir / f"{launch_id}.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(tool_result_text(f"tu-{launch_id}-x", canary)) + "\n")
    fake.behave = behave
    try:
        rec, _ = run(lab, lab["spec"](timeout=5), None, client=fake)
    finally:
        pr.stop_all.set()
    _assert_contained(rec, fake, "protected token")


def tool_result_text(tool_use_id, text):
    return {"type": "user", "uuid": f"r-{tool_use_id}", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_use_id, "content": text}]}}


def test_watch_lets_a_clean_contestant_finish(lab):
    def make(cwd, sib):
        return {"command": f'cd "{cwd}" && python -m pytest -q && git status'}
    rec, fake = _probe(lab, "Bash", make, then_finish=True)
    assert all(t["contained"] is None for t in rec["trials"])
    assert all(t["verdict"] == X.VERIFIED for t in rec["trials"])
    assert rec["summary"]["experiment_status"] == "completed"
    assert "watched, nothing tripped" in Path(rec["report"]).read_text(encoding="utf-8")


def test_rules_judge_paths_and_commands():
    from features.experiments.watch import Rules
    # the test harness puts "home" under temp, which is allowed. On POSIX the watch reads paths under
    # the top-level folders it knows, and /srv is one of them that is not on its allowed list.
    base = (Path(Path.cwd().anchor) / "ab-tests") if sys.platform == "win32" else Path("/srv/ab-tests")
    trial = base / "x" / "trial-abc"
    rules = Rules(trial_dir=str(trial), protected={"the record": str(base / "rec")},
                  secrets={"canary": "deadbeefcafe0123"}, parent_dir=str(trial.parent))
    assert rules.judge("Read", {"file_path": str(trial / "a.py")}) is None
    assert rules.judge("Bash", {"command": "python -m pytest -q"}) is None
    assert rules.judge("Bash", {"command": f"type {trial.parent / 'trial-def' / 'a.py'}"}) is not None
    assert "parent" in rules.judge("Bash", {"command": f"ls {trial.parent}"})
    assert "record" in rules.judge("Read", {"file_path": str(base / "rec" / "spec.json")})
    assert "routes" in rules.judge("Bash", {"command": "curl localhost:5050/api/sessions"})
    assert "permission" in rules.judge("Bash", {"command": "takeown /f ."})
    assert rules.judge("Bash", {"command": "chmod +x run.sh"}) is not None
    assert "outside" in rules.judge("Write", {"file_path": str(base / "elsewhere" / "x.txt")})
    assert rules.leaked("output: deadbeefcafe0123") is not None and rules.leaked("fine") is None
    # a Windows path written with forward slashes is not a Unix path (a real run tripped on "/U" in C:/Users)
    fwd = str(trial).replace("\\", "/")
    assert rules.judge("Bash", {"command": f'cd "{fwd}" && python -c "import calc"'}) is None
    if sys.platform == "win32":
        assert rules.judge("Bash", {"command": "ls /c" + fwd[2:]}) is None  # git-bash form of the same folder


def test_a_finished_trial_changed_afterwards_is_a_break_out(lab):
    seen = {}

    def spy(variant, cwd, prompt):
        # round 2's "plain" scribbles on round 1's "plain" copy, outside any tool call
        if variant == "plain" and "plain" in seen:
            Path(seen["plain"], "calc.py").write_text("tampered\n", encoding="utf-8")
        seen.setdefault(variant, cwd)
    rec, _ = run(lab, lab["spec"](trials=2), contestant({"plain": GOOD, "careful": GOOD}, spy=spy))
    assert rec["summary"]["experiment_status"] == "invalid"
    assert any("changed after it finished" in r for r in rec["summary"]["invalid_reasons"])
    assert any(not i["ok"] for i in rec["containment"]["integrity"])
