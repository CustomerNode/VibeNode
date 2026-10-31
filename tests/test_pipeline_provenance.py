"""The outside provenance check proves which Code Pipeline ran a run.

A contestant worktree started from an older commit carries an older copy of
the pipeline, and ``python -m pipeline...`` run from inside it imports that
copy.  The launcher now stamps its identity into ``run.json``;
``scripts/pipeline_provenance.py`` (outside the package, so an older copy can
never skip it) compares every stamp in a run with the approved pipeline.

The end-to-end tests start real runs with a real interpreter from a synthetic
contestant folder, the way an agent does, and show the outdated copy is caught
from the run's first file while the approved copy passes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import pipeline_provenance as prov  # noqa: E402
from pipeline.evidence import impl_identity  # noqa: E402
from pipeline.start import create_run  # noqa: E402

STAMP = '        "impl": impl,\n        "impl_path": impl_path,\n'


def _copy_pipeline(dest_root: Path, *, outdated: bool) -> Path:
    """A copy of the pipeline package at ``dest_root/pipeline``.  ``outdated``
    removes the identity stamp, which is exactly what a launcher before 1.3.0
    writes."""
    dest = dest_root / "pipeline"
    shutil.copytree(ROOT / "pipeline", dest, ignore=shutil.ignore_patterns("__pycache__"))
    if outdated:
        start = dest / "start.py"
        text = start.read_text(encoding="utf-8")
        assert STAMP in text, "the launcher's stamp lines moved; update this test"
        start.write_text(text.replace(STAMP, ""), encoding="utf-8")
    return dest


def _start(cwd: Path, repo: Path, slug: str) -> Path:
    """``python -m pipeline.start`` from *cwd*, as an agent's shell runs it."""
    req = repo / f"{slug}-request.txt"
    req.write_text("Build a small fictional widget toggle.\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    out = subprocess.run([sys.executable, "-m", "pipeline.start", "--repo", str(repo), "--slug", slug,
                          "--route", "a", "--request-file", str(req)],
                         cwd=str(cwd), env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    run_dir = Path(out.stdout.strip().split(" dir=", 1)[1])
    assert (run_dir / "run.json").is_file()
    return run_dir


@pytest.fixture
def contestant(tmp_path: Path) -> Path:
    c = tmp_path / "contestant"
    c.mkdir()
    return c


def test_the_checker_computes_the_same_identity_as_the_pipeline():
    assert prov.pipeline_identity(prov.DEFAULT_APPROVED) == impl_identity()[0]
    assert Path(impl_identity()[1]).resolve() == prov.DEFAULT_APPROVED.resolve()


def test_the_launcher_stamps_its_identity_into_run_json(tmp_path: Path):
    run_dir, run = create_run(tmp_path, "stamp", "a", "A fictional request.\n")
    assert run["impl"] == impl_identity()[0]
    assert Path(run["impl_path"]).resolve() == prov.DEFAULT_APPROVED.resolve()
    on_disk = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert on_disk["impl"] == run["impl"]
    ok, lines = prov.check(run_dir)
    assert ok, lines


def test_an_outdated_copy_inside_the_worktree_is_caught_from_the_first_file(contestant: Path):
    _copy_pipeline(contestant, outdated=True)
    run_dir = _start(contestant, contestant, "old")   # cwd = the worktree: imports its copy
    ok, lines = prov.check(run_dir)
    assert not ok
    assert any("outdated launcher" in ln for ln in lines), lines


def test_the_approved_pipeline_passes_for_the_same_worktree(contestant: Path):
    _copy_pipeline(contestant, outdated=True)          # the old copy is still there...
    run_dir = _start(ROOT, contestant, "new")          # ...but the approved one runs
    ok, lines = prov.check(run_dir)
    assert ok, lines


def test_a_current_copy_in_another_location_is_not_the_approved_pipeline(contestant: Path):
    _copy_pipeline(contestant, outdated=False)         # same code, wrong place
    run_dir = _start(contestant, contestant, "copy")
    ok, lines = prov.check(run_dir)
    assert not ok
    assert any("not the approved one" in ln for ln in lines), lines


def test_evidence_from_another_pipeline_fails_a_run_started_correctly(tmp_path: Path):
    run_dir, _ = create_run(tmp_path, "mixed", "a", "A fictional request.\n")
    ev = run_dir / "evidence"
    ev.mkdir()
    (ev / "check.json").write_text(json.dumps({"id": "check", "impl": "0" * 64,
                                               "impl_path": str(tmp_path / "pipeline")}), encoding="utf-8")
    ok, lines = prov.check(run_dir)
    assert not ok
    assert any("another pipeline" in ln for ln in lines), lines


