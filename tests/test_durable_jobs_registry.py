"""Durable Jobs step 1: the registry and its schema (daemon/durable_jobs.py).

Spec: docs/plans/durable-jobs-spec.md.  Covered here:

* a record has every field the spec lists, and registration refuses a job
  with no done test (rule 5);
* the registry round-trips, is written atomically, and a corrupt file is
  reported and left byte-for-byte untouched (acceptance test 5, file side);
* the done test, the irreversible marker and the progress pattern are read
  from the job's own log or the newest file matching a glob, with
  ``last_line`` honoured;
* ``parse_progress`` turns "cells 90000/143719" into a percentage
  (acceptance test 7, data side);
* ``launch_detached`` really starts a command on this OS that outlives the
  launcher and writes to the log (the launch step of step 2).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from daemon import durable_jobs as dj


@pytest.fixture
def reg(tmp_path, monkeypatch):
    path = tmp_path / "jobs.json"
    monkeypatch.setenv("VIBENODE_JOBS_FILE", str(path))
    return path


def _fields(tmp_path, **over):
    base = dict(name="demo", command="python -c pass", cwd=str(tmp_path),
                done_test="DONE")
    base.update(over)
    return base


def test_record_has_the_spec_fields(tmp_path):
    rec = dj.new_record(**_fields(tmp_path, session_id="s1", project="P",
                                  progress=r"cells (\d+)/(\d+)",
                                  irreversible_after="rebuild launched on donvm"))
    for field in ("id", "name", "project", "session_id", "command", "cwd", "env_file",
                  "running_match", "done_test", "progress", "irreversible_after",
                  "log_path", "registered_at", "last_seen_alive", "relaunch_count", "state"):
        assert field in rec, field
    assert rec["state"] == "running"
    assert rec["running_match"] == "python -c pass"     # defaults to the command
    assert rec["done_test"] == {"pattern": "DONE", "file": "", "last_line": False}
    assert rec["irreversible_after"]["pattern"] == "rebuild launched on donvm"
    assert "{ts}" in rec["log_path"]


def test_registration_refuses_a_missing_done_test(tmp_path):
    with pytest.raises(dj.JobError, match="done test is required"):
        dj.new_record(**_fields(tmp_path, done_test=""))
    with pytest.raises(dj.JobError, match="done test is required"):
        dj.new_record(**_fields(tmp_path, done_test=None))
    with pytest.raises(dj.JobError, match="bad pattern"):
        dj.new_record(**_fields(tmp_path, done_test="("))
    with pytest.raises(dj.JobError, match="command"):
        dj.new_record(**_fields(tmp_path, command="  "))
    with pytest.raises(dj.JobError, match="cwd"):
        dj.new_record(**_fields(tmp_path, cwd=str(tmp_path / "missing")))


def test_registry_round_trips_and_is_empty_when_missing(reg, tmp_path):
    assert dj.load_registry(reg) == {"version": 1, "jobs": {}}
    rec = dj.new_record(**_fields(tmp_path, job_id="a"))
    with dj.edit_registry(reg) as data:
        data["jobs"]["a"] = rec
    assert reg.exists()
    assert not list(reg.parent.glob("*.tmp")), "atomic write leaves no temp file"
    assert dj.get_job("a", reg)["name"] == "demo"
    assert [j["id"] for j in dj.list_jobs(reg)] == ["a"]


@pytest.mark.parametrize("junk", [b"{not json", b'{"jobs": []}', b'{"jobs": {"x": {"state": "zzz"}}}',
                                  b"\xff\xfe\x00"])
def test_a_corrupt_registry_is_reported_and_left_untouched(reg, junk):
    reg.write_bytes(junk)
    before = reg.read_bytes()
    with pytest.raises(dj.RegistryError, match="untouched"):
        dj.load_registry(reg)
    # Even an edit attempt must not rewrite it.
    with pytest.raises(dj.RegistryError):
        with dj.edit_registry(reg) as data:
            data["jobs"]["new"] = {}
    assert reg.read_bytes() == before


def test_done_marker_is_read_from_the_newest_glob_match(reg, tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    rec = dj.new_record(**_fields(tmp_path, job_id="auth",
                                  log_path=str(logs / "authorship-{ts}.log"),
                                  done_test={"pattern": "AUTHORSHIP_DONE",
                                             "file": "authorship-[0-9]*.log"}))
    old = logs / "authorship-100.log"
    old.write_text("cells 1/2\nAUTHORSHIP_DONE\n")
    os.utime(old, (1, 1))
    new = logs / "authorship-200.log"
    new.write_text("cells 5/10\n")
    assert not dj.is_done(rec), "the marker in an OLD log does not count"
    new.write_text("cells 10/10\nAUTHORSHIP_DONE\n")
    assert dj.is_done(rec)


def test_last_line_restricts_a_pattern_to_the_end_of_a_state_file(reg, tmp_path):
    state = tmp_path / "job.state.txt"
    state.write_text("09:00 authorship gone (needs a person)\n10:00 waiting for the read\n")
    rec = dj.new_record(**_fields(tmp_path, job_id="disc",
                                  log_path=str(tmp_path / "disc-{ts}.log"),
                                  done_test={"pattern": "done$", "file": "job.state.txt", "last_line": True},
                                  irreversible_after={"pattern": "needs a person", "file": "job.state.txt",
                                                      "last_line": True}))
    assert not dj.is_past_irreversible(rec), "an OLD 'needs a person' line must not count"
    assert not dj.is_done(rec)
    with state.open("a") as fh:
        fh.write("11:00 census done\n")
    assert dj.is_done(rec)
    with state.open("a") as fh:
        fh.write("12:00 stuck (needs a person)\n")
    assert dj.is_past_irreversible(rec)


def test_progress_parses_n_of_m_into_a_percentage(reg, tmp_path):
    """Acceptance test 7 (data side): "cells 90000/143719" -> 62.6%."""
    log = tmp_path / "run.log"
    log.write_text("heartbeat\ncells 1000/143719 {'ok': 1000}\ncells 90000/143719 {'ok': 90000}\nheartbeat\n")
    rec = dj.new_record(**_fields(tmp_path, log_path=str(log), progress=r"cells (\d+)/(\d+)"))
    rec["last_log"] = str(log)
    p = dj.parse_progress(rec)
    assert p["n"] == 90000 and p["m"] == 143719
    assert p["pct"] == 62.6
    assert p["text"] == "cells 90000/143719"


def test_is_running_matches_command_lines_but_not_onlookers(tmp_path):
    rec = dj.new_record(**_fields(tmp_path, command="python -u authorship.py --workers 30",
                                  running_match="*authorship.py*--workers*"))
    lines = ["C:\\Python\\python.exe  -u authorship.py --tp x --workers 30",
             "python vibenode.py job run --command \"python -u authorship.py --workers 30\""]
    assert dj.is_running(rec, lines)
    assert not dj.is_running(rec, lines[1:]), "the registering CLI is not the job"
    assert not dj.is_running(rec, ["python other.py"])
    plain = dj.new_record(**_fields(tmp_path, command="python -u  waiter.py"))
    assert dj.is_running(plain, ["cmd /c cd /d C:\\x && python -u waiter.py >> log 2>&1"])


def test_launch_detached_runs_the_command_and_writes_the_log(tmp_path):
    """A real launch on this OS: the job outlives the call and its output
    lands in the log."""
    log = tmp_path / "job-{ts}.log"
    rec = dj.new_record(**_fields(tmp_path, log_path=str(log),
                                  command=f'"{sys.executable}" -c "import time;print(\'hello\');time.sleep(0.5);print(\'DONE\')"'))
    with dj.edit_registry(tmp_path / "r.json") as data:
        res = dj._launch_record(rec)
        data["jobs"][rec["id"]] = rec
    assert res["ok"], res
    assert rec["last_log"]
    deadline = time.time() + 20
    while time.time() < deadline and "DONE" not in dj.read_tail(rec["last_log"]):
        time.sleep(0.2)
    assert "hello" in dj.read_tail(rec["last_log"])
    assert dj.is_done(rec)


def test_run_job_registers_a_failed_launch_so_it_is_seen(reg, tmp_path):
    def bad_launch(command, cwd, log_path, env):
        return {"ok": False, "error": "no such interpreter"}
    rec = dj.run_job(launch=bad_launch, **_fields(tmp_path, job_id="bad"))
    assert rec["state"] == "failed" and "no such interpreter" in rec["reason"]
    assert dj.get_job("bad", reg)["state"] == "failed"


def test_run_job_passes_env_file_and_expands_the_log_path(reg, tmp_path):
    envf = tmp_path / "secrets.env"
    envf.write_text("# comment\nTOKEN = 'abc'\nEMPTY\n")
    seen = {}
    def fake_launch(command, cwd, log_path, env):
        seen.update(command=command, cwd=cwd, log=log_path, env=env)
        return {"ok": True, "pid": 1, "launcher": "", "error": ""}
    rec = dj.run_job(launch=fake_launch, **_fields(tmp_path, job_id="e", env_file=str(envf),
                                                   log_path=str(tmp_path / "e-{ts}.log")))
    assert seen["env"] == {"TOKEN": "abc"}
    assert "{ts}" not in seen["log"] and seen["log"] == rec["last_log"]
    assert rec["state"] == "running" and dj.get_job("e", reg)["last_launch_at"] > 0
