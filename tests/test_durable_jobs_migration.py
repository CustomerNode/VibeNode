"""Durable Jobs step 6: the three laptop-patch jobs, migrated, behave as in
acceptance tests 1 to 3 under a dry run (acceptance test 6).

``resume_jobs.json`` (the per-laptop patch of 2026-10-15) has three shapes of
entry: a done marker in the newest log matching a glob (the authorship read),
a last-line state file whose "done" alternative finishes and whose other
alternative needs a person (the discussions census), and a whole-file pattern
on the waiter's log where "DONE" finishes and "rebuild launched on donvm"
is the irreversible step.  ``migrate_resume_jobs`` turns them into registry
records; ``register_existing`` records them WITHOUT launching (the jobs are
running); the supervisor's dry run then decides without changing anything.

The fixture below has the same shape as the real file with generic paths.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from daemon import durable_jobs as dj
from daemon import job_supervisor as js

RESUME_JOBS = {
    "about": "Long jobs relaunched by resume_after_reboot.ps1 when the user signs in after a restart.",
    "jobs": [
        {
            "name": "authorship read",
            "running": "*authorship.py*--workers*",
            "doneLogGlob": "authorship-[0-9]*.log",
            "doneText": "AUTHORSHIP_DONE",
            "launch": {"via": "collect", "name": "authorship",
                       "command": "python -X utf8 -u authorship.py --tp T --package P --out O --rate 300 --workers 30"},
        },
        {
            "name": "authorship summary commit, then discussions census",
            "running": "*discussions_after_authorship.py*",
            "stopIfLastLineMatches": "done$|needs a person",
            "lastLineFile": "discussions_after_authorship.state.txt",
            "launch": {"via": "collect", "name": "discussions-after-authorship",
                       "command": "python -u \"RD\\discussions_after_authorship.py\""},
        },
        {
            "name": "final rebuild waiter",
            "running": "*final_rebuild_waiter.py*",
            "stopIfFileMatches": "Z rebuild launched on donvm|Z DONE$|Z STOP: ",
            "matchFile": "final-rebuild-waiter.log",
            "launch": {"via": "task", "task": "ClaudeRunDetached_final-rebuild-waiter"},
        },
    ],
}

WAITER_CMD = "C:\\Python314\\python.exe -u final_rebuild_waiter.py"
LIVE = ["python -X utf8 -u authorship.py --tp T --package P --out O --rate 300 --workers 30",
        "python -u \"RD\\discussions_after_authorship.py\"",
        "C:\\Python314\\python.exe -u final_rebuild_waiter.py"]


@pytest.fixture
def world(tmp_path, monkeypatch):
    rd = tmp_path / "run-detached"
    collect = tmp_path / "collect"
    rd.mkdir()
    collect.mkdir()
    src = rd / "resume_jobs.json"
    src.write_text(json.dumps(RESUME_JOBS))
    reg = tmp_path / "jobs.json"
    monkeypatch.setenv("VIBENODE_JOBS_FILE", str(reg))
    # The jobs' own files, as they stand while the jobs run.
    (rd / "authorship-1792068205.log").write_text("heartbeat\ncells 17000/22919 {'ok': 17000}\n")
    (rd / "discussions_after_authorship.state.txt").write_text(
        "10:00 authorship process is gone WITHOUT AUTHORSHIP_DONE; nothing resumed (needs a person)\n"
        "11:00 waiting for the authorship read\n")
    (rd / "final-rebuild-waiter.log").write_text("Z gates: 1 authorship=WAIT\n")
    launched = []
    def launch(command, cwd, log_path, env):
        launched.append(command)
        return {"ok": True, "pid": 1, "launcher": "", "error": ""}
    return dict(rd=rd, collect=collect, src=src, reg=reg, launched=launched, launch=launch)


def _migrate(w):
    fields = dj.migrate_resume_jobs(w["src"], w["rd"], w["collect"], WAITER_CMD)
    return [dj.register_existing(**f) for f in fields]


def _sup(w, procs):
    return js.JobSupervisor(None, proc_lister=lambda: procs, launch=w["launch"], path=w["reg"])


def test_migration_registers_the_three_jobs_without_launching(world):
    w = world
    recs = _migrate(w)
    assert [r["id"] for r in recs] == ["authorship", "discussions-after-authorship", "final-rebuild-waiter"]
    assert w["launched"] == [], "migration must not restart a running job"
    assert all(r["state"] == "running" for r in recs)
    auth, disc, waiter = recs
    assert auth["done_test"] == {"pattern": "AUTHORSHIP_DONE", "file": "authorship-[0-9]*.log", "last_line": False}
    assert auth["progress"]["pattern"] == r"cells (\d+)/(\d+)" and auth["cwd"] == str(w["collect"])
    assert auth["running_match"] == "*authorship.py*--workers*"
    assert disc["done_test"] == {"pattern": "done$", "file": "discussions_after_authorship.state.txt", "last_line": True}
    assert disc["irreversible_after"] == {"pattern": "needs a person", "file": "discussions_after_authorship.state.txt", "last_line": True}
    assert waiter["command"] == WAITER_CMD and waiter["cwd"] == str(w["rd"])
    assert waiter["done_test"]["pattern"] == "Z DONE$" and waiter["done_test"]["file"] == "final-rebuild-waiter.log"
    assert waiter["irreversible_after"]["pattern"] == "Z rebuild launched on donvm|Z STOP: "
    assert dj.load_registry(w["reg"])["jobs"].keys() == {"authorship", "discussions-after-authorship", "final-rebuild-waiter"}


def test_6_dry_run_leaves_running_jobs_alone(world):
    w = world
    _migrate(w)
    before = w["reg"].read_bytes()
    d = {x["id"]: x for x in _sup(w, LIVE).tick(dry_run=True)}
    assert {x["action"] for x in d.values()} == {"alive"}
    assert d["authorship"]["progress"]["pct"] == 74.2
    assert w["reg"].read_bytes() == before and w["launched"] == []


def test_6_as_in_1_dead_jobs_would_be_relaunched(world):
    """Acceptance 1 under -WhatIf: process gone, not done → relaunch."""
    w = world
    _migrate(w)
    before = w["reg"].read_bytes()
    d = {x["id"]: x for x in _sup(w, []).tick(dry_run=True)}
    assert {x["action"] for x in d.values()} == {"relaunch"}, d
    assert w["reg"].read_bytes() == before and w["launched"] == []
    # And for real (not dry): each is relaunched once with its own command and folder.
    _sup(w, []).tick()
    assert sorted(w["launched"]) == sorted([RESUME_JOBS["jobs"][0]["launch"]["command"],
                                            RESUME_JOBS["jobs"][1]["launch"]["command"], WAITER_CMD])
    assert {r["state"] for r in dj.list_jobs(w["reg"])} == {"relaunched"}


def test_6_as_in_2_done_markers_finish_each_job(world):
    """Acceptance 2: the done marker present → finished, not relaunched."""
    w = world
    _migrate(w)
    with (w["rd"] / "authorship-1792068205.log").open("a") as fh:
        fh.write("cells 22919/22919\nAUTHORSHIP_DONE\n")
    with (w["rd"] / "discussions_after_authorship.state.txt").open("a") as fh:
        fh.write("12:00 census done\n")
    with (w["rd"] / "final-rebuild-waiter.log").open("a") as fh:
        fh.write("Z DONE\n")
    d = {x["id"]: x for x in _sup(w, []).tick(dry_run=True)}
    assert {x["action"] for x in d.values()} == {"finish"}, d
    _sup(w, []).tick()
    assert {r["state"] for r in dj.list_jobs(w["reg"])} == {"finished"} and w["launched"] == []


def test_6_as_in_3_irreversible_steps_need_a_person(world):
    """Acceptance 3: the irreversible marker → needs_person, never relaunched."""
    w = world
    _migrate(w)
    with (w["rd"] / "discussions_after_authorship.state.txt").open("a") as fh:
        fh.write("12:00 commit failed (needs a person)\n")
    with (w["rd"] / "final-rebuild-waiter.log").open("a") as fh:
        fh.write("Z rebuild launched on donvm\n")
    d = {x["id"]: x for x in _sup(w, []).tick(dry_run=True)}
    assert d["discussions-after-authorship"]["action"] == "needs_person"
    assert d["final-rebuild-waiter"]["action"] == "needs_person"
    assert d["authorship"]["action"] == "relaunch"      # no irreversible step of its own
    pushes = []
    class SM:
        _push_callback = staticmethod(lambda ev, data: pushes.append((ev, data["job_id"])))
    js.JobSupervisor(SM(), proc_lister=lambda: [], launch=w["launch"], path=w["reg"]).tick()
    states = {r["id"]: r["state"] for r in dj.list_jobs(w["reg"])}
    assert states == {"authorship": "relaunched", "discussions-after-authorship": "needs_person",
                      "final-rebuild-waiter": "needs_person"}
    assert w["launched"] == [RESUME_JOBS["jobs"][0]["launch"]["command"]]
    assert sorted(pushes) == [("job_needs_person", "discussions-after-authorship"),
                              ("job_needs_person", "final-rebuild-waiter")]


def test_the_old_needs_a_person_line_does_not_count(world):
    """The state file's history already contains 'needs a person' (the ps1
    only looked at the last line).  So does the registry's last_line rule."""
    w = world
    _migrate(w)
    d = {x["id"]: x for x in _sup(w, []).tick(dry_run=True)}
    assert d["discussions-after-authorship"]["action"] == "relaunch"