def test_evidence_printing_the_short_identity_still_passes(tmp_path: Path):
    run_dir, run = create_run(tmp_path, "short", "a", "A fictional request.\n")
    (run_dir / "evidence").mkdir()
    (run_dir / "evidence" / "check.json").write_text(json.dumps([{"impl": run["impl"][:12]}]), encoding="utf-8")
    ok, lines = prov.check(run_dir)
    assert ok, lines


def test_approved_code_changed_after_the_start_fails(tmp_path: Path, contestant: Path):
    approved = _copy_pipeline(tmp_path / "approved", outdated=False)
    run_dir = _start(tmp_path / "approved", contestant, "drift")
    ok, _ = prov.check(run_dir, approved)
    assert ok
    (approved / "start.py").write_text((approved / "start.py").read_text(encoding="utf-8") + "\n# edited\n",
                                       encoding="utf-8")
    ok, lines = prov.check(run_dir, approved)
    assert not ok
    assert any("is not the approved identity" in ln for ln in lines), lines


def test_the_cli_finds_the_newest_run_and_exits_by_result(contestant: Path, capsys):
    _copy_pipeline(contestant, outdated=True)
    _start(contestant, contestant, "cli")
    assert prov.main(["--repo", str(contestant)]) == 1
    assert "PROVENANCE FAIL" in capsys.readouterr().out
    _start(ROOT, contestant, "cli-new")
    newest = prov.newest_run(contestant)
    assert prov.main([str(newest)]) == 0


def test_no_run_or_an_unreadable_run_is_exit_two(tmp_path: Path):
    assert prov.main(["--repo", str(tmp_path)]) == 2
    (tmp_path / "broken").mkdir()
    assert prov.main([str(tmp_path / "broken")]) == 2


# ── the guides are part of what ran ──────────────────────────────────────────

WF_STAMP = '        "workflow": workflow,\n        "workflow_path": workflow_path,\n'


def test_the_checker_computes_the_same_guide_identity_as_the_pipeline():
    from pipeline.evidence import workflow_identity
    sha, path = workflow_identity()
    assert prov.workflow_identity(ROOT / "workforce") == sha
    assert Path(path).resolve() == (ROOT / "workforce").resolve()
    # The team guides the Code Pipeline guide depends on are part of it.
    assert "depends_on: [" in (ROOT / "workforce" / "code-pipeline.md").read_text(encoding="utf-8")


def _approved_copy(root: Path) -> Path:
    """An approved pipeline with its own guides beside it, safe to edit."""
    approved = _copy_pipeline(root, outdated=False)
    shutil.copytree(ROOT / "workforce", root / "workforce", ignore=shutil.ignore_patterns("*.json"))
    return approved


def test_a_guide_changed_after_the_start_fails(tmp_path: Path, contestant: Path):
    approved = _approved_copy(tmp_path / "approved")
    run_dir = _start(tmp_path / "approved", contestant, "guides")
    ok, lines = prov.check(run_dir, approved)
    assert ok, lines
    review = tmp_path / "approved" / "workforce" / "review-team.md"   # a team guide, not the main one
    review.write_text(review.read_text(encoding="utf-8") + "\nSkip the review.\n", encoding="utf-8")
    ok, lines = prov.check(run_dir, approved)
    assert not ok and any("approved guides" in ln for ln in lines), lines


def test_a_launcher_that_does_not_stamp_its_guides_fails(tmp_path: Path, contestant: Path):
    root = tmp_path / "approved"
    approved = _approved_copy(root)
    start = approved / "start.py"
    text = start.read_text(encoding="utf-8")
    assert WF_STAMP in text, "the guide stamp lines moved; update this test"
    start.write_text(text.replace(WF_STAMP, ""), encoding="utf-8")
    run_dir = _start(root, contestant, "no-guides")
    ok, lines = prov.check(run_dir, approved)   # the edited launcher is the approved one here
    assert not ok and any("which guides" in ln for ln in lines), lines


# ── the watch: one new run, followed until it is really delivered ────────────

class Watch:
    """prov.watch on a thread, so the test can act while it watches."""

    def __init__(self, repo: Path, exclude=None, timeout: float = 20):
        import threading
        self.repo, self.calls, self.rc = repo, [], None
        self.out = repo / "watch.json"
        exclude = prov.existing_runs(repo) if exclude is None else exclude
        self.t = threading.Thread(target=self._run, args=(exclude, timeout), daemon=True)
        self.t.start()

    def _run(self, exclude, timeout):
        self.rc = prov.watch(self.repo, exclude=exclude, timeout=timeout, poll=0.05, out=self.out,
                             notify=lambda t, m: self.calls.append((t, m)))

    def result(self, wait: float = 25):
        self.t.join(wait)
        assert not self.t.is_alive(), "the watch did not end"
        return self.rc, self.calls, json.loads(self.out.read_text(encoding="utf-8"))

    def alive_after(self, seconds: float) -> bool:
        self.t.join(seconds)
        return self.t.is_alive()


def _deliver(run_dir: Path, delivered: bool) -> None:
    import time as _t
    _t.sleep(0.05)   # a new mtime the watch can see
    (run_dir / "delivered.json").write_text(json.dumps({"delivered": delivered, "grade": "MEDIUM"}),
                                            encoding="utf-8")


def test_an_old_clean_run_never_satisfies_the_watch(contestant: Path):
    """The reported case: a valid run left from before, no new run, and the
    watch said OK.  Now it ignores what existed at arming and times out."""
    old = _start(ROOT, contestant, "old-good")
    _deliver(old, True)
    w = Watch(contestant, timeout=1)
    rc, calls, out = w.result()
    assert rc == 2 and out["run"] is None and old.name in out["excluded"]
    assert calls and "not proven" in calls[0][0]


def test_a_run_created_just_before_arming_is_excluded(contestant: Path):
    """No time window: a run from the same second as the arming is still old."""
    just_before = _start(ROOT, contestant, "same-second")
    w = Watch(contestant, timeout=1)
    rc, _calls, out = w.result()
    assert rc == 2 and out["run"] is None and just_before.name in out["excluded"]


def test_a_pending_delivery_keeps_the_watch_going_until_it_is_accepted(contestant: Path):
    w = Watch(contestant)
    run_dir = _start(ROOT, contestant, "pending")
    _deliver(run_dir, False)                      # waiting for the person's acceptance
    assert w.alive_after(1.0), "a delivery record saying false ended the watch"
    _deliver(run_dir, True)
    rc, calls, out = w.result()
    assert rc == 0 and not calls
    stages = [c["stage"] for c in out["checks"]]
    assert stages[-1] == "final" and "pending" in stages
    assert out["run"] == run_dir.name and out["result"] == "ok"


def test_evidence_added_while_pending_is_caught_before_delivery(contestant: Path):
    w = Watch(contestant)
    run_dir = _start(ROOT, contestant, "late-evidence")
    _deliver(run_dir, False)
    assert w.alive_after(0.5)
    (run_dir / "evidence").mkdir()
    (run_dir / "evidence" / "unit.json").write_text(json.dumps({"impl": "0" * 64}), encoding="utf-8")
    _deliver(run_dir, True)
    rc, calls, out = w.result()
    assert rc == 1 and calls and out["checks"][-1]["stage"] == "final" and not out["checks"][-1]["ok"]


def test_a_second_new_run_fails_the_watch(contestant: Path):
    w = Watch(contestant)
    first = _start(ROOT, contestant, "first")
    assert w.alive_after(0.3)
    _start(ROOT, contestant, "second")
    rc, calls, out = w.result()
    assert rc == 1 and out["run"] == first.name and "Two Code Pipeline runs" in calls[0][0]


def test_the_watch_catches_an_outdated_launcher_at_the_start(contestant: Path):
    _copy_pipeline(contestant, outdated=True)
    w = Watch(contestant)
    _start(contestant, contestant, "new-bad")
    rc, calls, out = w.result()
    assert rc == 1 and "Wrong Code Pipeline" in calls[0][0]
    assert [c["stage"] for c in out["checks"]] == ["start"]


def test_waiting_takes_only_a_run_started_after_the_command(contestant: Path):
    import threading
    _start(ROOT, contestant, "old-good")
    assert prov.main(["--repo", str(contestant), "--wait", "0.5"]) == 2     # the old run is not new
    _copy_pipeline(contestant, outdated=True)
    timer = threading.Timer(0.5, lambda: _start(contestant, contestant, "new-bad"))
    timer.start()
    assert prov.main(["--repo", str(contestant), "--wait", "20"]) == 1      # the new, outdated one
    timer.join(30)


def test_the_launcher_records_existing_runs_and_passes_them_to_the_watch(contestant: Path, tmp_path: Path,
                                                                          monkeypatch, capsys):
    import pipeline_experiment as exp
    monkeypatch.setattr(exp, "RESULTS", tmp_path / "results")
    subprocess.run(["git", "init", "-q", str(contestant)], check=True)
    old = _start(ROOT, contestant, "old-good")
    assert exp.main(["--worktree", str(contestant), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert prov.pipeline_identity(prov.DEFAULT_APPROVED)[:12] in out
    assert prov.workflow_identity(ROOT / "workforce")[:12] in out
    armed = tmp_path / "results" / f"{contestant.name}.armed.json"
    assert json.loads(armed.read_text(encoding="utf-8")) == [old.name]
    argv, result = exp.job_command(contestant, armed, 2)
    cmd = argv[argv.index("--command") + 1]
    assert argv[2:4] == ["job", "run"] and "WATCH DONE" in argv
    assert "--watch" in cmd and f'--exclude-file "{armed}"' in cmd and "--since" not in cmd
    assert exp.main(["--worktree", str(contestant / "nope"), "--dry-run"]) == 2
